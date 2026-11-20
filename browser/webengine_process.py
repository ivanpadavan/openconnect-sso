import asyncio
import json
import multiprocessing
import signal
import sys
from urllib.parse import urlparse

import attr
import pkg_resources
import structlog

from PyQt6.QtCore import (
    QFile,
    QIODevice,
    QObject,
    QUrl,
    QTimer,
    pyqtProperty,
    pyqtSignal,
    pyqtSlot,
    Qt,
)
from PyQt6.QtNetwork import QNetworkCookie, QNetworkProxy
from PyQt6.QtWebChannel import QWebChannel
from PyQt6.QtWebEngineCore import QWebEngineScript, QWebEngineProfile, QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QWidget, QSizePolicy, QVBoxLayout

from openconnect_sso import config
from openconnect_sso.sms import SmsCodeWatcher


app = None
logger = structlog.get_logger("webengine")
APPLICATION_WORLD = QWebEngineScript.ScriptWorldId.ApplicationWorld


@attr.s
class Url:
    url = attr.ib()


@attr.s
class PageHtml:
    url = attr.ib()
    html = attr.ib()


@attr.s
class Credentials:
    credentials = attr.ib()


@attr.s
class StartupInfo:
    url = attr.ib()
    credentials = attr.ib()


@attr.s
class SetCookie:
    name = attr.ib()
    value = attr.ib()


class Process(multiprocessing.Process):
    def __init__(self, proxy, display_mode, *, debug_html=False):
        super().__init__()

        self._commands = multiprocessing.Queue()
        self._states = multiprocessing.Queue()
        self.proxy = proxy
        self.display_mode = display_mode
        self.debug_html = debug_html

    def authenticate_at(self, url, credentials):
        self._commands.put(StartupInfo(url, credentials))

    async def get_state_async(self):
        while self.is_alive():
            try:
                return self._states.get_nowait()
            except multiprocessing.queues.Empty:
                await asyncio.sleep(0.01)
        if not self.is_alive():
            raise EOFError()

    def run(self):
        # To work around funky GC conflicts with C++ code by ensuring QApplication terminates last
        global app

        signal.signal(signal.SIGTERM, on_sigterm)
        signal.signal(signal.SIGINT, signal.SIG_DFL)

        cfg = config.load()

        argv = sys.argv.copy()
        if self.display_mode == config.DisplayMode.HIDDEN:
            argv += ["-platform", "minimal"]
        app = QApplication(argv)

        if self.proxy:
            parsed = urlparse(self.proxy)
            if parsed.scheme.startswith("socks5"):
                proxy_type = QNetworkProxy.Socks5Proxy
            elif parsed.scheme.startswith("http"):
                proxy_type = QNetworkProxy.HttpProxy
            else:
                raise ValueError("Unsupported proxy type", parsed.scheme)
            proxy = QNetworkProxy(proxy_type, parsed.hostname, parsed.port)

            QNetworkProxy.setApplicationProxy(proxy)

        # In order to make Python able to handle signals
        force_python_execution = QTimer()
        force_python_execution.start(200)

        def ignore():
            pass

        force_python_execution.timeout.connect(ignore)
        web = WebBrowser(
            cfg.auto_fill_rules,
            cfg.sms,
            self._states.put,
            max_auto_actions=cfg.max_auto_actions,
            debug_html=self.debug_html,
        )

        startup_info = self._commands.get()
        logger.info("Browser started", startup_info=startup_info)

        logger.info("Loading page", url=startup_info.url)

        web.authenticate_at(QUrl(startup_info.url), startup_info.credentials)

        web.show()
        try:
            rc = app.exec()
        finally:
            web.close()

        logger.info("Exiting browser")
        return rc

    async def wait(self):
        while self.is_alive():
            await asyncio.sleep(0.01)
        self.join()


def on_sigterm(signum, frame):
    logger.info("Terminate requested.")
    # Force flush cookieStore to disk. Without this hack the cookieStore may
    # not be synced at all if the browser lives only for a short amount of
    # time. Something is off with the call order of destructors as there is no
    # such issue in C++.

    # See: https://github.com/qutebrowser/qutebrowser/commit/8d55d093f29008b268569cdec28b700a8c42d761
    cookie = QNetworkCookie()
    QWebEngineProfile.defaultProfile().cookieStore().deleteCookie(cookie)

    # Give some time to actually save cookies
    exit_timer = QTimer(app)
    exit_timer.timeout.connect(QApplication.quit)
    exit_timer.start(1000)  # ms


class SmsBridge(QObject):
    code_received = pyqtSignal(str)
    error_received = pyqtSignal(str)


class AutoFillBudget(QObject):
    exhausted_changed = pyqtSignal()

    def __init__(self, limit, parent):
        super().__init__(parent)
        self._limit = limit
        self._remaining = limit

    @pyqtProperty(bool, notify=exhausted_changed)
    def exhausted(self):
        return self._remaining == 0

    @pyqtSlot(result=bool)
    def reserve(self):
        if self.exhausted:
            return False

        self._remaining -= 1
        if self.exhausted:
            logger.info("SSO autofill action limit reached", max_auto_actions=self._limit)
            self.exhausted_changed.emit()
        return True


class WebBrowser(QWebEngineView):
    def __init__(
        self, auto_fill_rules, sms_config, on_update, *, max_auto_actions=7, debug_html=False
    ):
        super().__init__()
        self._on_update = on_update
        self._auto_fill_rules = auto_fill_rules
        self._debug_html = debug_html
        self._autofill_enabled = max_auto_actions > 0
        self._popup_windows = set()
        self._budget = AutoFillBudget(max_auto_actions, self)
        self._channel = QWebChannel(self)
        self._channel.registerObject("budget", self._budget)
        self._sms_config = sms_config
        self._sms_code = None
        self._sms_watcher = None
        self._sms_bridge = SmsBridge()
        self._sms_bridge.code_received.connect(self._on_sms_code)
        self._sms_bridge.error_received.connect(self._on_sms_error)
        cookie_store = self.page().profile().cookieStore()
        cookie_store.cookieAdded.connect(self._on_cookie_added)
        self._setup_view(self, is_main=True)

    def _setup_view(self, view, *, is_main=False):
        view.page().setWebChannel(self._channel, APPLICATION_WORLD)
        view.loadFinished.connect(
            lambda success: self._on_load_finished(view.page(), success, is_main)
        )

    def createWindow(self, type):
        if type == QWebEnginePage.WebWindowType.WebDialog:
            popup = WebPopupWindow(self.page().profile())
            self._popup_windows.add(popup)
            popup.destroyed.connect(lambda: self._popup_windows.discard(popup))
            view = popup.view()
            self._setup_view(view)
            view.page().scripts().insert(self.page().scripts().toList())
            return view

    def authenticate_at(self, url, credentials):
        sms_selectors = get_sms_selectors(self._auto_fill_rules)
        if self._autofill_enabled and sms_selectors:
            if not self._sms_config:
                raise ValueError(
                    'fill = "sms" requires an [sms] config section with chat_id and regex'
                )
            self._sms_config.validate()
            self._start_sms_watcher()

        if self._autofill_enabled and credentials:
            self._install_autofill_scripts(credentials)

        self.load(QUrl(url))

    def _install_autofill_scripts(self, credentials):
        logger.info("Initiating autologin", cred=credentials)
        channel_source = QFile(":/qtwebchannel/qwebchannel.js")
        if not channel_source.open(QIODevice.OpenModeFlag.ReadOnly):
            raise RuntimeError("Unable to load Qt WebChannel JavaScript")
        script = QWebEngineScript()
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(APPLICATION_WORLD)
        script.setSourceCode(
            bytes(channel_source.readAll()).decode()
            + "\n"
            + pkg_resources.resource_string(__name__, "user.js").decode()
        )
        self.page().scripts().insert(script)

        for url_pattern, rules in self._auto_fill_rules.items():
            script = QWebEngineScript()
            script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
            script.setWorldId(APPLICATION_WORLD)
            script.setSourceCode(
                f"""
// ==UserScript==
// @include {url_pattern}
// ==/UserScript==

window.__openconnect_sso_register_rules({json.dumps(get_autofill_rules(rules, credentials))});
"""
            )
            self.page().scripts().insert(script)

    def _start_sms_watcher(self):
        if self._sms_watcher:
            return
        self._sms_watcher = SmsCodeWatcher(self._sms_config)
        self._sms_watcher.start(
            self._sms_bridge.code_received.emit,
            self._sms_bridge.error_received.emit,
        )

    def _on_sms_code(self, code):
        self._sms_code = code
        logger.info("SMS code received in browser")
        self._set_sms_code(self.page())
        for popup in self._popup_windows:
            self._set_sms_code(popup.view().page())

    def _on_sms_error(self, message):
        logger.error("SMS autofill error", message=message)

    def close(self):
        if self._sms_watcher:
            self._sms_watcher.stop()
            self._sms_watcher = None
        for popup in tuple(self._popup_windows):
            popup.close()
        super().close()

    def _set_sms_code(self, page):
        if not self._sms_code:
            return
        logger.debug(
            "Setting SMS code for autofill",
            url=page.url().toString(),
        )
        page.runJavaScript(make_sms_value_script(self._sms_code), APPLICATION_WORLD)

    def _on_cookie_added(self, cookie):
        logger.debug("Cookie set", name=to_str(cookie.name()))
        self._on_update(SetCookie(to_str(cookie.name()), to_str(cookie.value())))

    def _on_load_finished(self, page, success, is_main):
        url = page.url().toString()
        logger.debug("Page loaded", url=url)
        self._set_sms_code(page)

        if success and self._debug_html:
            page.toHtml(lambda html: self._report_page(url, is_main, html))
        else:
            self._report_page(url, is_main)

    def _report_page(self, url, is_main, html=None):
        if html:
            self._on_update(PageHtml(url, html))
        if is_main:
            self._on_update(Url(url))


class WebPopupWindow(QWidget):
    def __init__(self, profile):
        super().__init__()
        self._view = QWebEngineView(self)

        super().setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        super().setSizePolicy(QSizePolicy.Policy.Minimum, QSizePolicy.Policy.Minimum)

        layout = QVBoxLayout()
        super().setLayout(layout)
        layout.addWidget(self._view)

        self._view.setPage(QWebEnginePage(profile, self._view))

        self._view.titleChanged.connect(super().setWindowTitle)
        self._view.page().geometryChangeRequested.connect(
            self.handleGeometryChangeRequested
        )
        self._view.page().windowCloseRequested.connect(super().close)

    def view(self):
        return self._view

    @pyqtSlot("const QRect")
    def handleGeometryChangeRequested(self, newGeometry):
        self._view.setMinimumSize(newGeometry.width(), newGeometry.height())
        super().move(newGeometry.topLeft() - self._view.pos())
        super().resize(0, 0)
        super().show()


def to_str(qval):
    return bytes(qval).decode()


def get_autofill_rules(rules, credentials):
    actions = []
    for rule in rules:
        action = {"selector": rule.selector}
        if rule.action == "stop":
            action["type"] = "stop"
        elif rule.fill == "sms":
            action["type"] = "sms"
        elif rule.fill:
            value = getattr(credentials, rule.fill, None)
            if not value:
                logger.warning(
                    "Credential info not available",
                    type=rule.fill,
                    possibilities=dir(credentials),
                )
                continue
            action.update(type="fill", value=value)
        elif rule.action == "click":
            action["type"] = "click"
        else:
            continue
        actions.append(action)
    return actions


def get_sms_selectors(auto_fill_rules):
    return [
        rule.selector
        for rules in auto_fill_rules.values()
        for rule in rules
        if rule.fill == "sms"
    ]


def make_sms_value_script(code):
    code_json = json.dumps(code)
    return f"""
(function() {{
    window.__openconnect_sso_sms = {code_json};
}})();
"""
