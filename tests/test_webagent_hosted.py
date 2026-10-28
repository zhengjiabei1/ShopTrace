"""Boundary tests for explicit single-owner hosted mode; no model calls."""
import asyncio
import base64
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from webagent.hosted import HostedConfig, HostedGateway, SandboxListener, create_hosted_app, serve

PASSWORD = 'test-only-strong-password'
CONFIG = HostedConfig('https://shoptrace.example', 'owner', hashlib.sha256(PASSWORD.encode()).hexdigest())
AUTH = 'Basic ' + base64.b64encode(('owner:' + PASSWORD).encode()).decode()
HEADERS = {'host': 'shoptrace.example', 'authorization': AUTH}


class HostedConfigTests(unittest.TestCase):
    def test_configuration_fails_closed_and_does_not_reuse_model_credential(self):
        for origin in ['', 'http://shoptrace.example', 'https://shoptrace.example/path',
                       'https://owner:password@shoptrace.example', 'https://localhost']:
            with self.subTest(origin=origin), self.assertRaises(ValueError):
                HostedConfig(origin, 'owner', CONFIG.password_sha256)
        for digest in ['', PASSWORD, 'x' * 64]:
            with self.subTest(digest=digest), self.assertRaises(ValueError):
                HostedConfig(CONFIG.public_origin, 'owner', digest)
        with patch.dict(os.environ, {'WEBAGENT_PUBLIC_ORIGIN': CONFIG.public_origin,
                                      'WEBAGENT_AUTH_USER': 'owner',
                                      'WEBAGENT_AUTH_PASSWORD_SHA256': CONFIG.password_sha256,
                                      'LLM_API_KEY': PASSWORD}):
            with self.assertRaisesRegex(ValueError, 'different'):
                HostedConfig.from_environment()


class HostedBoundaryTests(unittest.TestCase):
    def make_inner(self):
        inner = FastAPI()

        @inner.api_route('/echo', methods=['GET', 'POST'])
        async def echo(request: Request):
            return JSONResponse({'url': str(request.url), 'headers': dict(request.headers),
                                 'scheme': request.scope['scheme']})
        return inner

    def test_public_gate_requires_credentials_even_for_loopback_and_ignores_forwarded_headers(self):
        client = TestClient(HostedGateway(self.make_inner(), CONFIG))
        for path in ['/echo', '/api/runs', '/api/runs/abc/screenshots/obs-0001.png', '/sandbox/']:
            response = client.get(path, headers={'host': 'shoptrace.example', 'x-forwarded-for': '127.0.0.1'})
            self.assertEqual(response.status_code, 401)
            self.assertIn('Basic', response.headers['www-authenticate'])
        self.assertEqual(client.get('/echo', headers={**HEADERS, 'host': 'attacker.example'}).status_code, 400)
        self.assertEqual(client.get('/echo', headers={**HEADERS, 'authorization': 'Basic !!!'}).status_code, 401)
        self.assertEqual(client.get('/healthz').json(), {'ready': True})

    def test_authenticated_write_checks_external_origin_before_rewriting_and_strips_credentials(self):
        client = TestClient(HostedGateway(self.make_inner(), CONFIG))
        for origin in [None, 'https://attacker.example', 'null', 'http://127.0.0.1:8777']:
            headers = dict(HEADERS)
            if origin is not None:
                headers['origin'] = origin
            self.assertEqual(client.post('/echo', headers=headers, json={}).status_code, 403)
        response = client.post('/echo', headers={**HEADERS, 'origin': CONFIG.public_origin,
                                                'x-forwarded-host': 'attacker.example',
                                                'forwarded': 'host=attacker.example', 'cookie': 'private=secret'}, json={})
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['url'], 'http://127.0.0.1:8777/echo')
        self.assertEqual(body['headers']['origin'], 'http://127.0.0.1:8777')
        for name in ['authorization', 'cookie', 'forwarded', 'x-forwarded-host']:
            self.assertNotIn(name, body['headers'])
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertIn('max-age=', response.headers['strict-transport-security'])
        self.assertNotIn('access-control-allow-origin', response.headers)

    def test_duplicate_boundary_headers_and_websockets_are_rejected(self):
        client = TestClient(HostedGateway(self.make_inner(), CONFIG))
        response = client.get('/echo', headers=[('Host', 'shoptrace.example'), ('Authorization', AUTH),
                                               ('Origin', CONFIG.public_origin), ('Origin', 'https://attacker.example')])
        self.assertEqual(response.status_code, 400)
        from starlette.websockets import WebSocketDisconnect
        with self.assertRaises(WebSocketDisconnect):
            with client.websocket_connect('/socket', headers=HEADERS):
                pass

    def test_internal_listener_exposes_only_sandbox_reads(self):
        with tempfile.TemporaryDirectory() as directory:
            from webagent.app import create_app
            inner = create_app(directory)
            client = TestClient(SandboxListener(inner))
            self.assertEqual(client.get('/sandbox/').status_code, 200)
            self.assertEqual(client.get('/sandbox/assets/catalog.json').status_code, 200)
            for path in ['/', '/api/status', '/api/runs', '/sandboxish/']:
                self.assertEqual(client.get(path).status_code, 404)
            self.assertEqual(client.post('/sandbox/').status_code, 404)

    def test_public_case_selection_cookie_survives_only_for_a_known_case(self):
        from webagent.cases import get_catalog, list_cases
        from webagent.app import create_app
        case = list_cases()[0]
        with tempfile.TemporaryDirectory() as directory:
            public, internal = create_hosted_app(CONFIG, inner_app=create_app(directory))
            client = TestClient(public)
            response = client.get(case['start_path'], headers=HEADERS)
            self.assertEqual(response.status_code, 200)
            self.assertIn('shoptrace_case', response.cookies)
            self.assertEqual(client.get('/sandbox/assets/catalog.json', headers=HEADERS).json(), get_catalog(case['id']))
        client = TestClient(HostedGateway(self.make_inner(), CONFIG))
        response = client.get('/echo', headers={**HEADERS, 'cookie': 'session=private; shoptrace_case=' + case['id']})
        self.assertEqual(response.json()['headers'].get('cookie'), 'shoptrace_case=' + case['id'])
        response = client.get('/echo', headers={**HEADERS, 'cookie': 'session=private; shoptrace_case=nonexistent-case'})
        self.assertNotIn('cookie', response.json()['headers'])

    def test_public_gateway_keeps_only_bounded_procurement_cookie(self):
        client = TestClient(HostedGateway(self.make_inner(), CONFIG))
        token = 'b' * 32
        raw = 'private=secret; st_proc=' + token + '; shoptrace_case=invalid-case'
        response = client.get('/echo', headers={**HEADERS, 'cookie': raw})
        self.assertEqual(response.json()['headers'].get('cookie'), 'st_proc=' + token)
        for token in ['b' * 31, 'b' * 33, 'g' * 32, '../invalid']:
            response = client.get('/echo', headers={**HEADERS, 'cookie': 'st_proc=' + token})
            self.assertNotIn('cookie', response.json()['headers'])

    def test_internal_store_writes_require_exact_route_and_origin(self):
        inner = FastAPI()

        @inner.api_route('/{path:path}', methods=['GET', 'POST', 'DELETE', 'PUT', 'PATCH'])
        async def echo(path: str, request: Request):
            return {'path': path, 'method': request.method}

        client = TestClient(SandboxListener(inner))
        prefix = '/sandbox/procurement/api/'
        for name in ['add', 'cart', 'remove']:
            path = prefix + name
            for origin in [None, 'null', 'https://attacker.example']:
                headers = {'origin': origin} if origin is not None else {}
                self.assertEqual(client.post(path, headers=headers, json={}).status_code, 403)
            response = client.post(path, headers={'origin': 'http://testserver'}, json={})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json()['method'], 'POST')
            self.assertEqual(client.delete(path, headers={'origin': 'http://testserver'}).status_code, 404)
        for path in ['/api/runs', '/sandbox/', prefix + 'reset', prefix + 'add/']:
            self.assertEqual(client.post(path, headers={'origin': 'http://testserver'}, json={}).status_code, 404)
        response = client.post(prefix + 'add', headers=[('Origin', 'http://testserver'),
                                                       ('Origin', 'https://attacker.example')], json={})
        self.assertEqual(response.status_code, 400)

    def test_procurement_cart_survives_public_and_internal_gateway(self):
        from webagent import procure
        inner = FastAPI()
        inner.include_router(procure.router)
        public, internal = create_hosted_app(CONFIG, inner_app=inner)
        case = procure.new_case('procure-dev-01', fault='ack_lost')
        token = case['start_path'].split('sid=', 1)[1]
        try:
            client = TestClient(public)
            response = client.get(case['start_path'], headers=HEADERS)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.cookies.get('st_proc'), token)
            headers = {**HEADERS, 'origin': CONFIG.public_origin}
            prefix = '/sandbox/procurement/api/'
            item = client.get(prefix + 'catalog', headers=HEADERS).json()['products'][0]
            reply = client.post(prefix + 'add', headers=headers, json={'id': item['id']})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(reply.json()['state'], 'uncertain')
            self.assertEqual(client.get(prefix + 'cart', headers=HEADERS).json()['count'], 1)
            browser = TestClient(internal, base_url='http://127.0.0.1:8777')
            headers = {'origin': 'http://127.0.0.1:8777', 'cookie': 'st_proc=' + token}
            reply = browser.post(prefix + 'cart', headers=headers, json={'id': item['id'], 'delta': 1})
            self.assertEqual(reply.status_code, 200)
            self.assertEqual(client.get(prefix + 'cart', headers=HEADERS).json()['count'], 2)
            self.assertEqual(browser.post(prefix + 'remove', headers=headers, json={'id': item['id']}).status_code, 200)
            self.assertEqual(client.get(prefix + 'cart', headers=HEADERS).json()['count'], 0)
        finally:
            with procure._LOCK:
                procure._SESSIONS.pop(token, None)

    def test_optional_reports_are_read_only_and_protected_by_the_same_login(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / 'report'
            report.mkdir()
            (report / 'index.html').write_text('<h1>Saved evidence</h1>')
            inner = self.make_inner()
            public, internal = create_hosted_app(CONFIG, inner_app=inner, report_dir=report)
            client = TestClient(public)
            self.assertEqual(client.get('/reports/', headers={'host': 'shoptrace.example'}).status_code, 401)
            self.assertIn('Saved evidence', client.get('/reports/', headers=HEADERS).text)
            self.assertEqual(client.post('/reports/index.html', headers={**HEADERS, 'origin': CONFIG.public_origin}).status_code, 405)
            self.assertEqual(TestClient(internal).get('/reports/').status_code, 404)

    def test_real_application_generates_internal_browser_urls_and_uses_one_lifespan(self):
        # The job is stopped before touching a model/browser: verify only that
        # its generated URL points at the private listener and auth is absent.
        with tempfile.TemporaryDirectory() as directory:
            from webagent.app import create_app
            inner = create_app(directory)
            public, internal = create_hosted_app(CONFIG, inner_app=inner)
            with patch('webagent.app.BrowserRuntime') as runtime:
                async def no_run(*args, **kwargs):
                    return None
                runtime.return_value.run.side_effect = no_run
                with TestClient(public) as client:
                    response = client.post('/api/runs', headers={**HEADERS, 'origin': CONFIG.public_origin},
                                           json={'task_id': 'browser-headphones-commute', 'provider': 'browser_smoke'})
                    self.assertEqual(response.status_code, 201, response.text)
                    document = response.json()
                    self.assertEqual(document['browser']['start_url'], 'http://127.0.0.1:8777/sandbox/')
                    self.assertEqual(document['browser']['allowed_origins'], ['http://127.0.0.1:8777'])
                    self.assertNotIn(PASSWORD, json.dumps(document))
                    self.assertNotIn(AUTH, json.dumps(document))
                    self.assertEqual(client.get('/api/status', headers=HEADERS).status_code, 200)


class HostedLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_servers_stop_when_one_finishes_and_signals_have_one_coordinator(self):
        servers = []

        class FakeServer:
            def __init__(self, config):
                self.config, self.should_exit = config, False
                self.capture_signals = lambda: None
                servers.append(self)

            async def serve(self):
                if self.config.host == '127.0.0.1':
                    return
                while not self.should_exit:
                    await asyncio.sleep(0)

        with tempfile.TemporaryDirectory() as directory, patch('uvicorn.Server', FakeServer):
            await asyncio.wait_for(serve(CONFIG, data_dir=directory), timeout=2)
        self.assertEqual(len(servers), 2)
        self.assertTrue(all(server.should_exit for server in servers))
        internal = next(s for s in servers if s.config.host == '127.0.0.1')
        self.assertEqual(internal.config.lifespan, 'off')
        self.assertTrue(all(server.capture_signals is __import__('contextlib').nullcontext for server in servers))
        self.assertFalse(any(s.config.proxy_headers for s in servers))


if __name__ == '__main__':
    unittest.main()
