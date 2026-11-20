import asyncio
import json
import multiprocessing
import signal
import sys
from urllib.parse import urlparse

import attr
import pkg_resources
import structlog

from PyQt6 import sip
from PyQt6.QtCore import QObject, QUrl, QTimer, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtNetwork import QNetworkCookie, QNetworkProxy
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
    actions_completed = attr.ib(default=0)


@attr.s
class PageHtml:
    url = attr.ib()
    html = attr.ib()


@attr.s
class AutoFillLimitExceeded:
    limit = attr.ib()


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


class WebBrowser(QWebEngineView):
    def __init__(
        self, auto_fill_rules, sms_config, on_update, *, max_auto_actions=7, debug_html=False
    ):
        super().__init__()
        self._on_update = on_update
        self._auto_fill_rules = auto_fill_rules
        self._debug_html = debug_html
        self._max_auto_actions = max_auto_actions
        self._auto_actions = 0
        self._autofill_running = False
        self._autofill_in_flight = False
        self._pending_urls = []
        self._autofill_timer = QTimer(self)
        self._autofill_timer.setSingleShot(True)
        self._autofill_timer.timeout.connect(self._run_autofill)
        self._popup_windows = set()
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
        if self._max_auto_actions and sms_selectors:
            if not self._sms_config:
                raise ValueError(
                    'fill = "sms" requires an [sms] config section with chat_id and regex'
                )
            self._sms_config.validate()
            self._start_sms_watcher()

        if self._max_auto_actions and credentials and self._auto_fill_rules:
            self._install_autofill_scripts(credentials)
            self._autofill_running = True
            self._autofill_timer.start(0)

        self.load(QUrl(url))

    def _install_autofill_scripts(self, credentials):
        logger.info("Initiating autologin", cred=credentials)
        script = QWebEngineScript()
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(APPLICATION_WORLD)
        script.setSourceCode(
            pkg_resources.resource_string(__name__, "user.js").decode()
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

    def _run_autofill(self):
        views = iter([self, *(popup.view() for popup in self._popup_windows)])
        self._autofill_next_view(views)

    def _autofill_next_view(self, views):
        if not self._autofill_running:
            return

        view = next((view for view in views if not sip.isdeleted(view)), None)
        if view is None:
            self._autofill_timer.start(1000)
            return

        remaining = self._max_auto_actions - self._auto_actions

        # Keep one request in flight for the whole browser so each page sees the
        # remaining budget after the previous page's actions have run.
        self._autofill_in_flight = True
        view.page().runJavaScript(
            f"window.__openconnect_sso_autofill?.({remaining})",
            APPLICATION_WORLD,
            lambda result: self._autofill_finished(views, result),
        )

    def _autofill_finished(self, views, result):
        self._autofill_in_flight = False
        if not self._autofill_running:
            return
        if result is not None:
            self._auto_actions += result["performed"]
            if result["overflow"]:
                self._autofill_running = False
                self._on_update(AutoFillLimitExceeded(self._max_auto_actions))
                return
        for url in self._pending_urls:
            self._on_update(Url(url, self._auto_actions))
        self._pending_urls.clear()
        self._autofill_next_view(views)

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
        self._autofill_running = False
        self._autofill_timer.stop()
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
            # A click can load the final page before its JavaScript result arrives.
            if self._autofill_in_flight:
                self._pending_urls.append(url)
            else:
                self._on_update(Url(url, self._auto_actions))


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
