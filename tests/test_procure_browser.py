"""Chromium checks for procurement pages and failure recovery."""
import asyncio
import importlib.util
import os
import shutil
import socket
import threading
import time
import unittest
from urllib.request import urlopen

from fastapi import FastAPI

from webagent import procure as proc


CHROME = os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")


@unittest.skipUnless(importlib.util.find_spec("playwright") and CHROME, "Requires Chromium and Playwright")
class ProcurementBrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        app = FastAPI()
        app.include_router(proc.router)
        cls.sock = socket.socket()
        cls.sock.bind(("127.0.0.1", 0))
        cls.base = f"http://127.0.0.1:{cls.sock.getsockname()[1]}"
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level="critical", access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.sock]}, daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                with urlopen(cls.base + proc.PREFIX + "/assets/app.js", timeout=.2) as resp:
                    if resp.status == 200:
                        return
            except OSError:
                time.sleep(.02)
        raise RuntimeError("Procurement test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.sock.close()

    def run_browser(self, fn):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as p:
                browser = await p.chromium.launch(executable_path=CHROME, headless=True,
                                                 args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 900})
                    errors = []
                    page.on("pageerror", lambda err: errors.append(str(err)))
                    await fn(page)
                    self.assertEqual(errors, [])
                finally:
                    await browser.close()
        asyncio.run(run())

    async def open(self, page, fault="none"):
        case = proc.new_case("procure-dev-01", 7, fault)
        await page.goto(self.base + case["start_path"])
        await page.wait_for_selector("#results")
        self.assertNotIn("sid=", page.url)
        self.assertNotIn(proc.COOKIE, await page.evaluate("document.cookie"))
        return case

    async def detail(self, page, pid):
        await page.goto(self.base + proc.PREFIX + "/product?id=" + pid)
        await page.wait_for_selector("#product-details")

    async def add(self, page):
        await page.get_by_role("button", name="Add to list", exact=True).click()
        await page.wait_for_selector('#content[aria-busy="false"]')

    async def cart(self, page):
        await page.get_by_role("link", name="View list", exact=True).click()
        await page.wait_for_selector("#proc-cart")

    def test_category_pagination_and_return_keep_filters_without_revealing_attributes(self):
        async def scenario(page):
            await self.open(page)
            await page.get_by_label("Category", exact=True).select_option("keyboard")
            await page.get_by_role("button", name="Search", exact=True).click()
            await page.wait_for_selector("#results[data-total-results='8']")
            self.assertEqual(await page.locator(".product-card").count(), 4)
            self.assertNotIn("Wireless", await page.locator("#results").inner_text())
            await page.get_by_role("link", name="Next page", exact=True).click()
            await page.wait_for_url("**page=2")
            await page.locator(".product-title").first.click()
            await page.wait_for_selector("#product-details")
            self.assertIn("Connection", await page.locator(".facts").inner_text())
            await page.get_by_role("link", name="Back to results", exact=False).click()
            await page.wait_for_selector("#results")
            self.assertIn("page=2", page.url)
            self.assertEqual(await page.get_by_label("Category", exact=True).input_value(), "keyboard")
            for width in (390, 768, 1440):
                await page.set_viewport_size({"width": width, "height": 900})
                self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.run_browser(scenario)

    def test_lost_feedback_keeps_badge_uncertain_and_duplicate_can_be_fixed(self):
        async def scenario(page):
            case = await self.open(page, "ack_lost")
            await self.detail(page, "pk01")
            await self.add(page)
            self.assertEqual(await page.locator("#cart-count").inner_text(), "0")
            self.assertIn("not confirmed", await page.locator("#add-status").inner_text())
            await self.add(page)
            await self.cart(page)
            self.assertEqual(await page.locator(".quantity strong").inner_text(), "2")
            await page.get_by_role("button", name="Decrease Atlas Keyboard quantity", exact=True).click()
            await page.wait_for_function("document.querySelector('.quantity strong')?.textContent === '1'")
            await self.detail(page, "pm01")
            await self.add(page)
            await self.cart(page)
            result = await proc.evaluate(page, "procure-dev-01", {"reason": "completed"})
            self.assertTrue(result["success"], result)
            self.assertEqual(result["metrics"]["duplicates"], 0)
            self.assertEqual(result["metrics"]["duplicate_peak"], 1)
            self.assertEqual(proc.audit(case["start_path"], "procure-dev-01")["metrics"], result["metrics"])
            await page.evaluate("document.querySelector('.proc-item').dataset.qty = '2'")
            self.assertFalse((await proc.evaluate(page, "procure-dev-01", {"reason": "completed"}))["success"])
        self.run_browser(scenario)

    def test_no_effect_requires_an_explicit_retry_and_stockout_allows_alternative(self):
        async def scenario(page):
            for fault, first, final in (("no_effect", "pk01", "pk01"), ("stockout", "pk01", "pk07")):
                await self.open(page, fault)
                await self.detail(page, first)
                await self.add(page)
                if fault == "stockout":
                    self.assertTrue(await page.get_by_role("button", name="Add to list", exact=True).is_disabled())
                await self.cart(page)
                self.assertEqual(await page.locator("#cart-count").inner_text(), "0")
                await self.detail(page, final)
                await self.add(page)
                await self.detail(page, "pm01")
                await self.add(page)
                await self.cart(page)
                result = await proc.evaluate(page, "procure-dev-01", {"reason": "completed"})
                self.assertTrue(result["success"], result)
                self.assertTrue(result["metrics"]["fault_triggered"])
                self.assertEqual(result["metrics"]["duplicates"], 0)
                self.assertIsNone((await proc.evaluate(page, "procure-dev-02", {"reason": "completed"}))["success"])
        self.run_browser(scenario)


if __name__ == "__main__":
    unittest.main()
