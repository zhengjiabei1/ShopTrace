"""Real Chromium contract checks for browser observations and bounded actions."""
from __future__ import annotations

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import os
from pathlib import Path
import shutil
import tempfile
import threading
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from webagent.browser import BrowserError, BrowserSession


class _FixtureHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        self.server.posted.append(self.path)
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.end_headers()
        self.wfile.write(b"fixture write reached server")

    def do_GET(self):
        self.server.visited.append(self.path)
        if self.path == "/sandbox/download":
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Disposition", 'attachment; filename="should-not-save.txt"')
            self.end_headers()
            self.wfile.write(b"download fixture")
            return
        if self.path == "/sandbox/detail":
            html = '<!doctype html><title>Details</title><h1>Camera details</h1><p>Visible detail evidence.</p><a href="/sandbox/">Back to catalog</a>'
        elif self.path == "/sandbox/popup":
            html = '<!doctype html><title>Popup</title><p>A popup must not remain open.</p>'
        elif self.path == "/outside":
            html = '<!doctype html><title>Forbidden path</title><p>Outside the allowed path.</p>'
        else:
            html = '''<!doctype html>
<html lang="en"><head><title>Browser fixture catalog</title></head><body>
<h1>Browser fixture catalog</h1>
<label for="query">Search products</label><input id="query" type="text">
<label for="colour">Colour</label><select id="colour"><option value="black">Black</option><option value="blue">Blue</option></select>
<button id="apply" onclick="document.querySelector('#result').textContent='Chosen '+document.querySelector('#query').value+' '+document.querySelector('#colour').value">Apply filters</button>
<p id="result">Waiting for a choice.</p>
<button id="delayed" onclick="this.dataset.clicks=String(Number(this.dataset.clicks||0)+1);document.body.setAttribute('aria-busy','true');setTimeout(()=>{document.querySelector('#result').textContent='Delayed update complete';document.body.setAttribute('aria-busy','false')},200)">Delayed update</button>
<button disabled>Disabled action</button>
<input type="hidden" value="hidden-secret-value"><p style="display:none">hidden-secret-text</p>
<a href="/sandbox/detail">Open details</a>
<a href="/outside">Leave allowed path</a>
<a href="EXTERNAL_ORIGIN/sandbox/">Leave allowed origin</a>
<a href="/sandbox/popup" target="_blank">Open popup</a>
<a href="/sandbox/download" download>Download fixture</a>
<button onclick="fetch('/sandbox/write',{method:'POST'}).then(()=>document.querySelector('#result').textContent='Write reached server').catch(()=>document.querySelector('#result').textContent='Write blocked')">Attempt write</button>
<button onclick="fetch('EXTERNAL_ORIGIN/sandbox/').catch(()=>{});const f=document.createElement('iframe');f.src='EXTERNAL_ORIGIN/sandbox/';document.body.appendChild(f);try{const w=new WebSocket('EXTERNAL_WEBSOCKET/socket');w.onerror=()=>{};}catch(e){}">Embed outside resources</button>
<div style="height:1600px" aria-hidden="true"></div><p>End of catalog.</p>
</body></html>'''.replace("EXTERNAL_ORIGIN", self.server.external_origin).replace("EXTERNAL_WEBSOCKET", self.server.external_origin.replace("http://", "ws://"))
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write(html.encode())

    def log_message(self, *_args):
        pass


@unittest.skipUnless(importlib.util.find_spec("playwright"), "Playwright is not installed")
class BrowserDriverTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.executable = os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium")
        if not cls.executable:
            raise unittest.SkipTest("Chromium executable is unavailable; real-browser checks were not run")
        cls.external_server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.external_server.visited = []
        cls.external_server.posted = []
        cls.external_origin = f"http://127.0.0.1:{cls.external_server.server_port}"
        cls.external_server.external_origin = cls.external_origin
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
        cls.server.visited = []
        cls.server.posted = []
        cls.server.external_origin = cls.external_origin
        cls.origin = f"http://127.0.0.1:{cls.server.server_port}"
        cls.threads = []
        for server in (cls.server, cls.external_server):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            cls.threads.append(thread)

    @classmethod
    def tearDownClass(cls):
        for server in (cls.server, cls.external_server):
            server.shutdown()
            server.server_close()
        for thread in cls.threads:
            thread.join(timeout=2)

    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.artifacts = Path(self.temporary.name)
        self.session = BrowserSession(
            self.origin + "/sandbox/", self.artifacts,
            allowed_origins=[self.origin],
            allowed_path_prefixes={self.origin: ["/sandbox/"]},
            executable_path=self.executable,
        )
        self.observation = await self.session.start()

    async def asyncTearDown(self):
        await self.session.close()
        self.temporary.cleanup()

    def _ref(self, observation, name):
        matches = [item for item in observation["elements"] if item.get("name", "").strip() == name]
        self.assertEqual(len(matches), 1, f"Expected one visible interactive element named {name!r}, got {matches!r}")
        return matches[0]["ref"]

    async def test_real_dom_observation_and_form_actions(self):
        observed = self.observation
        self.assertEqual(observed["title"], "Browser fixture catalog")
        self.assertEqual(observed["url"], self.origin + "/sandbox/")
        self.assertTrue(observed["observation_id"])
        self.assertIn("Waiting for a choice.", observed["text"])
        self.assertNotIn("hidden-secret-value", observed["text"])
        self.assertNotIn("hidden-secret-text", observed["text"])
        observed = await self.session.act({"type": "fill", "ref": self._ref(observed, "Search products"), "text": "camera"})
        observed = await self.session.act({"type": "select", "ref": self._ref(observed, "Colour"), "value": "blue"})
        observed = await self.session.act({"type": "click", "ref": self._ref(observed, "Apply filters")})
        self.assertIn("Chosen camera blue", observed["text"])
        self.assertEqual(await self.session.page.locator("#query").input_value(), "camera")
        self.assertEqual(await self.session.page.locator("#colour").input_value(), "blue")
        await self.session.page.evaluate("() => { const p=document.createElement('p');p.textContent='Visible evidence. '.repeat(2000);document.body.appendChild(p); }")
        bounded = await self.session.observe()
        self.assertTrue(bounded["truncated"])
        self.assertLessEqual(len(bounded["text"]), 12500)

    async def test_stale_refs_and_extra_action_fields_are_rejected(self):
        stale = self._ref(self.observation, "Search products")
        fresh = await self.session.observe()
        self.assertNotEqual(fresh["observation_id"], self.observation["observation_id"])
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "fill", "ref": stale, "text": "stale"})
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "click", "ref": self._ref(fresh, "Apply filters"), "javascript": "alert(1)"})
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "evaluate", "script": "document.body.innerHTML=''"})
        self.assertEqual(await self.session.page.locator("#query").input_value(), "")

    async def test_delayed_updates_settle_without_repeating_the_click(self):
        obs = await self.session.act({"type": "click", "ref": self._ref(self.observation, "Delayed update")})
        self.assertIn("Delayed update complete", obs["text"])
        self.assertFalse(obs["loading"])
        self.assertEqual(await self.session.page.locator("#delayed").get_attribute("data-clicks"), "1")
        await self.session.page.locator("body").evaluate("node => node.setAttribute('aria-busy', 'true')")
        busy = await self.session.observe()
        self.assertTrue(busy["loading"])
        self.assertEqual(await self.session.page.locator("#delayed").get_attribute("data-clicks"), "1")

    async def test_reference_cannot_be_reassigned_to_a_replacement_dom_node(self):
        reference = self._ref(self.observation, "Apply filters")
        await self.session.page.evaluate("""reference => {
          const replacement = document.createElement('button');
          replacement.textContent = 'Apply filters';
          replacement.setAttribute('data-shoptrace-ref', reference);
          replacement.onclick = () => document.body.setAttribute('data-wrong-target', 'executed');
          document.querySelector('#apply').replaceWith(replacement);
        }""", reference)
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "click", "ref": reference})
        self.assertIsNone(await self.session.page.locator("body").get_attribute("data-wrong-target"))

    async def test_cancelling_an_inflight_action_propagates_and_closes_the_page(self):
        page = self.session.page
        action = asyncio.create_task(self.session.act({"type": "wait", "milliseconds": 2000}))
        await asyncio.sleep(0.05)
        self.assertFalse(action.done())
        action.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await action
        self.assertTrue(page.is_closed())

    async def test_disabled_and_invisible_targets_cannot_be_acted_on(self):
        disabled = [item for item in self.observation["elements"] if item.get("name") == "Disabled action"]
        if disabled:
            self.assertTrue(disabled[0].get("disabled"))
            with self.assertRaises(BrowserError):
                await self.session.act({"type": "click", "ref": disabled[0]["ref"]})
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "click", "ref": "not-an-observed-reference"})
        serialized = str(self.observation["elements"])
        self.assertNotIn("hidden-secret-value", serialized)

    async def test_out_of_scope_origin_and_path_are_blocked_before_network(self):
        external_before = len(self.external_server.visited)
        path_before = self.server.visited.count("/outside")
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "click", "ref": self._ref(self.observation, "Leave allowed origin")})
        observed = await self.session.observe()
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "click", "ref": self._ref(observed, "Leave allowed path")})
        self.assertEqual(len(self.external_server.visited), external_before)
        self.assertEqual(self.server.visited.count("/outside"), path_before)
        self.assertTrue(self.session.page.url.startswith(self.origin + "/sandbox/"))

    async def test_navigation_back_scroll_and_bounded_wait(self):
        observed = await self.session.act({"type": "click", "ref": self._ref(self.observation, "Open details")})
        self.assertEqual(observed["title"], "Details")
        self.assertIn("Visible detail evidence.", observed["text"])
        observed = await self.session.act({"type": "back"})
        self.assertEqual(observed["title"], "Browser fixture catalog")
        await self.session.act({"type": "scroll", "direction": "down"})
        self.assertGreater(await self.session.page.evaluate("window.scrollY"), 0)
        await self.session.act({"type": "wait", "milliseconds": 1})
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "wait", "milliseconds": 2001})
        with self.assertRaises(BrowserError):
            await self.session.act({"type": "press", "ref": self._ref(await self.session.observe(), "Search products"), "key": "Meta+O"})

    async def test_page_scripts_frames_and_websockets_cannot_contact_other_origins(self):
        external_before = len(self.external_server.visited)
        try:
            await self.session.act({"type": "click", "ref": self._ref(self.observation, "Embed outside resources")})
        except BrowserError:
            pass
        await asyncio.sleep(0.15)
        self.assertEqual(len(self.external_server.visited), external_before)
        self.assertTrue(self.session.page.url.startswith(self.origin + "/sandbox/"))

    async def test_read_only_mode_blocks_same_origin_posts_before_the_server(self):
        before = len(self.server.posted)
        await self.session.page.evaluate("async () => { await fetch('/sandbox/write', {method:'POST'}); }")
        self.assertEqual(len(self.server.posted), before + 1)
        await self.session.close()
        self.session = BrowserSession(
            self.origin + "/sandbox/", self.artifacts / "read-only",
            allowed_origins=[self.origin],
            allowed_path_prefixes={self.origin: ["/sandbox/"]},
            executable_path=self.executable, read_only=True,
        )
        observation = await self.session.start()
        try:
            await self.session.act({"type": "click", "ref": self._ref(observation, "Attempt write")})
        except BrowserError:
            pass
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.server.posted), before + 1)
        self.assertTrue(self.session.page.url.startswith(self.origin + "/sandbox/"))

    async def test_screenshots_are_local_and_browser_closes_cleanly(self):
        relative = await self.session.screenshot("audit.png")
        self.assertFalse(Path(relative).is_absolute())
        path = self.artifacts / relative
        self.assertTrue(path.is_file())
        self.assertEqual(path.read_bytes()[:8], b"\x89PNG\r\n\x1a\n")
        with self.assertRaises(BrowserError):
            await self.session.screenshot("../escaped.png")
        old_page = self.session.page
        await self.session.close()
        self.assertTrue(old_page.is_closed())
        await self.session.close()
        with self.assertRaises(BrowserError):
            await self.session.observe()

    async def test_popups_and_downloads_do_not_escape_the_session(self):
        try:
            await self.session.act({"type": "click", "ref": self._ref(self.observation, "Open popup")})
        except BrowserError:
            pass
        await asyncio.sleep(0.05)
        self.assertEqual(len(self.session.page.context.pages), 1)
        observed = await self.session.observe()
        try:
            await self.session.act({"type": "click", "ref": self._ref(observed, "Download fixture")})
        except BrowserError:
            pass
        await asyncio.sleep(0.05)
        self.assertFalse(any(path.name == "should-not-save.txt" for path in self.artifacts.rglob("*")))
        self.assertTrue(self.session.page.url.startswith(self.origin + "/sandbox/"))


class BrowserStartupDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_goto_failures_report_safe_diagnostics_and_close_resources(self):
        cases = [
            ("net::ERR_CERT_AUTHORITY_INVALID", "Chromium to trust the session proxy CA"),
            ("net::ERR_CERT_DATE_INVALID", "TLS certificate validation failed"),
            ("net::ERR_TUNNEL_CONNECTION_FAILED", "proxy tunnel failed"),
            ("unexpected provider startup failure", "Browser startup failed"),
        ]
        for error, expected in cases:
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temporary:
                private_url = "https://private.invalid/path?token=not-for-output"
                page = MagicMock()
                page.goto = AsyncMock(side_effect=RuntimeError(error + " at " + private_url))
                context = MagicMock()
                context.route = AsyncMock()
                context.route_web_socket = AsyncMock()
                context.new_page = AsyncMock(return_value=page)
                context.close = AsyncMock()
                browser = MagicMock()
                browser.new_context = AsyncMock(return_value=context)
                browser.close = AsyncMock()
                playwright = MagicMock()
                playwright.chromium.launch = AsyncMock(return_value=browser)
                playwright.stop = AsyncMock()
                factory = MagicMock()
                factory.start = AsyncMock(return_value=playwright)
                package = types.ModuleType("playwright")
                package.__path__ = []
                async_api = types.ModuleType("playwright.async_api")
                async_api.async_playwright = MagicMock(return_value=factory)
                session = BrowserSession("https://example.invalid/", Path(temporary),
                    allowed_origins=["https://example.invalid"])
                with patch.dict("sys.modules", {"playwright": package, "playwright.async_api": async_api}):
                    with self.assertRaises(BrowserError) as failure:
                        await session.start()
                self.assertIn(expected, str(failure.exception))
                self.assertNotIn(private_url, str(failure.exception))
                self.assertNotIn("not-for-output", str(failure.exception))
                context.close.assert_awaited_once()
                browser.close.assert_awaited_once()
                playwright.stop.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
