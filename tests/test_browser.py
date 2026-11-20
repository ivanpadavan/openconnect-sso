import tempfile
import time
import unittest
from pathlib import Path

from PyQt6.QtCore import QUrl
from PyQt6.QtTest import QTest
from PyQt6.QtWebEngineCore import QWebEnginePage
from PyQt6.QtWidgets import QApplication

from openconnect_sso.browser.webengine_process import (
    AutoFillLimitExceeded,
    PageHtml,
    Url,
    WebBrowser,
)
from openconnect_sso.config import AutoFillRule, Config, Credentials


class BrowserTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication(["test-browser"])

    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.directory = Path(directory.name)
        self.updates = []

    def make_page(self, name, body):
        path = self.directory / (name + ".html")
        path.write_text("<!DOCTYPE html><html><body>" + body + "</body></html>")
        return QUrl.fromLocalFile(str(path))

    def make_browser(self, *, limit, debug_html=False):
        rules = {
            "file://*": [
                AutoFillRule(selector="#removed", action="click"),
                AutoFillRule(selector="#username", fill="username"),
                AutoFillRule(selector="#next", action="click"),
            ]
        }
        browser = WebBrowser(
            rules, None, self.updates.append, max_auto_actions=limit, debug_html=debug_html
        )
        self.addCleanup(browser.close)
        browser.show()
        return browser

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while not predicate():
            self.assertLess(time.monotonic(), deadline, "Timed out waiting for Qt")
            QTest.qWait(20)

    def js(self, view, source):
        result = []
        view.page().runJavaScript(source, result.append)
        self.wait_until(lambda: result)
        return result[0]

    def test_budget_survives_navigation_and_is_shared_with_popup(self):
        stage = self.make_page("stage", "<p>Next stage</p>")
        start = self.make_page(
            "start",
            '<input id="username" value="alice">'
            '<button id="next" onclick="location.href=\'stage.html\'">Next</button>',
        )
        popup_body = (
            '<button id="removed" onfocus="this.remove()">Removed before click</button>'
            '<input id="username" oninput="this.defaultValue = this.value">'
            '<button id="next" onclick="this.dataset.clicks = 1; this.disabled = true">'
            "Click</button>"
            f"<p>{'a' * 1024} end of HTML</p>"
        )
        dialog = self.make_page("dialog", popup_body)
        after_limit = self.make_page("after-limit", popup_body)

        browser = self.make_browser(limit=3, debug_html=True)
        browser.authenticate_at(start, Credentials("alice"))
        self.wait_until(lambda: Url(stage.toString(), actions_completed=1) in self.updates)

        popup = browser.createWindow(QWebEnginePage.WebWindowType.WebDialog)
        popup.setUrl(dialog)
        popup.window().show()
        self.wait_until(
            lambda: self.js(popup, "document.querySelector('#next')?.dataset.clicks") == "1"
        )

        QTest.qWait(1200)
        self.assertNotIn(AutoFillLimitExceeded(3), self.updates)
        self.assertTrue(self.js(popup, "document.querySelector('#removed') === null"))
        self.assertEqual(self.js(popup, "document.querySelector('#username').value"), "alice")
        self.assertEqual(self.js(popup, "document.querySelector('#next').dataset.clicks"), "1")

        browser.load(after_limit)
        self.wait_until(lambda: AutoFillLimitExceeded(3) in self.updates)
        self.assertEqual(self.js(browser, "document.querySelector('#username').value"), "")
        self.assertIsNone(self.js(browser, "document.querySelector('#next').dataset.clicks"))

        pages = {msg.url: msg.html for msg in self.updates if isinstance(msg, PageHtml)}
        self.assertIn("<p>Next stage</p>", pages[stage.toString()])
        self.assertIn("a" * 1024 + " end of HTML</p></body></html>", pages[dialog.toString()])

    def test_zero_disables_autofill_and_html_logging_is_opt_in(self):
        page = self.make_page("manual", '<input id="username">')
        browser = self.make_browser(limit=0)
        browser.authenticate_at(page, Credentials("alice"))
        self.wait_until(lambda: Url(page.toString()) in self.updates)
        self.assertEqual(self.js(browser, "document.querySelector('#username').value"), "")
        self.assertFalse(any(isinstance(msg, PageHtml) for msg in self.updates))


class ConfigTest(unittest.TestCase):
    def test_max_auto_actions(self):
        self.assertEqual(Config.from_dict({}).max_auto_actions, 7)
        config = Config.from_dict({"max_auto_actions": 3})
        self.assertEqual(config.as_dict()["max_auto_actions"], 3)
        for value in (-1, False, 2.5, "7"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                Config.from_dict({"max_auto_actions": value})
