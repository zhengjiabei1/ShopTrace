"""Offline data-publication checks; these fixtures do not execute a model."""
import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location("shoptrace_portal_builder", Path(__file__).with_name("build_report.py"))
builder = importlib.util.module_from_spec(spec)
spec.loader.exec_module(builder)


class PortalPublicationTests(unittest.TestCase):
    def make_batch(self, root, ident="mock-id"):
        folder = root / "attempts" / "mock" / "attempt-01"
        (folder / "screenshots").mkdir(parents=True)
        (folder / "screenshots" / "obs-0001.png").write_bytes(b"\x89PNG\r\n\x1a\nfixture")
        run = {"run_id": ident, "task_id": "fixture", "source": "api_mock_browser_contract", "status": "failed",
               "model": {"name": "mock"}, "result": {"success": None, "reason": "not_finished"},
               "model_calls": 1, "usage": {"input_tokens": None, "output_tokens": None}, "runtime": {"context_mode": "full_history"}}
        trajectory = {"run": run, "events": [{"type": "observation", "payload": {"step": 0, "observation": {
            "screenshot": "obs-0001.png", "url": "http://localhost/sandbox/", "text": "Visible fixture"}}},
            {"type": "error", "payload": {"step": 0, "kind": "fixture", "message": "deliberate failure"}}]}
        (folder / "trajectory.json").write_text(json.dumps(trajectory))
        (folder / "result.json").write_text(json.dumps(run))
        state = {"status": "completed", "attempts": [{"folder": str(folder.relative_to(root)), "run": run, "repeat": 1}]}
        (root / "batch.json").write_text(json.dumps(state))
        return folder, state

    def test_unknown_mock_result_and_full_saved_evidence_are_preserved(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "batch"; batch.mkdir(); folder, state = self.make_batch(batch)
            dest = root / "portal"
            report = builder.build([("development", "full_history", batch)], dest)
            attempt = report["suites"][0]["attempts"][0]
            self.assertIsNone(attempt["success"])
            self.assertEqual(attempt["source"], "api_mock_browser_contract")
            self.assertEqual(attempt["steps"][0]["error"][0]["message"], "deliberate failure")
            self.assertEqual((dest / attempt["trajectory_url"]).read_bytes(), (folder / "trajectory.json").read_bytes())
            self.assertTrue((dest / attempt["steps"][0]["screenshot"]).is_file())
            self.assertTrue((dest / "manifest.json").is_file())

    def test_failed_rebuild_preserves_previous_report_and_images(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "batch"; batch.mkdir(); folder, state = self.make_batch(batch)
            dest = root / "portal"
            builder.build([("stability", "", batch)], dest)
            previous = (dest / "report.json").read_bytes()
            state["status"] = "running"; (batch / "batch.json").write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                builder.build([("stability", "", batch)], dest)
            self.assertEqual((dest / "report.json").read_bytes(), previous)
            self.assertTrue(next((dest / "evidence").rglob("*.png")).is_file())

    def test_path_escape_and_exact_credential_stop_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "batch"; batch.mkdir(); folder, state = self.make_batch(batch)
            dest = root / "portal"
            state["attempts"][0]["folder"] = "../outside"
            (batch / "batch.json").write_text(json.dumps(state))
            with self.assertRaises(ValueError):
                builder.build([("stability", "", batch)], dest)
            folder, state = self.make_batch_reusing(batch)
            (folder / "result.json").write_text('"fake-publication-test-secret"')
            with patch.dict(os.environ, {"LLM_API_KEY": "fake-publication-test-secret"}):
                with self.assertRaises(ValueError):
                    builder.build([("stability", "", batch)], dest)

    def test_append_keeps_prior_evidence_and_rejects_duplicate_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "first"; batch.mkdir()
            folder, state = self.make_batch(batch)
            dest = root / "portal"
            old = builder.build([("english", "", batch)], dest)
            prior = old["suites"][0]["attempts"][0]
            trace = (dest / prior["trajectory_url"]).read_bytes()
            with self.assertRaises(ValueError):
                builder.build([("procurement-dev", "", batch)], dest, preserve=True)
            self.assertEqual(json.loads((dest / "report.json").read_text()), old)
            more = root / "more"; more.mkdir(); self.make_batch(more, "new-id")
            new = builder.build([("procurement-dev", "", more)], dest, preserve=True)
            self.assertEqual(new["suites"][0], old["suites"][0])
            self.assertEqual((dest / prior["trajectory_url"]).read_bytes(), trace)
            self.assertEqual(len(new["suites"]), 2)
            self.assertNotEqual(new["suites"][1]["attempts"][0]["trajectory_url"], prior["trajectory_url"])

    def test_procurement_conditions_and_observation_notes_are_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "batch"; batch.mkdir()
            folder, state = self.make_batch(batch)
            path = folder / "trajectory.json"; trace = json.loads(path.read_text())
            trace["run"]["runtime"].update(context_mode="page_memory", recovery="adaptive", max_steps=70)
            trace["run"]["benchmark_case"] = {"fault": "ack_lost", "seed": 7, "split": "development"}
            trace["run"]["result"]["metrics"] = {"duplicates": 0, "fault_triggered": True}
            trace["events"].append({"type": "memory", "payload": {"step": 0, "pages": 2}})
            trace["events"].append({"type": "recovery", "payload": {"step": 0, "decision": "check_cart"}})
            path.write_text(json.dumps(trace))
            report = builder.build([("procurement-dev", "", batch)], root / "portal")
            row = report["suites"][0]["attempts"][0]
            self.assertEqual(row["arm"], "adaptive")
            self.assertEqual(row["conditions"], {"fault": "ack_lost", "seed": 7, "split": "development"})
            self.assertEqual(row["context_mode"], "page_memory")
            self.assertTrue(row["metrics"]["fault_triggered"])
            self.assertEqual([note["type"] for note in row["steps"][0]["notes"]], ["memory", "recovery"])

    def test_interrupted_initial_zero_time_is_not_a_measured_duration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); batch = root / "batch"; batch.mkdir()
            folder, state = self.make_batch(batch)
            path = folder / "trajectory.json"; trace = json.loads(path.read_text())
            trace["run"].update(termination_reason="process_interrupted", elapsed_seconds=0)
            path.write_text(json.dumps(trace))
            report = builder.build([("procurement-pilot", "", batch)], root / "portal")
            row = report["suites"][0]["attempts"][0]
            self.assertIsNone(row["elapsed_seconds"])
            self.assertIn("unavailable", row["time_note"])
            kept = json.loads((root / "portal" / row["trajectory_url"]).read_text())
            self.assertEqual(kept["run"]["elapsed_seconds"], 0)

    def make_batch_reusing(self, root):
        import shutil
        shutil.rmtree(root / "attempts")
        return self.make_batch(root)


if __name__ == "__main__":
    unittest.main()
