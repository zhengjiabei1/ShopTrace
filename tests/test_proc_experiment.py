"""Experiment transport fixtures; these checks never call a model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx

from webagent.experiment import (BENCH, PLAN, BenchmarkError, compare, digest,
                                 report, run_plan, select, validate_plan)

SOURCE = "a" * 64
PNG = b"\x89PNG\r\n\x1a\nfixture"


def plan(cases=None):
    return {"schema_version": PLAN, "name": "fixture", "split": "development",
            "benchmark": BENCH, "model": "test-model", "budgets": {
                "max_steps": 70, "max_seconds": 180, "max_output_tokens": 512},
            "cases": cases or [{"task_id": "procure-dev-01", "seed": 7, "fault": "ack_lost",
                                "recovery": "verify", "context_mode": "page_memory"}]}


class Workbench:
    def __init__(self):
        self.posts = []
        self.runs = {}
        self.outcomes = [True]
        self.unknown_post = False
        self.active = False
        self.missing = False
        self.model = "test-model"
        self.source = SOURCE
        self.prompt = "prompt"
        self.missing_use = False

    def handle(self, req):
        path = req.url.path
        if path == "/api/status":
            return httpx.Response(200, json={"model_name": self.model, "source_fingerprint": self.source})
        if path == "/api/procurement":
            return httpx.Response(200, json={"tasks": [{"id": "procure-dev-01"}, {"id": "procure-dev-02"}]})
        if req.method == "POST":
            spec = json.loads(req.content)
            self.posts.append(spec)
            if self.unknown_post:
                raise httpx.ReadTimeout("private-url-and-token", request=req)
            n = len(self.posts)
            run_id = f"{n:032x}"
            success = self.outcomes[(n - 1) % len(self.outcomes)]
            run = {"run_id": run_id, "task_id": spec["task_id"], "source": "browser_model",
                   "source_fingerprint": self.source, "status": "completed", "query": "Test shopping goal",
                   "model": {"provider": "api", "name": self.model}, "runtime": {
                       k: spec[k] for k in ("max_steps", "max_seconds", "max_output_tokens", "context_mode", "recovery")},
                   "benchmark_case": {"id": spec["task_id"], "split": "development", "seed": spec["seed"],
                                      "fault": spec["fault"], "benchmark": BENCH, "catalog_fingerprint": "b" * 64,
                                      "dataset_fingerprint": "c" * 64, "benchmark_fingerprint": "d" * 64},
                   "protocol_version": "fixture-v1", "prompt_hash": self.prompt, "tools_hash": "tools",
                   "result": {"success": success, "reason": "fixture", "metrics": {
                       "duplicates": 0 if success else 1, "fault_triggered": spec["fault"] != "none",
                       "cart_checks": 2, "duplicate_adds": 0, "uncertain_adds": 1, "add_attempts": 2}},
                   "usage": {"input_tokens": None if self.missing_use else 100, "output_tokens": 20},
                   "steps": 2, "model_calls": 2, "elapsed_seconds": 1.2}
            self.runs[run_id] = run
            return httpx.Response(201, json={**run, "status": "running"} if self.active else run)
        run_id = path.split("/")[3]
        run = self.runs[run_id]
        if path.endswith("/export"):
            return httpx.Response(200, json={"run": run, "events": [
                {"type": "observation", "payload": {"step": 0, "observation": {"text": "Visible page", "screenshot": "obs-0001.png"}}},
                {"type": "recovery", "payload": {"decision": "check_cart"}}]})
        if path.endswith(".png"):
            return httpx.Response(404) if self.missing else httpx.Response(200, content=PNG)
        return httpx.Response(200, json=run)

    def client(self):
        return httpx.Client(base_url="http://127.0.0.1:8775", transport=httpx.MockTransport(self.handle))


class ExperimentTests(unittest.TestCase):
    def setUp(self):
        self.patch = patch("webagent.experiment.source_snapshot", return_value={"commit": "fixture", "files": {"fixture.py": "hash"}})
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def test_plan_rejects_split_leakage_duplicates_and_unbounded_budgets(self):
        tests = []
        wrong = plan()
        wrong["cases"][0]["task_id"] = "procure-test-01"
        tests.append(wrong)
        dup = plan()
        dup["cases"] *= 2
        tests.append(dup)
        huge = plan()
        huge["budgets"]["max_steps"] = 1000
        tests.append(huge)
        bool_seed = plan()
        bool_seed["cases"][0]["seed"] = True
        tests.append(bool_seed)
        for value in tests:
            with self.subTest(value=value), self.assertRaises(BenchmarkError):
                validate_plan(value)

    def test_all_outcomes_saved_and_offline_report_is_byte_identical(self):
        env = Workbench()
        env.outcomes = [False, True, None]
        cases = [{**plan()["cases"][0], "seed": seed} for seed in (7, 8, 9)]
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            doc = run_plan(plan(cases), out, client=client, poll=0)
            self.assertEqual(doc["summary"]["passes"], 1)
            self.assertEqual(doc["summary"]["failures"], 1)
            self.assertEqual(doc["summary"]["unknown"], 1)
            self.assertEqual(doc["summary"]["confirmed_pass_rate"], 1 / 3)
            self.assertEqual(doc["summary"]["coverage"], 2 / 3)
            self.assertEqual(doc["summary"]["fault_triggered"], 3)
            for row in doc["rows"]:
                folder = out / "attempts" / row["id"]
                self.assertEqual((folder / "screenshots/obs-0001.png").read_bytes(), PNG)
            before = {name: (out / name).read_bytes() for name in ("report.json", "episodes.csv", "report.md", "batch.json")}
            report(out)
            self.assertEqual(before, {name: (out / name).read_bytes() for name in before})
            self.assertTrue((out / "evidence.zip").is_file())

    def test_resume_does_not_repeat_failed_completed_attempts(self):
        env = Workbench()
        env.outcomes = [False]
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            run_plan(plan(), out, client=client, poll=0)
            again = run_plan(plan(), out, resume=True, client=client, poll=0)
            self.assertEqual(len(env.posts), 1)
            self.assertEqual(again["summary"]["failures"], 1)

    def test_uncertain_submission_is_never_resent(self):
        env = Workbench()
        env.unknown_post = True
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            with self.assertRaises(BenchmarkError):
                run_plan(plan(), out, client=client, poll=0)
            with self.assertRaisesRegex(BenchmarkError, "uncertain"):
                run_plan(plan(), out, client=client, resume=True, poll=0)
            self.assertEqual(len(env.posts), 1)
            self.assertNotIn("private-url", (out / "experiment.json").read_text())
            self.assertEqual(json.loads((out / "experiment.json").read_text())["attempts"][0]["state"], "unknown")

    def test_resume_after_interruption_reuses_acknowledged_run(self):
        env = Workbench()
        env.active = True
        def stop(_):
            raise KeyboardInterrupt()
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            with self.assertRaises(KeyboardInterrupt):
                run_plan(plan(), out, client=client, sleep=stop, poll=0)
            doc = run_plan(plan(), out, client=client, resume=True, poll=0)
            self.assertEqual(len(env.posts), 1)
            self.assertEqual(doc["summary"]["passes"], 1)

    def test_missing_screenshot_recollects_original_run(self):
        env = Workbench()
        env.missing = True
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            with self.assertRaisesRegex(BenchmarkError, "Screenshot"):
                run_plan(plan(), out, client=client, poll=0)
            doc = report(out)
            self.assertEqual(doc["summary"]["passes"], 1)
            self.assertEqual(doc["summary"]["evidence_complete"], 0)
            env.missing = False
            done = run_plan(plan(), out, client=client, resume=True, poll=0)
            self.assertEqual(done["summary"]["evidence_complete"], 1)
            self.assertEqual(len(env.posts), 1)

    def test_saved_receipt_recovers_crash_before_state_update(self):
        env = Workbench()
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            run_plan(plan(), out, client=client, poll=0)
            state = json.loads((out / "experiment.json").read_text())
            state["attempts"][0].update(state="submitting")
            state["attempts"][0].pop("run_id")
            (out / "experiment.json").write_text(json.dumps(state))
            doc = run_plan(plan(), out, client=client, resume=True, poll=0)
            self.assertEqual(len(env.posts), 1)
            self.assertEqual(doc["summary"]["passes"], 1)

    def test_resume_rejects_server_code_model_or_plan_change(self):
        env = Workbench()
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            run_plan(plan(), out, client=client, poll=0)
            env.source = "e" * 64
            with self.assertRaisesRegex(BenchmarkError, "Source or model changed"):
                run_plan(plan(), out, client=client, resume=True, poll=0)
            env.source = SOURCE
            changed = plan()
            changed["cases"][0]["recovery"] = "adaptive"
            with self.assertRaisesRegex(BenchmarkError, "Frozen plan"):
                run_plan(changed, out, client=client, resume=True, poll=0)
            self.assertEqual(len(env.posts), 1)

    def test_evidence_tampering_blocks_report_and_resume(self):
        env = Workbench()
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            out = Path(tmp) / "batch"
            doc = run_plan(plan(), out, client=client, poll=0)
            shot = out / "attempts" / doc["rows"][0]["id"] / "screenshots/obs-0001.png"
            shot.write_bytes(PNG + b"changed")
            with self.assertRaisesRegex(BenchmarkError, "manifest"):
                report(out)
            with self.assertRaisesRegex(BenchmarkError, "manifest"):
                run_plan(plan(), out, client=client, resume=True, poll=0)
            self.assertEqual(len(env.posts), 1)

    def test_missing_usage_stays_unknown(self):
        env = Workbench()
        env.missing_use = True
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            doc = run_plan(plan(), Path(tmp) / "batch", client=client, poll=0)
            self.assertIsNone(doc["summary"]["input_tokens"]["sum"])
            self.assertEqual(doc["summary"]["input_tokens"]["missing_count"], 1)

    def test_compare_checks_cases_and_environment_and_preserves_negative_control(self):
        env = Workbench()
        env.outcomes = [False]
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            doc = run_plan(plan(), Path(tmp) / "batch", client=client, poll=0)
            self.assertEqual(compare(doc, doc)["status"], "pass")
            changed = deepcopy(doc)
            changed["rows"][0]["benchmark"]["benchmark_fingerprint"] = "f" * 64
            with self.assertRaisesRegex(BenchmarkError, "benchmark identity"):
                compare(doc, changed)
            missing = deepcopy(doc)
            missing["rows"] = []
            with self.assertRaisesRegex(BenchmarkError, "complete"):
                compare(doc, missing)
            better = deepcopy(doc)
            better["rows"][0]["success"] = True
            self.assertEqual(compare(better, doc)["status"], "regression")
            unknown = deepcopy(doc)
            unknown["rows"][0]["success"] = None
            self.assertEqual(compare(doc, unknown)["status"], "blocked")

    def test_missing_model_or_source_blocks_compare_and_selection(self):
        env = Workbench()
        cases = [{**plan()["cases"][0], "recovery": arm} for arm in ("verify", "adaptive")]
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            doc = run_plan(plan(cases), Path(tmp) / "batch", client=client, poll=0)
            for field in ("source", "model"):
                with self.subTest(field=field):
                    changed = deepcopy(doc)
                    changed["rows"][0].pop(field)
                    self.assertEqual(compare(doc, changed)["status"], "blocked")
                    with self.assertRaisesRegex(BenchmarkError, "frozen source"):
                        select(changed, "recovery", plan(), Path(tmp) / "choice")

    def test_selection_retains_control_when_candidate_cost_is_unknown(self):
        env = Workbench()
        env.missing_use = True
        cases = [{**plan()["cases"][0], "recovery": arm} for arm in ("verify", "adaptive")]
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            doc = run_plan(plan(cases), Path(tmp) / "batch", client=client, poll=0)
            template = plan()
            template["split"] = "heldout"
            template["cases"][0]["task_id"] = "procure-test-01"
            choice = select(doc, "recovery", template, Path(tmp) / "choice")
            self.assertEqual(choice["selected"], "verify")
            selected = json.loads((Path(tmp) / "choice/selected-plan.json").read_text())
            self.assertEqual(selected["selection"]["decision_hash"], digest(choice))
            self.assertEqual(selected["selection"]["source_fingerprint"], SOURCE)
            with self.assertRaisesRegex(BenchmarkError, "bound"):
                run_plan(template, Path(tmp) / "heldout", client=client, poll=0)

    def test_selection_requires_complete_paired_arms(self):
        env = Workbench()
        with tempfile.TemporaryDirectory() as tmp, env.client() as client:
            doc = run_plan(plan(), Path(tmp) / "batch", client=client, poll=0)
            with self.assertRaisesRegex(BenchmarkError, "both comparison"):
                select(doc, "recovery", plan(), Path(tmp) / "choice")


if __name__ == "__main__":
    unittest.main()
