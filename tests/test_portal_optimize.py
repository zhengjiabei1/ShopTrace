"""Offline portal image optimization and manifest rewrite."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
PIL = importlib.util.find_spec("PIL") is not None


@unittest.skipUnless(PIL, "Pillow is required for the portal optimizer")
class PortalOptimizeTests(unittest.TestCase):
    def test_pngs_become_webp_and_traces_are_gzipped(self):
        from PIL import Image
        from scripts.optimize_portal import optimize

        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            evidence = root / "evidence" / "group-00" / "attempt-001"
            (evidence / "screenshots").mkdir(parents=True)
            buffer = io.BytesIO()
            Image.new("RGB", (1280, 900), "white").save(buffer, "PNG")
            (evidence / "screenshots" / "obs-0001.png").write_bytes(buffer.getvalue())
            (evidence / "trajectory.json").write_text(
                json.dumps({"events": [{"screenshot": "obs-0001.png"}]}), encoding="utf-8")
            report = {"suites": [{"id": "demo", "attempts": [{
                "id": "a1", "trajectory_url": "evidence/group-00/attempt-001/trajectory.json",
                "steps": [{"step": 0, "screenshot": "evidence/group-00/attempt-001/screenshots/obs-0001.png"}],
            }]}]}
            (root / "report.json").write_text(json.dumps(report), encoding="utf-8")
            (root / "report-data.js").write_text("window.SHOPTRACE_REPORT = " + json.dumps(report) + ";\n", encoding="utf-8")
            (root / "manifest.json").write_text("{}\n", encoding="utf-8")

            result = optimize(root, max_width=640, quality=70, gzip_trajectories=True)

            self.assertEqual(result["converted"], 1)
            self.assertFalse((evidence / "screenshots" / "obs-0001.png").exists())
            self.assertTrue((evidence / "screenshots" / "obs-0001.webp").is_file())
            updated = json.loads((root / "report.json").read_text(encoding="utf-8"))
            attempt = updated["suites"][0]["attempts"][0]
            self.assertTrue(attempt["steps"][0]["screenshot"].endswith(".webp"))
            self.assertTrue(attempt["trajectory_url"].endswith(".json.gz"))
            self.assertTrue((evidence / "trajectory.json.gz").is_file())
            self.assertFalse((evidence / "trajectory.json").exists())
            trace = json.loads(__import__("gzip").decompress((evidence / "trajectory.json.gz").read_bytes()))
            self.assertTrue(trace["events"][0]["screenshot"].endswith(".webp"))
            manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
            paths = {item["path"] for item in manifest["files"]}
            self.assertIn("evidence/group-00/attempt-001/screenshots/obs-0001.webp", paths)
            for item in manifest["files"]:
                raw = (root / item["path"]).read_bytes()
                self.assertEqual(item["bytes"], len(raw))
                self.assertEqual(item["sha256"], hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    unittest.main()
