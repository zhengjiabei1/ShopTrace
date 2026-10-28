"""Sandbox page interactions and grading boundaries, without a model service."""
import asyncio
import importlib.util
import json
import os
import shutil
import socket
import threading
import time
import unittest
from urllib.request import urlopen

from fastapi import FastAPI
from fastapi.testclient import TestClient

from webagent.sandbox import router
from webagent import tasks


class SandboxContractTests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)

    def test_public_pages_and_task_metadata_do_not_expose_grading_keys(self):
        public = tasks.list_tasks()
        self.assertEqual(len(public), 6)
        for item in public:
            self.assertEqual(set(item), {"id", "title", "query", "difficulty", "start_path"})
        for path in ("/sandbox/", "/sandbox/product?id=t-bay", "/sandbox/cart"):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertIn("Sample store", response.text)
        catalog = self.client.get("/sandbox/assets/catalog.json")
        self.assertEqual(len(catalog.json()["products"]), 12)
        for forbidden in ("gold", "expected_product", "browser-headphones-commute", "unknown_field"):
            self.assertNotIn(forbidden, catalog.text)
        self.assertEqual(self.client.get("/sandbox/assets/tasks.py").status_code, 404)

    def cart_state(self, product_id, color, capacity, price):
        return {"url": "http://localhost/sandbox/cart", "cart_count": 1,
                "cart": [{"product_id": product_id, "color": color, "capacity": capacity,
                          "price": str(price), "text": "Visible cart item"}]}

    def test_final_variant_price_and_selected_capacity_are_independently_checked(self):
        task = tasks._private_task("browser-thermos-variant")
        wrong = tasks._grade_snapshot(task, self.cart_state("t-bay", "Blue", "450ml", 79), {"reason": "completed"})
        self.assertFalse(wrong["success"])
        right = tasks._grade_snapshot(task, self.cart_state("t-bay", "Blue", "500ml", 99), {"reason": "completed"})
        self.assertTrue(right["success"])
        false_price = tasks._grade_snapshot(task, self.cart_state("t-bay", "Blue", "500ml", 79), {"reason": "completed"})
        self.assertFalse(false_price["success"])

    def test_actor_claim_does_not_override_wrong_item_or_unfinished_page(self):
        task = tasks._private_task("browser-headphones-commute")
        result = tasks._grade_snapshot(task, self.cart_state("h-cloud", "Gray", "", 269),
                                       {"reason": "completed", "success": True, "evidence": ["All requirements met"]})
        self.assertFalse(result["success"])
        state = self.cart_state("h-commute", "Black", "", 289)
        state["url"] = "http://localhost/sandbox/product?id=h-commute"
        self.assertFalse(tasks._grade_snapshot(task, state, {"reason": "completed"})["success"])

    def test_unknown_and_no_match_require_visible_browser_evidence(self):
        unknown = tasks._private_task("browser-headphones-unknown")
        state = {"url": "http://localhost/sandbox/", "cart_count": 0, "cart": []}
        self.assertFalse(tasks._grade_snapshot(unknown, state, {"reason": "blocked"})["success"])
        state.update(url="http://localhost/sandbox/product?id=h-cloud", detail={"product_id": "h-cloud", "text": "Connection Not provided"})
        self.assertTrue(tasks._grade_snapshot(unknown, state, {"reason": "blocked"})["success"])
        no_match = tasks._private_task("browser-thermos-budget")
        state = {"url": "http://localhost/sandbox/", "cart_count": 0, "cart": [],
                 "results": {"total": 0, "query": "Thermos", "category": "", "max_price": "30", "text": "0 products found"}}
        self.assertTrue(tasks._grade_snapshot(no_match, state, {"reason": "no_suitable_product"})["success"])
        state["results"]["max_price"] = ""
        self.assertFalse(tasks._grade_snapshot(no_match, state, {"reason": "no_suitable_product"})["success"])


CHROME = os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")


@unittest.skipUnless(importlib.util.find_spec("playwright") and CHROME,
                     "Requires Playwright and a local Chromium binary; no browser is installed by tests")
class RealBrowserSandboxTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        cls.app = FastAPI()
        cls.app.include_router(router)
        try:
            cls.socket = socket.socket()
            cls.socket.bind(("127.0.0.1", 0))
        except OSError as exc:
            raise unittest.SkipTest("Local sockets unavailable for real browser checks") from exc
        cls.port = cls.socket.getsockname()[1]
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.server = uvicorn.Server(uvicorn.Config(cls.app, log_level="critical", access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={"sockets": [cls.socket]}, daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                with urlopen(cls.base + "/sandbox/", timeout=.2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(.02)
        raise RuntimeError("Sandbox test server did not start")

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.socket.close()

    def run_browser(self, function):
        async def scenario():
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
                                                           args=["--no-sandbox", "--disable-dev-shm-usage"])
                try:
                    page = await browser.new_page(viewport={"width": 1280, "height": 900})
                    await function(page)
                finally:
                    await browser.close()
        asyncio.run(scenario())

    def test_real_search_pagination_detail_and_cart_complete_task(self):
        async def scenario(page):
            await page.goto(self.base + "/sandbox/")
            await page.get_by_label("Search products", exact=True).fill("Headphones")
            await page.get_by_role("button", name="Search", exact=True).click()
            await page.wait_for_selector("#results")
            self.assertEqual(await page.locator("#results").get_attribute("data-total-results"), "4")
            await page.get_by_role("link", name="Next page", exact=True).click()
            await page.get_by_role("link", name="Sample Commute Headphones", exact=True).click()
            await page.get_by_role("button", name="Add to list", exact=True).click()
            await page.get_by_role("link", name="View list", exact=True).click()
            await page.wait_for_selector(".cart-item")
            result = await tasks.evaluate_task(page, "browser-headphones-commute", {"reason": "completed"})
            self.assertTrue(result["success"], result)
            self.assertEqual(result["evidence"][0]["product_id"], "h-commute")
        self.run_browser(scenario)

    def test_real_variant_selection_updates_price_and_mobile_layout(self):
        async def scenario(page):
            await page.set_viewport_size({"width": 390, "height": 844})
            await page.goto(self.base + "/sandbox/product?id=t-bay")
            await page.wait_for_selector("#variant-price")
            self.assertEqual(await page.locator("#variant-price").inner_text(), "CNY 79.00")
            await page.get_by_label("Capacity", exact=True).select_option("500ml")
            self.assertEqual(await page.locator("#variant-price").inner_text(), "CNY 99.00")
            await page.get_by_role("button", name="Add to list", exact=True).click()
            await page.get_by_role("link", name="View list", exact=True).click()
            await page.wait_for_selector(".cart-item")
            self.assertTrue((await tasks.evaluate_task(page, "browser-thermos-variant", {"reason": "completed"}))["success"])
            self.assertTrue(await page.evaluate("document.documentElement.scrollWidth <= innerWidth"))
        self.run_browser(scenario)

    def test_real_missing_information_and_empty_filtered_results(self):
        async def scenario(page):
            await page.goto(self.base + "/sandbox/product?id=h-cloud")
            await page.wait_for_selector("#product-details")
            self.assertTrue((await tasks.evaluate_task(page, "browser-headphones-unknown", {"reason": "blocked"}))["success"])
            await page.goto(self.base + "/sandbox/")
            await page.get_by_label("Category", exact=True).select_option("thermos")
            await page.get_by_label("Maximum price (CNY)", exact=True).fill("30")
            await page.get_by_role("button", name="Search", exact=True).click()
            await page.wait_for_selector("#results")
            self.assertTrue((await tasks.evaluate_task(page, "browser-thermos-budget", {"reason": "no_suitable_product"}))["success"])
        self.run_browser(scenario)

    def test_old_cart_colors_migrate_only_to_available_current_variants(self):
        async def scenario(page):
            await page.goto(self.base + "/sandbox/")
            await page.wait_for_selector("#results")
            await page.evaluate(r"""() => localStorage.setItem('shoptrace.browser.sandbox.cart.v1', JSON.stringify([
                {productId:'t-bay',selections:{color:'\u84dd\u8272',capacity:'500ml'}},
                {productId:'t-bay',selections:{color:'unlisted-color',capacity:'500ml'}},
                {productId:'t-bay',selections:{color:'\u7eff\u8272',capacity:'500ml'}},
                {productId:'t-bay',selections:{color:'\u84dd\u8272',capacity:'900ml'}},
                {productId:'unknown-product',selections:{color:'\u84dd\u8272',capacity:'500ml'}},
                {productId:'r284',selections:{color:'\u7eff\u8272',capacity:'600ml'}}
            ]))""")
            await page.goto(self.base + "/sandbox/cart")
            await page.wait_for_selector(".cart-item")
            self.assertEqual(await page.locator(".cart-item").count(), 1)
            row = page.locator(".cart-item")
            self.assertEqual(await row.get_attribute("data-variant-color"), "Blue")
            self.assertEqual(await row.get_attribute("data-variant-capacity"), "500ml")
            self.assertEqual(await row.get_attribute("data-unit-price"), "99")
            self.assertIn("CNY 99.00", await row.inner_text())
            result = await tasks.evaluate_task(page, "browser-thermos-variant", {"reason": "completed"})
            self.assertTrue(result["success"], result)
            await page.goto(self.base + "/sandbox/?case=case-31f9")
            await page.wait_for_selector("#results")
            await page.goto(self.base + "/sandbox/cart")
            await page.get_by_role("heading", name="Your shopping list is empty", exact=True).wait_for()
            self.assertEqual(await page.locator(".cart-item").count(), 0)
            self.assertEqual(await page.locator("#cart-count").inner_text(), "0")
        self.run_browser(scenario)
