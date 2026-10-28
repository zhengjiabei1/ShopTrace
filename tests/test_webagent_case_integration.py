"""Case API and real Chromium integration checks, without a model provider."""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.request import urlopen

from fastapi import FastAPI
from fastapi.testclient import TestClient

from webagent import cases, tasks
from webagent.app import create_app
from webagent.sandbox import router


def matching_product(case_id):
    private = cases.private_case(case_id)
    for product in private['catalog']['products']:
        for variant in tasks._variants(product):
            if tasks._matches(variant, private['goal']):
                return product, variant
    raise AssertionError('Test case must contain a matching product')


def cart_state(variant):
    return {'url': 'http://localhost/sandbox/cart', 'cart_count': 1,
            'cart': [{'product_id': variant['product_id'], 'color': variant.get('color', ''),
                      'capacity': f"{variant['capacity_ml']}ml" if variant.get('capacity_ml') else '',
                      'price': str(variant['price']), 'text': 'Observed cart record'}]}


class CaseAPIIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.app = create_app(Path(self.directory.name))

    def tearDown(self):
        self.directory.cleanup()

    def test_case_catalog_cookie_is_client_scoped_and_survives_detail_and_cart(self):
        with TestClient(self.app) as selected, TestClient(self.app) as untouched:
            response = selected.get('/sandbox/?case=case-b8d1')
            self.assertEqual(response.status_code, 200)
            cookie_header = response.headers['set-cookie']
            self.assertIn('HttpOnly', cookie_header)
            self.assertIn('Path=/sandbox', cookie_header)
            self.assertIn('SameSite=strict', cookie_header)
            expected = cases.get_catalog('case-b8d1')
            for path in ('/sandbox/', '/sandbox/product?id=r284', '/sandbox/cart'):
                self.assertEqual(selected.get(path).status_code, 200)
                catalog = selected.get('/sandbox/assets/catalog.json')
                self.assertEqual(catalog.json(), expected)
                self.assertEqual(catalog.headers['cache-control'], 'no-store')
                self.assertEqual(catalog.headers['vary'], 'Cookie')
            self.assertEqual(len(untouched.get('/sandbox/assets/catalog.json').json()['products']), 12)
            self.assertEqual(selected.get('/sandbox/?case=legacy').status_code, 200)
            self.assertEqual(len(selected.get('/sandbox/assets/catalog.json').json()['products']), 12)

    def test_unknown_case_and_private_dataset_paths_are_rejected_without_legacy_fallback(self):
        with TestClient(self.app) as client:
            self.assertEqual(client.get('/sandbox/?case=case-0000').status_code, 404)
            self.assertEqual(client.get('/sandbox/product?case=../../tasks.py').status_code, 404)
            client.cookies.set('shoptrace_case', 'case-0000', path='/sandbox')
            self.assertEqual(client.get('/sandbox/assets/catalog.json').status_code, 404)
            for path in ('/sandbox/assets/cases-v1.json', '/sandbox/assets/cases.py', '/sandbox/assets/tasks.py'):
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post('/api/runs', json={'task_id': 'case-0000', 'provider': 'api'}).status_code, 422)
            self.assertEqual(client.post('/api/runs', json={'task_id': 'case-b8d1', 'provider': 'browser_smoke'}).status_code, 422)

    def test_task_metadata_preserves_legacy_six_and_public_case_records_exclude_private_keys(self):
        with TestClient(self.app) as client:
            self.assertEqual(len(client.get('/api/tasks').json()['tasks']), 6)
            for split in ('development', 'heldout'):
                response = client.get('/api/cases', params={'split': split})
                self.assertEqual(response.status_code, 200)
                self.assertEqual(len(response.json()['cases']), 6)
                for item in response.json()['cases']:
                    self.assertEqual(set(item), {'id', 'title', 'query', 'difficulty', 'start_path'})
                for private in ('"goal"', '"catalog"', 'unknown_field', '"split"', 'expected_product'):
                    self.assertNotIn(private, response.text)
            self.assertEqual(len(client.get('/api/cases').json()['cases']), 12)
            self.assertEqual(client.get('/api/cases?split=training').status_code, 422)

    def test_api_run_records_case_fingerprints_without_sending_goals_to_runtime_query(self):
        captured = []

        async def no_model(runtime, run_id, spec, cancel):
            captured.append(spec)
            runtime.store.update_run(run_id, status='completed', termination_reason='test_no_model', result={'success': None})

        with patch('webagent.app.BrowserRuntime.run', new=no_model), patch.dict('os.environ', {
                'LLM_BASE_URL': 'https://api.deepseek.com/v1', 'LLM_MODEL': 'test-config-only'}):
            with TestClient(self.app) as client:
                response = client.post('/api/runs', json={'task_id': 'case-b8d1', 'provider': 'api'})
                self.assertEqual(response.status_code, 201)
                record = response.json()
                self.assertEqual(record['query'], cases.get_case_public('case-b8d1')['query'])
                self.assertEqual(record['browser']['start_url'], 'http://testserver/sandbox/?case=case-b8d1')
                self.assertEqual(record['benchmark_case']['catalog_fingerprint'], cases.catalog_fingerprint('case-b8d1'))
                self.assertEqual(record['benchmark_case']['dataset_fingerprint'], cases.dataset_fingerprint())
                self.assertNotIn('goal', record)
                self.assertNotIn('catalog', record)
        self.assertEqual(len(captured), 1)
        self.assertNotIn('goal', captured[0])
        self.assertNotIn('catalog', captured[0])

    def test_grading_uses_selected_case_catalog_and_rejects_foreign_product_ids(self):
        case = cases.private_case('case-b8d1')
        _, variant = matching_product('case-b8d1')
        state = cart_state(variant)
        self.assertTrue(tasks._grade_snapshot(case, state, {'reason': 'completed'}, catalog=case['catalog']['products'])['success'])
        self.assertFalse(tasks._grade_snapshot(case, state, {'reason': 'completed'}, catalog=cases.get_catalog('case-31f9')['products'])['success'])
        self.assertFalse(tasks._grade_snapshot(case, state, {'reason': 'completed'})['success'])

        class SavedPage:
            async def evaluate(self, script):
                return state

        self.assertTrue(asyncio.run(tasks.evaluate_task(SavedPage(), 'case-b8d1', {'reason': 'completed'}))['success'])
        self.assertFalse(asyncio.run(tasks.evaluate_task(SavedPage(), 'case-31f9', {'reason': 'completed'}))['success'])


CHROME = os.environ.get('WEBAGENT_CHROMIUM_PATH') or shutil.which('chromium') or shutil.which('chromium-browser')


@unittest.skipUnless(importlib.util.find_spec('playwright') and CHROME,
                     'Requires installed Playwright and Chromium; no dependencies are downloaded')
class CaseChromiumIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import uvicorn
        app = FastAPI()
        app.include_router(router)
        try:
            cls.socket = socket.socket()
            cls.socket.bind(('127.0.0.1', 0))
        except OSError as exc:
            raise unittest.SkipTest('Loopback socket unavailable') from exc
        cls.base = f'http://127.0.0.1:{cls.socket.getsockname()[1]}'
        cls.server = uvicorn.Server(uvicorn.Config(app, log_level='critical', access_log=False))
        cls.thread = threading.Thread(target=cls.server.run, kwargs={'sockets': [cls.socket]}, daemon=True)
        cls.thread.start()
        for _ in range(100):
            try:
                with urlopen(cls.base + '/sandbox/', timeout=.2) as response:
                    if response.status == 200:
                        return
            except OSError:
                time.sleep(.02)
        raise RuntimeError('Case sandbox test server did not start')

    @classmethod
    def tearDownClass(cls):
        cls.server.should_exit = True
        cls.thread.join(timeout=5)
        cls.socket.close()

    def scenario(self, action):
        async def run():
            from playwright.async_api import async_playwright
            async with async_playwright() as playwright:
                browser = await playwright.chromium.launch(executable_path=CHROME, headless=True,
                                                           args=['--no-sandbox', '--disable-dev-shm-usage'])
                try:
                    await action(browser)
                finally:
                    await browser.close()
        asyncio.run(run())

    def test_separate_contexts_keep_catalog_and_cart_isolated_across_navigation(self):
        async def action(browser):
            left, right = await browser.new_context(), await browser.new_context()
            try:
                a, b = await left.new_page(), await right.new_page()
                await a.goto(self.base + '/sandbox/?case=case-b8d1')
                await b.goto(self.base + '/sandbox/?case=case-31f9')
                await a.wait_for_selector('#results')
                await b.wait_for_selector('#results')
                for page, case_id in ((a, 'case-b8d1'), (b, 'case-31f9')):
                    product, variant = matching_product(case_id)
                    await page.goto(self.base + '/sandbox/product?id=' + product['id'])
                    await page.wait_for_selector('#product-details')
                    await page.get_by_label('Color', exact=True).select_option(variant['color'])
                    await page.get_by_label('Capacity', exact=True).select_option(str(variant['capacity_ml']) + 'ml')
                    self.assertEqual(await page.locator('#variant-price').inner_text(), f"CNY {variant['price']:.2f}")
                    await page.get_by_role('button', name='Add to list', exact=True).click()
                    await page.get_by_role('link', name='View list', exact=True).click()
                    await page.wait_for_selector('.cart-item')
                    self.assertTrue((await tasks.evaluate_task(page, case_id, {'reason': 'completed'}))['success'])
                self.assertIn('Sample Rainfall Thermos', await a.locator('#cart-items').inner_text())
                self.assertNotIn('Sample Clear Lake Thermos', await a.locator('#cart-items').inner_text())
                self.assertIn('Sample Clear Lake Thermos', await b.locator('#cart-items').inner_text())
                self.assertFalse((await tasks.evaluate_task(a, 'case-31f9', {'reason': 'completed'}))['success'])
                cookies = await left.cookies()
                case_cookie = next(c for c in cookies if c['name'] == 'shoptrace_case')
                self.assertTrue(case_cookie['httpOnly'])
                self.assertEqual(case_cookie['path'], '/sandbox')
            finally:
                await left.close()
                await right.close()
        self.scenario(action)

    def test_unknown_case_detail_grading_and_case_switch_do_not_reuse_cached_catalog(self):
        async def action(browser):
            context = await browser.new_context()
            try:
                page = await context.new_page()
                await page.goto(self.base + '/sandbox/')
                await page.wait_for_selector('#results')
                self.assertEqual(await page.locator('#results').get_attribute('data-total-results'), '12')
                await page.goto(self.base + '/sandbox/?case=case-90ce')
                await page.wait_for_selector('#results')
                self.assertEqual(await page.locator('#results').get_attribute('data-total-results'), '8')
                case = cases.private_case('case-90ce')
                known = {k: v for k, v in case['goal'].items() if k != case['unknown_field']}
                product = next(p for p in case['catalog']['products'] if tasks._matches(tasks._variant(p), known))
                await page.goto(self.base + '/sandbox/product?id=' + product['id'])
                await page.wait_for_selector('#product-details')
                self.assertIn('Not provided', await page.locator('#product-details').inner_text())
                self.assertTrue((await tasks.evaluate_task(page, case['id'], {'reason': 'blocked'}))['success'])
                self.assertFalse((await tasks.evaluate_task(page, 'case-0b65', {'reason': 'blocked'}))['success'])
                await page.goto(self.base + '/sandbox/?case=legacy')
                await page.wait_for_selector('#results')
                self.assertEqual(await page.locator('#results').get_attribute('data-total-results'), '12')
                self.assertNotIn('shoptrace_case', {c['name'] for c in await context.cookies()})
            finally:
                await context.close()
        self.scenario(action)


if __name__ == '__main__':
    unittest.main()
