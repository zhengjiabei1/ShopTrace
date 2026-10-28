"""Batch safety/measurement contracts using an explicitly mocked workbench."""
import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import httpx

from webagent.benchmark import BenchmarkError, _base_url, run_batch, source_snapshot, summarize


def run_document(task="task-one", number=1, *, success=True, status="completed", prompt="prompt", usage=True):
    return {"run_id": f"{number:032x}", "task_id": task, "source": "browser_model",
            "model": {"provider": "api", "name": "mock-model"}, "status": status,
            "runtime": {"max_steps": 35, "max_seconds": 180, "max_output_tokens": 512},
            "result": {"success": success}, "error": None, "steps": 2, "model_calls": 2,
            "model_responses": 2, "elapsed_seconds": 4,
            "usage": {"input_tokens": 20 if usage else None, "output_tokens": 5 if usage else None},
            "protocol_version": "test-only", "prompt_hash": prompt, "tools_hash": "tools",
            "context_policy": {"max": 96000}}


class BenchmarkTests(unittest.TestCase):
    def client(self, handler):
        return httpx.Client(base_url="http://127.0.0.1:8775", transport=httpx.MockTransport(handler))

    def test_source_snapshot_covers_catalog_routing_browser_assets_and_shared_helpers(self):
        root = Path(__file__).resolve().parents[1]
        snapshot = source_snapshot()
        for name in ("webagent/sandbox.py", "webagent/sandbox_assets/app.js",
                     "webagent/sandbox_assets/index.html", "webagent/sandbox_assets/styles.css",
                     "lab/runtime.py", "lab/storage.py"):
            with self.subTest(name=name):
                self.assertIn(name, snapshot["files"])
                self.assertEqual(snapshot["files"][name], hashlib.sha256((root / name).read_bytes()).hexdigest())

    def test_existing_directory_stops_before_any_request(self):
        requests = []
        with tempfile.TemporaryDirectory() as directory, self.client(lambda r: requests.append(r)) as client:
            with self.assertRaisesRegex(BenchmarkError, "already exists"):
                run_batch("http://127.0.0.1:8775", ["task-one"], 1, directory, client=client)
        self.assertEqual(requests, [])

    def test_remote_control_urls_and_unbounded_plans_are_rejected(self):
        for url in ("https://model.example", "http://127.0.0.1@evil.example", "http://127.0.0.1/path", "http://localhost:bad"):
            with self.subTest(url=url), self.assertRaises(BenchmarkError):
                _base_url(url)
        with tempfile.TemporaryDirectory() as directory:
            for repeats in (0, 11, True):
                with self.assertRaises(BenchmarkError):
                    run_batch("http://localhost:8775", ["task-one"], repeats, Path(directory) / "new")
            with self.assertRaises(BenchmarkError):
                run_batch("http://localhost:8775", ["task-one", "task-one"], 1, Path(directory) / "new")
            self.assertFalse((Path(directory) / "new").exists())

    def test_unknown_scores_are_not_failures_and_usage_missing_is_not_zero(self):
        rows = [{"run": run_document(success=True)},
                {"run": run_document(number=2, success=False)},
                {"run": run_document(number=3, success=None, status="failed", usage=False)},
                {"run": {**run_document(number=4), "source": "browser_smoke"}},
                {"submission_state": "not_started"}]
        result = summarize(rows)
        self.assertEqual(result["eligible_model_attempts"], 3)
        self.assertEqual(result["successes"], 1)
        self.assertEqual(result["failures"], 1)
        self.assertEqual(result["unknown_results"], 1)
        self.assertEqual(result["success_rate_known"], .5)
        self.assertAlmostEqual(result["result_coverage"], 2 / 3)
        self.assertEqual(result["excluded_non_model_runs"], 1)
        self.assertEqual(result["input_tokens"], {"known_count": 2, "missing_count": 1, "sum": 40, "mean": 20, "median": 20})
        self.assertEqual(result["incomplete_submissions"], 1)

    def test_different_prompts_do_not_produce_one_pooled_model_score(self):
        result = summarize([{"run": run_document()}, {"run": run_document(number=2, prompt="other", success=False)}])
        self.assertTrue(result["mixed_configuration"])
        self.assertIsNone(result["success_rate_known"])
        self.assertIsNone(result["pooled_success_rate"])
        self.assertEqual(len(result["configuration_groups"]), 2)
        self.assertEqual(sorted(g["success_rate_known"] for g in result["configuration_groups"]), [0, 1])
        self.assertTrue(result["per_task"]["task-one"]["mixed_configuration"])
        self.assertIsNone(result["per_task"]["task-one"]["success_rate_known"])

    def test_case_catalogs_share_suite_group_but_heldout_is_separate(self):
        first = {**run_document(task="case-0001"), "benchmark_case": {
            "id": "case-0001", "split": "development", "dataset_fingerprint": "dataset-one", "catalog_fingerprint": "catalog-a"}}
        second = {**run_document(task="case-0002", number=2), "benchmark_case": {
            "id": "case-0002", "split": "development", "dataset_fingerprint": "dataset-one", "catalog_fingerprint": "catalog-b"}}
        result = summarize([{"run": first}, {"run": second}])
        self.assertFalse(result["mixed_configuration"])
        self.assertEqual(result["pooled_success_rate"], 1)
        heldout = {**second, "benchmark_case": {**second["benchmark_case"], "split": "heldout"}}
        result = summarize([{"run": first}, {"run": heldout}])
        self.assertTrue(result["mixed_configuration"])
        self.assertIsNone(result["pooled_success_rate"])

    def test_latest_context_mode_is_forwarded_and_expanded_ids_are_discovered(self):
        doc = run_document(task="case-0001")
        doc["runtime"]["context_mode"] = "latest_observation"
        doc["benchmark_case"] = {"id": "case-0001", "split": "development", "dataset_fingerprint": "version-one"}
        calls = []

        def handler(request):
            calls.append(request)
            if request.url.path == "/api/tasks":
                return httpx.Response(200, json={"tasks": []})
            if request.url.path == "/api/cases":
                return httpx.Response(200, json={"cases": [{"id": "case-0001"}]})
            if request.method == "POST":
                self.assertEqual(json.loads(request.content)["context_mode"], "latest_observation")
                return httpx.Response(201, json=doc)
            return httpx.Response(200, json={"run": doc, "events": []})

        with tempfile.TemporaryDirectory() as directory, self.client(handler) as client:
            output = Path(directory) / "batch"
            state = run_batch("http://localhost:8775", ["case-0001"], 1, output,
                              context_mode="latest_observation", client=client)
            self.assertEqual(state["budgets"]["context_mode"], "latest_observation")
            self.assertEqual(state["attempts"][0]["run"]["benchmark_case"], doc["benchmark_case"])
            self.assertEqual([r.method for r in calls], ["GET", "GET", "POST", "GET"])

    def test_failed_and_successful_runs_both_export_screenshots_and_receipts(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "batch"
            posted = []
            docs = {}
            # PNG signature is all that the collector needs; no browser is run.
            png = b"\x89PNG\r\n\x1a\nmock-image"

            def handler(request):
                path = request.url.path
                if path == "/api/tasks":
                    return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
                if request.method == "POST":
                    posted.append(request)
                    receipt = json.loads((output / "batch.json").read_text())
                    current = receipt["attempts"][len(posted) - 1]
                    self.assertEqual(current["submission_state"], "submitting")
                    self.assertTrue((output / current["folder"] / "request.json").is_file())
                    doc = run_document(number=len(posted), success=len(posted) != 1)
                    docs[doc["run_id"]] = doc
                    return httpx.Response(201, json={**doc, "status": "pending"})
                run_id = path.split("/")[3]
                doc = docs[run_id]
                if path.endswith("/export"):
                    return httpx.Response(200, json={"run": doc, "events": [{"type": "observation", "payload": {
                        "step": 0, "observation": {"text": "mock <script>unsafe</script>", "screenshot": "obs-0001.png"}}}]})
                if path.endswith(".png"):
                    return httpx.Response(200, content=png)
                return httpx.Response(200, json=doc)

            with self.client(handler) as client:
                result = run_batch("http://127.0.0.1:8775", ["task-one"], 2, output, client=client, sleep=lambda _: None)
            self.assertEqual(len(posted), 2)
            self.assertEqual(result["statistics"]["failures"], 1)
            self.assertEqual(result["statistics"]["successes"], 1)
            self.assertEqual(result["status"], "completed")
            for attempt in result["attempts"]:
                folder = output / attempt["folder"]
                self.assertEqual((folder / "screenshots/obs-0001.png").read_bytes(), png)
                self.assertEqual(json.loads((folder / "result.json").read_text())["result"], attempt["run"]["result"])
            report = (output / "index.html").read_text()
            self.assertIn("&lt;script&gt;unsafe&lt;/script&gt;", report)
            self.assertNotIn("<script>unsafe</script>", report)
            with zipfile.ZipFile(output / "evidence.zip") as archive:
                self.assertIsNone(archive.testzip())
                manifest = json.loads(archive.read("manifest.json"))["files"]
                for item in manifest:
                    self.assertEqual(hashlib.sha256(archive.read(item["path"])).hexdigest(), item["sha256"])

    def test_uncertain_post_is_not_retried_and_future_attempt_remains_unstarted(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "batch"
            posted = []

            def handler(request):
                if request.method == "GET":
                    return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
                posted.append(request)
                raise httpx.ReadTimeout("a-private-url-and-credential", request=request)

            with self.client(handler) as client, self.assertRaises(BenchmarkError):
                run_batch("http://127.0.0.1:8775", ["task-one"], 2, output, client=client)
            self.assertEqual(len(posted), 1)
            state = json.loads((output / "batch.json").read_text())
            self.assertEqual(state["status"], "stopped")
            self.assertEqual([a["submission_state"] for a in state["attempts"]], ["unknown", "not_started"])
            self.assertNotIn("private-url", json.dumps(state))
            self.assertTrue((output / "evidence.zip").is_file())

    def test_missing_screenshot_marks_collection_incomplete_and_retains_result(self):
        doc = run_document()

        def handler(request):
            if request.url.path == "/api/tasks":
                return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
            if request.url.path.endswith("/export"):
                return httpx.Response(200, json={"run": doc, "events": [{"type": "observation", "payload": {
                    "observation": {"screenshot": "obs-0001.png"}}}]})
            if request.url.path.endswith(".png"):
                return httpx.Response(404)
            return httpx.Response(201, json=doc)

        with tempfile.TemporaryDirectory() as directory, self.client(handler) as client:
            output = Path(directory) / "batch"
            state = run_batch("http://localhost:8775", ["task-one"], 1, output, client=client)
            self.assertEqual(state["statistics"]["collection_errors"], 1)
            self.assertTrue((output / state["attempts"][0]["folder"] / "trajectory.json").is_file())
            self.assertTrue(state["attempts"][0]["run"]["result"]["success"])

    def test_interruption_preserves_original_run_id_and_never_creates_replacement(self):
        pending = run_document(status="running", success=None)
        posts = []

        def handler(request):
            if request.url.path == "/api/tasks":
                return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
            if request.method == "POST":
                posts.append(request)
                return httpx.Response(201, json=pending)
            return httpx.Response(200, json={"run": pending, "events": []})

        def interrupt(_):
            raise KeyboardInterrupt()

        with tempfile.TemporaryDirectory() as directory, self.client(handler) as client:
            output = Path(directory) / "batch"
            with self.assertRaises(KeyboardInterrupt):
                run_batch("http://localhost:8775", ["task-one"], 2, output, client=client, sleep=interrupt)
            state = json.loads((output / "batch.json").read_text())
            self.assertEqual(state["status"], "interrupted")
            self.assertEqual(state["attempts"][0]["run_id"], pending["run_id"])
            self.assertEqual(len(posts), 1)
            self.assertEqual(state["attempts"][1]["submission_state"], "not_started")

    def test_wrong_source_is_retained_but_cannot_be_scored_as_live_model(self):
        doc = {**run_document(), "source": "api_mock_browser_contract"}
        posts = []

        def handler(request):
            if request.url.path == "/api/tasks":
                return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
            if request.method == "POST":
                posts.append(request)
                return httpx.Response(201, json=doc)
            return httpx.Response(200, json={"run": doc, "events": []})

        with tempfile.TemporaryDirectory() as directory, self.client(handler) as client:
            output = Path(directory) / "batch"
            with self.assertRaisesRegex(BenchmarkError, "execution source"):
                run_batch("http://localhost:8775", ["task-one"], 1, output, client=client)
            state = json.loads((output / "batch.json").read_text())
            self.assertEqual(state["attempts"][0]["run_id"], doc["run_id"])
            self.assertEqual(state["statistics"]["eligible_model_attempts"], 0)
            self.assertEqual(state["statistics"]["excluded_non_model_runs"], 1)
            self.assertEqual(len(posts), 1)

    def test_fixed_action_batch_is_collected_without_model_scoring(self):
        doc = {**run_document(), "source": "browser_smoke", "model": {"provider": "browser_smoke", "name": None}}
        specs = []

        def handler(request):
            if request.url.path == "/api/tasks":
                return httpx.Response(200, json={"tasks": [{"id": "task-one"}]})
            if request.method == "POST":
                specs.append(json.loads(request.content))
                return httpx.Response(201, json=doc)
            return httpx.Response(200, json={"run": doc, "events": []})

        with tempfile.TemporaryDirectory() as directory, self.client(handler) as client:
            output = Path(directory) / "batch"
            state = run_batch("http://localhost:8775", ["task-one"], 1, output,
                              provider="browser_smoke", client=client)
            self.assertEqual(specs[0]["provider"], "browser_smoke")
            self.assertEqual(state["budgets"]["provider"], "browser_smoke")
            self.assertEqual(state["attempts"][0]["run"]["source"], "browser_smoke")
            self.assertEqual(state["statistics"]["eligible_model_attempts"], 0)
            self.assertEqual(state["statistics"]["excluded_non_model_runs"], 1)


if __name__ == "__main__":
    unittest.main()
