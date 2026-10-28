"""Storage failures must not bypass browser and model cleanup."""
import asyncio
import errno
import importlib
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from lab.storage import Store
from webagent.runtime import BrowserRuntime


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_full_disk_and_failed_close_still_close_each_resource(self):
        for close_err in (None, OSError("close failed"), asyncio.CancelledError()):
            with self.subTest(close_error=type(close_err).__name__), tempfile.TemporaryDirectory() as temp:
                closed, writes = [], []

                class Session:
                    async def start(self):
                        return {"text": "Visible page", "elements": [], "url": "http://127.0.0.1/sandbox/procurement/"}

                    async def close(self):
                        closed.append("browser")
                        if close_err is not None:
                            raise close_err

                class Model:
                    async def generate(self, *args):
                        raise AssertionError("A failed observation must stop before a model request")

                    async def close(self):
                        closed.append("model")

                store = Store(temp)
                spec = {"mode": "browser", "task_id": "procure-dev-01", "query": "Buy a keyboard",
                        "model": {"provider": "api", "name": "fixture"},
                        "runtime": {"max_steps": 5, "max_seconds": 20, "max_output_tokens": 128,
                                    "context_mode": "page_memory", "recovery": "verify"},
                        "browser": {"start_url": "http://127.0.0.1/sandbox/procurement/?sid=fixture", "allowed_origins": ["http://127.0.0.1"]}}
                run = store.create_run(spec, source="api_mock_browser_contract")
                event, update = store.append_event, store.update_run
                full = False

                def fail(method, *args, **kwargs):
                    nonlocal full
                    if full:
                        writes.append(tuple(closed))
                        raise OSError(errno.ENOSPC, "No space left on device")
                    if method == "event" and args[1] == "observation":
                        full = True
                        raise OSError(errno.ENOSPC, "No space left on device")
                    return (event if method == "event" else update)(*args, **kwargs)

                store.append_event = lambda *a, **kw: fail("event", *a, **kw)
                store.update_run = lambda *a, **kw: fail("update", *a, **kw)
                runtime = BrowserRuntime(store, Path(temp) / "shots", session_factory=lambda *a, **kw: Session(),
                                         model_factory=lambda spec: Model())
                runtime.source_check = lambda: True
                with patch("webagent.procure.audit", return_value={"metrics": {"fault_triggered": False}}):
                    with self.assertRaises(OSError) as raised:
                        await runtime.run(run["run_id"], spec, asyncio.Event())
                self.assertEqual(raised.exception.errno, errno.ENOSPC)
                self.assertEqual(closed, ["browser", "model"])
                self.assertGreaterEqual(len(writes), 5)
                self.assertTrue(all(row == ("browser", "model") for row in writes))
                self.assertIsNone(store.get_run(run["run_id"])["result"].get("success"))


class DiskSpaceTests(unittest.TestCase):
    def test_low_disk_rejects_before_creating_a_session_or_run(self):
        appmod = importlib.import_module("webagent.app")
        with tempfile.TemporaryDirectory() as temp, patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1", "LLM_MODEL": "fixture"}):
            with TestClient(appmod.create_app(temp)) as client:
                with patch.object(appmod.shutil, "disk_usage", return_value=SimpleNamespace(free=99 * 1024 * 1024)), \
                        patch("webagent.procure.new_case") as create:
                    response = client.post("/api/runs", json={"task_id": "procure-dev-01", "provider": "api"})
                    self.assertEqual(response.status_code, 503)
                    self.assertIn("100 MiB", response.json()["detail"])
                    create.assert_not_called()
                self.assertEqual(client.get("/api/runs").json()["runs"], [])
