"""Contract checks for the read-only external benchmark slice."""
import csv
import json
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import external_benchmark as benchmark  # noqa: E402


class ExternalBenchmarkTests(unittest.TestCase):
    def setUp(self):
        self.data = benchmark.load_manifest(benchmark.DEFAULT_MANIFEST)

    def test_manifest_has_thirty_read_only_tasks_across_sites(self):
        self.assertEqual(len(self.data["tasks"]), 30)
        self.assertEqual(len({task["id"] for task in self.data["tasks"]}), 30)
        self.assertGreaterEqual(len({task["site"] for task in self.data["tasks"]}), 8)
        self.assertEqual(benchmark.validate(self.data), [])
        self.assertTrue(all(task["read_only"] for task in self.data["tasks"]))

    def test_pilot_is_a_valid_subset_of_the_thirty_task_slice(self):
        pilot = benchmark.load_manifest(ROOT / "benchmarks" / "external" / "pilot-15.json")
        self.assertEqual(len(pilot["tasks"]), 15)
        self.assertEqual(benchmark.validate(pilot), [])
        base_ids = {task["id"] for task in self.data["tasks"]}
        self.assertTrue({task["id"] for task in pilot["tasks"]} <= base_ids)

    def test_origins_are_https_and_deduplicated(self):
        origins = benchmark.origins(self.data)
        self.assertEqual(len(origins), len(set(origins)))
        self.assertTrue(all(origin.startswith("https://") for origin in origins))
        self.assertIn("https://support.apple.com", origins)

    def test_every_task_has_a_gradable_rubric(self):
        for task in self.data["tasks"]:
            self.assertGreater(len(task["rubric"]), 30, task["id"])
            self.assertIn(task["stability"], {"stable", "snapshot"})
            self.assertEqual(task["grading"], "manual")

    def test_summary_keeps_unknowns_out_of_the_known_denominator(self):
        rows = [
            {"id": "Cambridge Dictionary--0", "success": "true", "notes": ""},
            {"id": "Cambridge Dictionary--1", "success": "true", "notes": ""},
            {"id": "Cambridge Dictionary--2", "success": "false", "notes": ""},
            {"id": "Cambridge Dictionary--6", "success": "unknown", "notes": "captcha"},
        ]
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "grades.csv"
            with path.open("w", newline="", encoding="utf-8") as handle:
                writer = csv.DictWriter(handle, fieldnames=["id", "success", "notes"])
                writer.writeheader()
                writer.writerows(rows)
            result = benchmark.summary(self.data, path)
        self.assertEqual(result["attempted"], 4)
        self.assertEqual(result["passed"], 2)
        self.assertEqual(result["failed"], 1)
        self.assertEqual(result["unknown"], 1)
        self.assertEqual(result["known"], 3)
        self.assertAlmostEqual(result["known_success_rate"], 2 / 3, places=4)
        self.assertAlmostEqual(result["coverage"], 4 / 30, places=4)
        low, high = result["wilson_95"]
        self.assertLess(low, 2 / 3)
        self.assertGreater(high, 2 / 3)
        self.assertEqual(len(result["missing_grades"]), 26)

    def test_summary_rejects_an_unknown_id_and_bad_grade(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "grades.csv"
            path.write_text("id,success,notes\nnot-a-task,true,\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                benchmark.summary(self.data, path)
            path.write_text("id,success,notes\nCambridge Dictionary--0,maybe,\n", encoding="utf-8")
            with self.assertRaises(ValueError):
                benchmark.summary(self.data, path)

    def test_plan_is_deterministic_for_a_small_selection(self):
        argv = ["--plan", "--task-id", "Cambridge Dictionary--0"]
        import contextlib
        import io
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = benchmark.main(argv)
        self.assertEqual(code, 0)
        text = output.getvalue()
        self.assertIn("WEBAGENT_ALLOWED_ORIGINS=", text)
        self.assertIn("Cambridge Dictionary--0", text)
        self.assertIn("python -m webagent serve", text)


if __name__ == "__main__":
    unittest.main()
