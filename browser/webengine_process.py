import asyncio
import json
import multiprocessing
import signal
import sys
from urllib.parse import urlparse

import attr
import pkg_resources
import structlog

from PyQt6.QtCore import QObject, QUrl, QTimer, pyqtSignal, pyqtSlot, Qt
from PyQt6.QtNetwork import QNetworkCookie, QNetworkProxy
from PyQt6.QtWebEngineCore import QWebEngineScript, QWebEngineProfile, QWebEnginePage
from PyQt6.QtWebEngineWidgets import QWebEngineView
from PyQt6.QtWidgets import QApplication, QWidget, QSizePolicy, QVBoxLayout

from openconnect_sso import config
from openconnect_sso.sms import SmsCodeWatcher


app = None
logger = structlog.get_logger("webengine")


@attr.s
class Url:
    url = attr.ib()


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
    def __init__(self, proxy, display_mode):
        super().__init__()

        self._commands = multiprocessing.Queue()
        self._states = multiprocessing.Queue()
        self.proxy = proxy
        self.display_mode = display_mode

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
        web = WebBrowser(cfg.auto_fill_rules, cfg.sms, self._states.put)

        startup_info = self._commands.get()
        logger.info("Browser started", startup_info=startup_info)

        logger.info("Loading page", url=startup_info.url)

        web.authenticate_at(QUrl(startup_info.url), startup_info.credentials)

        web.show()
        rc = app.exec()

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
    def __init__(self, auto_fill_rules, sms_config, on_update):
        super().__init__()
        self._on_update = on_update
        self._auto_fill_rules = auto_fill_rules
        self._sms_config = sms_config
        self._sms_code = None
        self._sms_watcher = None
        self._sms_bridge = SmsBridge()
        self._sms_bridge.code_received.connect(self._on_sms_code)
        self._sms_bridge.error_received.connect(self._on_sms_error)
        cookie_store = self.page().profile().cookieStore()
        cookie_store.cookieAdded.connect(self._on_cookie_added)
        self.page().loadFinished.connect(self._on_load_finished)

    def createWindow(self, type):
        if type == QWebEnginePage.WebDialog:
            self._popupWindow = WebPopupWindow(self.page().profile())
            return self._popupWindow.view()

    def authenticate_at(self, url, credentials):
        sms_selectors = get_sms_selectors(self._auto_fill_rules)
        if sms_selectors:
            if not self._sms_config:
                raise ValueError(
                    'fill = "sms" requires an [sms] config section with chat_id and regex'
                )
            self._sms_config.validate()
            self._start_sms_watcher()

        script_source = pkg_resources.resource_string(__name__, "user.js").decode()
        script = QWebEngineScript()
        script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentCreation)
        script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
        script.setSourceCode(script_source)
        self.page().scripts().insert(script)

        if credentials:
            logger.info("Initiating autologin", cred=credentials)
            for url_pattern, rules in self._auto_fill_rules.items():
                script = QWebEngineScript()
                script.setInjectionPoint(QWebEngineScript.InjectionPoint.DocumentReady)
                script.setWorldId(QWebEngineScript.ScriptWorldId.ApplicationWorld)
                script.setSourceCode(
                    f"""
// ==UserScript==
// @include {url_pattern}
// ==/UserScript==

function autoFill() {{
    {get_selectors(rules, credentials)}
    setTimeout(autoFill, 1000);
}}
autoFill();
"""
                )
                self.page().scripts().insert(script)

        self.load(QUrl(url))

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
        self._fill_sms_code()

    def _on_sms_error(self, message):
        logger.error("SMS autofill error", message=message)

    def _fill_sms_code(self):
        if not self._sms_code:
            return
        selectors = get_sms_selectors(self._auto_fill_rules)
        if not selectors:
            return
        self.page().runJavaScript(make_sms_fill_script(selectors, self._sms_code))

    def _on_cookie_added(self, cookie):
        logger.debug("Cookie set", name=to_str(cookie.name()))
        self._on_update(SetCookie(to_str(cookie.name()), to_str(cookie.value())))

    def _on_load_finished(self, success):
        url = self.page().url().toString()
        logger.debug("Page loaded", url=url)
        self._fill_sms_code()

        self._on_update(Url(url))


class WebPopupWindow(QWidget):
    def __init__(self, profile):
        super().__init__()
        self._view = QWebEngineView(self)

        super().setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        super().setSizePolicy(QSizePolicy.Minimum, QSizePolicy.Minimum)

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


def get_selectors(rules, credentials):
    statements = []
    for rule in rules:
        selector = json.dumps(rule.selector)
        if rule.action == "stop":
            statements.append(
                f"""var elem = document.querySelector({selector}); if (elem) {{ return; }}"""
            )
        elif rule.fill:
            if rule.fill == "sms":
                statements.append(
                    f"""var elem = document.querySelector({selector}); if (elem && !elem.value) {{ return; }}"""
                )
                continue
            value = json.dumps(getattr(credentials, rule.fill, None))
            if value:
                statements.append(
                    f"""var elem = document.querySelector({selector}); if (elem) {{ elem.dispatchEvent(new Event("focus")); elem.value = {value}; elem.dispatchEvent(new Event("blur")); elem.dispatchEvent(new Event("input")); }}"""
                )
            else:
                logger.warning(
                    "Credential info not available",
                    type=rule.fill,
                    possibilities=dir(credentials),
                )
        elif rule.action == "click":
            statements.append(
                f"""var elem = document.querySelector({selector}); if (elem) {{ elem.dispatchEvent(new Event("focus")); elem.click(); }}"""
            )
    return "\n".join(statements)


def get_sms_selectors(auto_fill_rules):
    return [
        rule.selector
        for rules in auto_fill_rules.values()
        for rule in rules
        if rule.fill == "sms"
    ]


def make_sms_fill_script(selectors, code):
    selectors_json = json.dumps(selectors)
    code_json = json.dumps(code)
    return f"""
(function() {{
    var selectors = {selectors_json};
    var code = {code_json};
    for (var i = 0; i < selectors.length; i++) {{
        var elem = document.querySelector(selectors[i]);
        if (!elem) {{
            continue;
        }}
        elem.dispatchEvent(new Event("focus"));
        elem.value = code;
        elem.dispatchEvent(new Event("input"));
        elem.dispatchEvent(new Event("change"));
        elem.dispatchEvent(new Event("blur"));
    }}
}})();
"""
