"""Real Chromium plus a labelled fake HTTP provider; never an LLM benchmark."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
import shutil
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from lab.models import APIModel
from lab.storage import Store
from webagent.runtime import BrowserRuntime, evaluation, parse_action
from webagent.smoke import SmokePolicy

ASSETS = Path(__file__).resolve().parents[1] / "webagent/sandbox_assets"
AVAILABLE = importlib.util.find_spec("playwright") is not None and bool(os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium"))


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        path = urlsplit(self.path).path
        if path in {"/sandbox/", "/sandbox/product", "/sandbox/cart"}:
            name = "index.html"
        elif path.startswith("/sandbox/assets/"):
            name = path.removeprefix("/sandbox/assets/")
        else:
            self.send_error(404)
            return
        if name not in {"index.html", "app.js", "styles.css", "catalog.json"}:
            self.send_error(404)
            return
        raw = (ASSETS / name).read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", {"html": "text/html", "js": "application/javascript", "css": "text/css", "json": "application/json"}[name.rsplit(".", 1)[1]] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class ProtocolTests(unittest.TestCase):
    def test_parallel_or_invented_browser_tools_are_rejected(self):
        from lab.models import ModelProtocolError
        with self.assertRaises(ModelProtocolError):
            parse_action({"tool_calls": []})
        with self.assertRaises(ModelProtocolError):
            parse_action({"tool_calls": [{"id": "c", "type": "function", "function": {"name": "browser_evaluate", "arguments": '{"code":"1+1"}'}}]})

    def test_smoke_and_unknown_custom_outcomes_are_excluded_from_model_metric(self):
        result = evaluation([{"source": "browser_smoke", "task_id": "x", "status": "completed", "result": {"success": True}},
                             {"source": "browser_model", "task_id": "custom", "status": "completed", "result": {"success": None}}])
        self.assertEqual(result["smoke_runs"], 1)
        self.assertEqual(result["model_runs"], 0)
        self.assertIsNone(result["model_success_rate"])


@unittest.skipUnless(AVAILABLE, "Playwright and a Chromium executable are required")
class BrowserRuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_dom_loop_through_mock_deepseek_tool_provider(self):
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_port}"
        policy, requests = SmokePolicy("browser-thermos-variant"), []

        def provider(request):
            data = json.loads(request.content)
            requests.append(data)
            self.assertEqual(request.url.host, "api.deepseek.com")
            self.assertEqual(data["thinking"], {"type": "disabled"})
            self.assertNotIn("parallel_tool_calls", data)
            content = data["messages"][-1]["content"]
            if data["messages"][-1]["role"] == "user":
                observation = json.loads(content.split("\n\n", 1)[1])
            else:
                observation = json.loads(content)
            action = policy.next_action(observation)
            name = action.pop("type")
            return httpx.Response(200, json={"choices": [{"message": {"role": "assistant", "content": None,
                "tool_calls": [{"id": f"mock-{len(requests)}", "type": "function", "function": {
                    "name": "finish" if name == "finish" else "browser_" + name,
                    "arguments": json.dumps(action, ensure_ascii=False)}}]}}], "usage": {"prompt_tokens": 100, "completion_tokens": 20}})

        try:
            with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {
                "LLM_BASE_URL": "https://api.deepseek.com/v1", "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "not-a-real-model-secret"}):
                store = Store(Path(directory) / "db")
                spec = {"mode": "browser", "task_id": "browser-thermos-variant", "query": "Find a blue 500 mL thermos costing no more than CNY 100 and add it to the list.",
                    "model": {"provider": "api", "name": "deepseek-flash"},
                    "runtime": {"max_steps": 30, "max_seconds": 120, "max_output_tokens": 512},
                    "browser": {"start_url": base + "/sandbox/", "allowed_origins": [base], "allowed_path_prefixes": {base: ["/sandbox/"]}}}
                run = store.create_run(spec, source="api_mock_browser_contract")
                runtime = BrowserRuntime(store, Path(directory) / "screenshots",
                    model_factory=lambda spec: APIModel(spec["model"]["name"], transport=httpx.MockTransport(provider)))
                await runtime.run(run["run_id"], spec, asyncio.Event())
                final = store.get_run(run["run_id"])
                self.assertEqual(final["status"], "completed", final)
                self.assertTrue(final["result"]["success"], final)
                self.assertGreater(final["model_calls"], 0)
                self.assertEqual(final["model_calls"], len(requests))
                self.assertEqual(final["usage"]["input_tokens"], len(requests) * 100)
                exported = store.export(run["run_id"])
                self.assertNotIn("not-a-real-model-secret", json.dumps(exported))
                model_events = [event for event in exported["events"] if event["type"] == "model_request"]
                self.assertTrue(model_events)
                for event in model_events:
                    self.assertEqual(event["payload"]["generation_config"]["thinking"], {"type": "disabled"})
                    self.assertNotIn("parallel_tool_calls", event["payload"]["generation_config"])
                images = [event for event in exported["events"] if event["type"] == "observation"]
                self.assertGreater(len(images), 4)
                self.assertTrue(all((Path(directory) / "screenshots" / run["run_id"] / event["payload"]["observation"]["screenshot"]).is_file() for event in images))
                self.assertEqual(evaluation([final])["model_runs"], 0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
