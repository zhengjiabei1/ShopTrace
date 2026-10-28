"""Procurement API and runtime contracts with labelled test doubles."""
import asyncio
from copy import deepcopy
import hashlib
import importlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from lab.storage import Store
from webagent.runtime import BrowserRuntime, PROMPT, evaluation
from webagent.recovery import policy_text


class Session:
    def __init__(self, *args, **kwargs):
        self.page, self.seq, self.actions = object(), 1, []

    async def start(self):
        return await self.observe()

    async def observe(self):
        return {"observation_id": f"obs-{self.seq:04d}", "url": f"http://127.0.0.1/sandbox/procurement/product?id={self.seq}",
                "text": "Keyboard wireless CNY 120" if self.seq == 1 else "Add result not confirmed. Your shopping list may have changed.",
                "elements": [{"ref": f"obs-{self.seq:04d}-e1", "name": "View shopping list"}]}

    async def act(self, action):
        self.actions.append(deepcopy(action))
        self.seq += 1
        return await self.observe()

    async def close(self):
        pass


class Model:
    def __init__(self):
        self.requests = []

    async def generate(self, messages, tools, max_tokens):
        self.requests.append(deepcopy(messages))
        count = len(self.requests)
        name, args = ("browser_wait", {"milliseconds": 0}) if count == 1 else ("finish", {"reason": "completed", "evidence": ["List checked"]})
        return {"message": {"role": "assistant", "content": None, "tool_calls": [{"id": str(count), "type": "function",
                "function": {"name": name, "arguments": json.dumps(args)}}]},
                "usage": {"input_tokens": 10, "output_tokens": 4}}

    async def close(self):
        pass


class ProcRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def execute(self, budget=5, source_ok=None):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        store, session, model = Store(temp.name), Session(), Model()
        spec = {"mode": "browser", "task_id": "procure-dev-01", "query": "Buy a keyboard and mouse",
                "benchmark_case": {"fault": "ack_lost", "answer": "PRIVATE-GRADING-TARGET"},
                "runtime": {"max_steps": budget, "max_seconds": 10, "max_output_tokens": 128,
                            "context_mode": "page_memory", "recovery": "adaptive"},
                "model": {"provider": "api", "name": "labelled-mock-model"},
                "browser": {"start_url": "http://127.0.0.1/sandbox/procurement/", "allowed_origins": ["http://127.0.0.1"]}}
        run = store.create_run(spec, source="api_mock_browser_contract")
        runtime = BrowserRuntime(store, Path(temp.name) / "shots", session_factory=lambda *a, **kw: session,
                                 model_factory=lambda spec: model)
        if source_ok is not None:
            runtime.source_check = lambda: source_ok
        grade = AsyncMock(return_value={"benchmark": "shoptrace-procurement-v1", "success": True, "reason": "test double"})
        audit = {"benchmark": "shoptrace-procurement-v1", "task_id": spec["task_id"], "fault": "ack_lost",
                 "metrics": {"fault_triggered": True, "add_attempts": 1, "duplicates": 0}, "operations": []}
        with patch("webagent.tasks.evaluate_task", grade), patch("webagent.procure.audit", return_value=audit):
            await runtime.run(run["run_id"], spec, asyncio.Event())
        return store.get_run(run["run_id"]), store.events(run["run_id"]), session, model, grade

    async def test_memory_and_visible_recovery_are_recorded_without_hidden_inputs(self):
        run, events, session, model, grade = await self.execute()
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["model_calls"], 2)
        self.assertEqual(run["source"], "api_mock_browser_contract")
        self.assertEqual(len(session.actions), 1)
        grade.assert_awaited_once()
        request = json.dumps(model.requests[1])
        self.assertIn("Keyboard wireless CNY 120", request)
        self.assertIn("check its current quantity", request)
        self.assertNotIn("PRIVATE-GRADING-TARGET", request)
        self.assertNotIn("ack_lost", request)
        self.assertNotIn("obs-0001-e1", request)
        self.assertIn("obs-0002-e1", request)
        self.assertEqual(run["memory_stats"]["pages"], 2)
        self.assertFalse(run["recovery_policy"]["automatic_actions"])
        self.assertEqual(run["prompt_hash"], hashlib.sha256((PROMPT + "\n\n" + policy_text("adaptive")).encode()).hexdigest())
        self.assertEqual(sum(e["type"] == "memory" for e in events), 2)
        self.assertEqual(sum(e["type"] == "procurement_audit" for e in events), 1)
        hints = [e["payload"] for e in events if e["type"] == "recovery"]
        self.assertEqual([row["state"] for row in hints], ["neutral", "uncertain"])

    async def test_budget_stop_keeps_unknown_outcome_and_never_replays_action(self):
        run, events, session, model, grade = await self.execute(budget=1)
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertIsNone(run["result"]["success"])
        self.assertEqual(run["result"]["benchmark"], "shoptrace-procurement-v1")
        self.assertEqual(len(session.actions), 1)
        self.assertEqual(run["model_calls"], 1)
        self.assertEqual(run["memory_stats"]["pages"], 2)
        self.assertTrue(run["result"]["metrics"]["fault_triggered"])
        grade.assert_not_awaited()

    async def test_changed_server_source_invalidates_score_but_retains_evidence(self):
        run, events, session, model, grade = await self.execute(source_ok=False)
        self.assertEqual(run["status"], "failed")
        self.assertEqual(run["termination_reason"], "source_changed")
        self.assertFalse(run["source_unchanged"])
        self.assertIsNone(run["result"]["success"])
        self.assertTrue(run["result"]["score_before_source_change"])
        self.assertTrue(run["result"]["metrics"]["fault_triggered"])
        self.assertTrue(any(e["type"] == "evaluation" for e in events))


class APIRuntime:
    def __init__(self, store, artifacts):
        self.store = store

    async def run(self, run_id, spec, cancel):
        self.store.update_run(run_id, status="completed", result={"success": None, "reason": "scheduling test double"})


class ProcAPITests(unittest.TestCase):
    def setUp(self):
        self.appmod = importlib.import_module("webagent.app")
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        env = patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1", "LLM_MODEL": "fixture-model"})
        env.start()
        self.addCleanup(env.stop)
        fake = patch.object(self.appmod, "BrowserRuntime", APIRuntime)
        fake.start()
        self.addCleanup(fake.stop)
        self.client = TestClient(self.appmod.create_app(temp.name))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def test_procurement_run_has_frozen_source_and_separate_configuration(self):
        status = self.client.get("/api/status").json()
        tasks = self.client.get("/api/procurement?split=development").json()["tasks"]
        self.assertEqual(len(tasks), 6)
        self.assertEqual(len(self.client.get("/api/tasks").json()["tasks"]), 6)
        self.assertTrue(all("goal" not in t and "fault" not in t for t in tasks))
        response = self.client.post("/api/runs", json={"task_id": tasks[0]["id"], "provider": "api", "seed": 31,
            "fault": "ack_lost", "recovery": "adaptive", "context_mode": "page_memory"})
        self.assertEqual(response.status_code, 201, response.text)
        run = response.json()
        self.assertEqual(run["source_fingerprint"], status["source_fingerprint"])
        self.assertEqual(run["runtime"]["recovery"], "adaptive")
        self.assertEqual(run["runtime"]["context_mode"], "page_memory")
        self.assertEqual(run["benchmark_case"]["fault"], "ack_lost")
        self.assertEqual(run["benchmark_case"]["seed"], 31)
        self.assertEqual(run["benchmark_case"]["benchmark"], "shoptrace-procurement-v1")
        self.assertNotIn("ack_lost", run["query"] + run["browser"]["start_url"])

    def test_unsupported_settings_are_rejected_before_launch(self):
        for changes in ({"fault": "ack_lost"}, {"seed": 8}, {"recovery": "adaptive"}):
            response = self.client.post("/api/runs", json={"task_id": "browser-thermos-variant", **changes})
            self.assertEqual(response.status_code, 422)
        for changes in ({"provider": "browser_smoke"}, {"provider": "api", "fault": "invented"}, {"provider": "api", "seed": -1}):
            response = self.client.post("/api/runs", json={"task_id": "procure-dev-01", **changes})
            self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])

    def test_evaluation_keeps_recovery_and_benchmark_groups_separate(self):
        common = {"source": "browser_model", "status": "completed", "model": {"provider": "api", "name": "mock"},
                  "runtime": {"context_mode": "page_memory", "recovery": "verify"},
                  "task_id": "procure-dev-01", "result": {"success": True, "benchmark": "shoptrace-procurement-v1"}}
        rows = [deepcopy(common), {**deepcopy(common), "runtime": {"context_mode": "page_memory", "recovery": "baseline"},
                                  "result": {"success": False, "benchmark": "shoptrace-procurement-v1"}}]
        report = evaluation(rows)
        self.assertTrue(report["mixed_configuration"])
        self.assertIsNone(report["model_success_rate"])
        self.assertEqual(len(report["configuration_groups"]), 2)
        self.assertEqual(sorted(g["success_rate_known"] for g in report["configuration_groups"]), [0, 1])

    def test_changed_source_requires_restart_before_any_new_session(self):
        frozen = self.client.get("/api/status").json()["source_fingerprint"]
        with patch.object(self.appmod, "source_info", return_value={"source_fingerprint": "changed", "source_files": {}}):
            status = self.client.get("/api/status").json()
            self.assertFalse(status["source_current"])
            self.assertEqual(status["source_fingerprint"], frozen)
            with patch("webagent.procure.new_case") as create:
                response = self.client.post("/api/runs", json={"task_id": "procure-dev-01", "provider": "api"})
                self.assertEqual(response.status_code, 409)
                create.assert_not_called()
        self.assertEqual(self.client.get("/api/runs").json()["runs"], [])
