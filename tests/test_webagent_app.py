"""Lightweight API contracts; browser and model execution are explicit test doubles."""
import asyncio
import importlib
import os
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

appmod = importlib.import_module("webagent.app")


class FakeBrowserRuntime:
    """API scheduling fixture, not a browser or model capability test."""

    def __init__(self, store, artifacts):
        self.store = store

    async def run(self, run_id, spec, cancel):
        await asyncio.sleep(0)
        self.store.update_run(run_id, status="completed", steps=1, model_calls=0,
                              result={"success": None if spec["task_id"] == "custom" else True,
                                      "reason": "API scheduling fixture only"})
        self.store.append_event(run_id, "status", {"status": "completed", "fixture_only": True})


class WebAgentAppTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.environment = patch.dict(os.environ, {
            "LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": "",
            "WEBAGENT_ALLOWED_ORIGINS": "",
        })
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.runtime = patch.object(appmod, "BrowserRuntime", FakeBrowserRuntime)
        self.runtime.start()
        self.addCleanup(self.runtime.stop)
        self.browser = patch.object(appmod, "browser_available", return_value=True)
        self.browser.start()
        self.addCleanup(self.browser.stop)
        self.app = appmod.create_app(self.root)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.task_id = self.client.get("/api/tasks").json()["tasks"][0]["id"]

    def _smoke(self):
        response = self.client.post("/api/runs", json={"task_id": self.task_id, "provider": "browser_smoke"})
        self.assertEqual(response.status_code, 201)
        run = response.json()
        for _ in range(100):
            run = self.client.get(f'/api/runs/{run["run_id"]}').json()
            if run["status"] not in {"pending", "running"}:
                return run
            time.sleep(0.01)
        self.fail("API scheduling fixture did not finish")

    def test_status_and_unconfigured_model_rejection(self):
        status = self.client.get("/api/status").json()
        self.assertEqual(status["application"], "shoptrace-webagent")
        self.assertFalse(status["capabilities"]["llm_configured"])
        response = self.client.post("/api/runs", json={"task_id": self.task_id, "provider": "api"})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])

    def test_task_budget_and_cross_origin_boundaries(self):
        self.assertEqual(self.client.post("/api/runs", json={"task_id": "unknown", "provider": "browser_smoke"}).status_code, 422)
        self.assertEqual(self.client.post("/api/runs", json={"task_id": self.task_id, "max_steps": 0}).status_code, 422)
        self.assertEqual(self.client.post("/api/runs", json={"task_id": self.task_id, "max_seconds": 1801}).status_code, 422)
        self.assertEqual(self.client.post("/api/runs", headers={"Origin": "https://other.example"}, json={"task_id": self.task_id}).status_code, 403)
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])

    def test_smoke_source_export_and_model_metric_exclusion(self):
        run = self._smoke()
        self.assertEqual(run["source"], "browser_smoke")
        self.assertEqual(run["model_calls"], 0)
        response = self.client.get(f'/api/runs/{run["run_id"]}/export')
        self.assertEqual(response.status_code, 200)
        exported = response.json()
        self.assertEqual(exported["schema_version"], "shoptrace.webagent.trajectory.v1")
        self.assertEqual(exported["run"]["run_id"], run["run_id"])
        self.assertTrue(exported["events"])
        summary = self.client.get("/api/evaluation").json()
        self.assertEqual(summary["smoke_runs"], 1)
        self.assertEqual(summary["model_runs"], 0)
        self.assertIsNone(summary["model_success_rate"])

    def test_custom_sites_require_explicit_allowed_origin_and_remain_unscored(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1", "LLM_MODEL": "fixture-provider", "LLM_API_KEY": "private-unit-test-key"}):
            response = self.client.post("/api/custom-runs", json={"url": "https://docs.example/", "goal": "Find a public heading"})
            self.assertEqual(response.status_code, 422)
            with patch.dict(os.environ, {"WEBAGENT_ALLOWED_ORIGINS": "https://docs.example"}):
                response = self.client.post("/api/custom-runs", json={"url": "https://docs.example/", "goal": "Find a public heading"})
                self.assertEqual(response.status_code, 201)
                run_id = response.json()["run_id"]
                for _ in range(100):
                    run = self.client.get(f"/api/runs/{run_id}").json()
                    if run["status"] == "completed":
                        break
                    time.sleep(0.01)
                self.assertEqual(run["task_id"], "custom")
                self.assertIsNone(run["result"]["success"])
                exported = self.client.get(f"/api/runs/{run_id}/export").text
                self.assertNotIn("private-unit-test-key", exported)
                self.assertEqual(self.client.get("/api/evaluation").json()["model_runs"], 0)

    def test_invalid_urls_are_rejected_before_scheduling(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1", "LLM_MODEL": "fixture-provider", "WEBAGENT_ALLOWED_ORIGINS": "https://docs.example"}):
            for url in ["file:///tmp/private", "javascript:alert(1)", "https://user:password@docs.example/", "https://docs.example/#fragment"]:
                with self.subTest(url=url):
                    response = self.client.post("/api/custom-runs", json={"url": url, "goal": "Read a page"})
                    self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])

    def test_unknown_runs_and_screenshot_paths_return_not_found(self):
        self.assertEqual(self.client.get("/api/runs/not-a-run").status_code, 404)
        run = self._smoke()
        self.assertEqual(self.client.get(f'/api/runs/{run["run_id"]}/screenshots/not-allowed.txt').status_code, 404)
        self.assertEqual(self.client.get(f'/api/runs/{run["run_id"]}/screenshots/obs-0001.png').status_code, 404)


if __name__ == "__main__":
    unittest.main()
