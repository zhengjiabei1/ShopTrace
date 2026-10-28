"""Local application flows and provenance boundaries (no external services)."""
import json
import os
import sqlite3
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient

from lab.app import create_app
from lab.storage import SCHEMA_VERSION, Store


class ApplicationTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"LLM_API_KEY": "test-secret-not-for-logs"})
        self.env.start()
        self.app = create_app(self.directory.name)
        self.context = TestClient(self.app)
        self.client = self.context.__enter__()

    def tearDown(self):
        self.context.__exit__(None, None, None)
        self.env.stop()
        self.directory.cleanup()

    def start_fixture(self, **overrides):
        response = self.client.post("/api/runs", json={"task_id": "fixture-001", **overrides})
        self.assertEqual(response.status_code, 201, response.text)
        run_id = response.json()["run_id"]
        for _ in range(100):
            run = self.client.get(f"/api/runs/{run_id}").json()
            if run["status"] not in {"running", "pending"}:
                return run
            time.sleep(.01)
        self.fail("fixture did not terminate")

    def test_run_export_import_annotation_and_evaluation(self):
        run = self.start_fixture()
        self.assertEqual(run["status"], "completed", run)
        self.assertIsNone(run["result"]["strict_success"])
        self.assertTrue(run["result"]["fixture_success"])
        run_id = run["run_id"]
        events = self.client.get(f"/api/runs/{run_id}/events").json()["events"]
        self.assertTrue(any(e["type"] == "model_request" for e in events))
        annotation = self.client.post(f"/api/runs/{run_id}/annotations", json={"event_id": events[0]["event_id"], "category": "evidence", "note": "test-secret-not-for-logs"})
        self.assertEqual(annotation.status_code, 201)
        exported = self.client.get(f"/api/runs/{run_id}/export").json()
        self.assertNotIn("test-secret-not-for-logs", json.dumps(exported))
        self.assertEqual(exported["schema_version"], SCHEMA_VERSION)
        imported = self.client.post("/api/import", json={"trajectory": exported})
        self.assertEqual(imported.status_code, 201, imported.text)
        self.assertEqual(imported.json()["source"], "imported")
        self.assertNotEqual(imported.json()["run_id"], run_id)
        saved = self.client.get(f'/api/runs/{imported.json()["run_id"]}/events').json()["events"]
        self.assertEqual(len(saved), len(events))
        summary = self.client.get("/api/evaluation")
        self.assertEqual(summary.status_code, 200, summary.text)
        self.assertEqual(self.client.get("/api/compare", params={"left": run_id, "right": imported.json()["run_id"]}).status_code, 200)

    def test_budget_end_keeps_events(self):
        run = self.start_fixture(runtime={"max_steps": 1})
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["steps"], 1)
        self.assertTrue(self.client.get(f'/api/runs/{run["run_id"]}/events').json()["events"])

    def test_api_boundary_rejects_scripted_real_and_cross_origin(self):
        self.assertEqual(self.client.post("/api/runs", json={"task_id": "1", "mode": "shopsimulator"}).status_code, 422)
        self.assertEqual(self.client.post("/api/runs", headers={"Origin": "https://foreign.example"}, json={"task_id": "fixture-001"}).status_code, 403)
        self.assertEqual(self.client.get("/api/status", headers={"Host": "foreign.example"}).status_code, 400)
        self.assertEqual(self.client.post("/api/runs", json={"task_id": "fixture-001", "query": "do not silently change benchmark query"}).status_code, 422)

    def test_malformed_import_does_not_create_run(self):
        before = len(self.app.state.store.list_runs())
        for data in ({}, {"trajectory": []}, {"schema_version": SCHEMA_VERSION, "run": {}, "events": []}):
            self.assertEqual(self.client.post("/api/import", json=data).status_code, 422)
        self.assertEqual(len(self.app.state.store.list_runs()), before)

    def test_invalid_annotation_is_rejected(self):
        run = self.start_fixture()
        response = self.client.post(f'/api/runs/{run["run_id"]}/annotations', json={"event_id": "invented", "category": "scoring", "note": "not evidence"})
        self.assertEqual(response.status_code, 422)

    def test_upstream_history_import_does_not_execute_or_certify_model(self):
        upstream = {"task_id": 53, "initial_result": {"instruction": "Development task", "goal_options": "PRIVATE"},
                    "messages": [], "steps": [{"tool_name": "search_products", "parameters": {"query": "public query"},
                    "env_action": "search[public query]", "observation": "Public results", "done": False}],
                    "terminal_result": {"done": False, "over": True}}
        response = self.client.post("/api/import", json=upstream)
        self.assertEqual(response.status_code, 201, response.text)
        run = response.json()
        self.assertEqual(run["source"], "imported")
        self.assertEqual(run["runtime"], {})
        self.assertIsNone(run["model_calls"])
        exported = self.client.get(f'/api/runs/{run["run_id"]}/export').json()
        self.assertNotIn("PRIVATE", json.dumps(exported))
        self.assertFalse(any(e["type"] == "model_request" for e in exported["events"]))
        self.assertEqual(self.client.get("/api/evaluation").json()["formal_run_count"], 0)

    def test_raw_json_endpoints_reject_nonobjects(self):
        for endpoint in ("/api/evaluation", "/api/datasets/validate", "/api/datasets/export-sft"):
            self.assertEqual(self.client.post(endpoint, json=[]).status_code, 422)
            self.assertEqual(self.client.post(endpoint, content="{broken", headers={"Content-Type": "application/json"}).status_code, 422)


class StorageTests(unittest.TestCase):
    def test_connection_is_closed_after_transaction(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with store.connect() as connection:
                self.assertEqual(connection.execute("SELECT 1").fetchone()[0], 1)
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_failed_transaction_rolls_back_and_closes(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with self.assertRaises(ValueError):
                with store.connect() as connection:
                    connection.execute("INSERT INTO runs VALUES (?,?)", ("aborted", "{}"))
                    raise ValueError("abort transaction")
            self.assertEqual(store.list_runs(), [])
            with self.assertRaises(sqlite3.ProgrammingError):
                connection.execute("SELECT 1")

    def test_events_survive_restart_and_interrupted_runs_are_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            first = Store(directory)
            run = first.create_run({"task_id": "fixture-001", "mode": "fixture"})
            first.append_event(run["run_id"], "observation", {"observation": "saved before crash"})
            second = Store(directory)
            self.assertEqual(second.recover_interrupted(), 1)
            restored = second.get_run(run["run_id"])
            self.assertEqual(restored["termination_reason"], "process_interrupted")
            self.assertEqual(second.events(run["run_id"])[0]["payload"]["observation"], "saved before crash")
            self.assertEqual(second.recover_interrupted(), 0)


if __name__ == "__main__":
    unittest.main()
