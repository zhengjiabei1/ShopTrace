"""Pinned-source and evidence-boundary checks for upstream baseline handoff."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts import upstream_baseline as b


class UpstreamBaselineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.upstream = self.root / "upstream"
        self.upstream.mkdir()
        self.run_dir = self.root / "baseline-run"
        self.train_ids = [53, 56, *range(100, 125)]
        files = {
            "scripts/evaluate_shop_benchmark.py": "# Native entrypoint fixture; never makes model requests.\n",
            "src/shopping_grpo/__init__.py": "",
            "src/shopping_grpo/evaluation/__init__.py": "",
            "src/shopping_grpo/evaluation/rollout.py": "# pinned rollout fixture\n",
            "src/shopping_grpo/evaluation/summary.py": "# pinned summary fixture\n",
            "src/shopping_grpo/tools.json": '{"fixture": true}\n',
            "data/grpo/train.jsonl": self._tasks(self.train_ids),
            "data/grpo/validation.jsonl": self._tasks([54]),
            "data/evaluation/tasks.jsonl": self._tasks([55]),
        }
        for name, content in files.items():
            target = self.upstream / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
        self._git("init", "-q")
        self._git("add", ".")
        self._git("commit", "-q", "-m", "Pinned test source")
        self.commit = self._git("rev-parse", "HEAD")
        self.lock = {
            "reproduction": {
                "repository": "https://github.com/example/upstream-baseline",
                "commit": self.commit,
                "evaluation": {
                    "path": "data/evaluation/tasks.jsonl",
                    "sha256": hashlib.sha256(
                        (self.upstream / "data/evaluation/tasks.jsonl").read_bytes()
                    ).hexdigest(),
                },
            },
        }
        self.lock_patch = patch.object(b, "load_lock", return_value=self.lock)
        self.lock_patch.start()
        self.addCleanup(self.lock_patch.stop)

    @staticmethod
    def _tasks(ids):
        return "".join(json.dumps({"task_id": task_id, "instruction": f"Need {task_id}"}) + "\n" for task_id in ids)

    def _git(self, *args):
        result = subprocess.run(
            ["git", "-c", "user.name=Baseline Tests", "-c", "user.email=baseline-tests@local", *args],
            cwd=self.upstream, capture_output=True, text=True, timeout=15, check=True,
        )
        return result.stdout.strip()

    def _plan(self, ids=None):
        return b.create_plan(self.upstream, self.run_dir, [53] if ids is None else ids)

    def test_clean_pinned_plan_can_be_revalidated_without_execution(self):
        plan = self._plan()
        self.assertIsInstance(plan, dict)
        self.assertIsInstance(b.validate_plan(self.run_dir), dict)
        self.assertTrue((self.run_dir / "plan.json").is_file())
        self.assertTrue((self.run_dir / "dev-tasks.jsonl").is_file())
        self.assertFalse((self.run_dir / "run.json").exists())

    def test_wrong_commit_and_dirty_source_are_rejected_before_planning(self):
        wrong_lock = copy.deepcopy(self.lock)
        wrong_lock["reproduction"]["commit"] = "f" * 40
        with patch.object(b, "load_lock", return_value=wrong_lock):
            with self.assertRaises(ValueError):
                self._plan()
        target = self.upstream / "src/shopping_grpo/evaluation/rollout.py"
        target.write_text("# uncommitted behavior change\n", encoding="utf-8")
        with self.assertRaises(ValueError):
            self._plan()

    def test_source_fingerprint_catches_change_hidden_from_git_status(self):
        self._plan()
        name = "src/shopping_grpo/evaluation/rollout.py"
        self._git("update-index", "--assume-unchanged", name)
        (self.upstream / name).write_text("# changed execution behavior\n", encoding="utf-8")
        self.assertEqual(self._git("status", "--porcelain"), "")
        with self.assertRaises(ValueError):
            b.validate_plan(self.run_dir)

    def test_missing_native_source_is_rejected(self):
        self._plan()
        (self.upstream / "src/shopping_grpo/evaluation/summary.py").unlink()
        with self.assertRaises(ValueError):
            b.validate_plan(self.run_dir)

    def test_development_selection_excludes_validation_test_unknown_and_duplicates(self):
        selections = ([54], [55], [999], [53, 53], [], self.train_ids[:21])
        for ids in selections:
            with self.subTest(ids=ids):
                with self.assertRaises(ValueError):
                    b.create_plan(self.upstream, self.root / f"invalid-{len(ids)}-{ids[:1]}", ids)

    def test_frozen_tasks_and_protocol_tampering_are_rejected(self):
        self._plan()
        tasks = self.run_dir / "dev-tasks.jsonl"
        original_tasks = tasks.read_bytes()
        tasks.write_text(self._tasks([56]), encoding="utf-8")
        with self.assertRaises(ValueError):
            b.validate_plan(self.run_dir)
        tasks.write_bytes(original_tasks)
        path = self.run_dir / "plan.json"
        plan = json.loads(path.read_text(encoding="utf-8"))
        plan["protocol"]["id"] = "tampered-protocol"
        path.write_text(json.dumps(plan), encoding="utf-8")
        with self.assertRaises(ValueError):
            b.validate_plan(self.run_dir)

    def test_missing_services_block_without_disclosing_credentials_or_running(self):
        self._plan()
        environment = {"PATH": os.environ.get("PATH", ""), "LLM_API_KEY": "private-baseline-test-key"}
        with patch.dict(os.environ, environment, clear=True):
            report = b.preflight(self.run_dir, python_executable=sys.executable)
            self.assertEqual(report["status"], "blocked")
            self.assertTrue(report["blockers"])
            self.assertFalse(report["model_called"])
            self.assertFalse(report["environment_mutated"])
            self.assertNotIn(environment["LLM_API_KEY"], json.dumps(report))
            with patch.object(b, "_run_native_process") as native:
                execution = b.execute(self.run_dir, python_executable=sys.executable)
            native.assert_not_called()
        self.assertEqual(execution["status"], "blocked")

    def test_configured_service_values_are_not_saved_and_default_check_does_not_probe(self):
        environment = {
            "SHOPSIM_BASE_URL": "https://private-environment.example",
            "LLM_BASE_URL": "https://private-model.example/v1",
            "LLM_API_KEY": "private-configured-model-key",
        }
        with patch.dict(os.environ, environment), patch.object(b, "_probe") as probe:
            self._plan()
            report = b.preflight(self.run_dir, python_executable=sys.executable)
        probe.assert_not_called()
        rendered = (self.run_dir / "plan.json").read_text() + json.dumps(report)
        for secret_value in environment.values():
            self.assertNotIn(secret_value, rendered)
        self.assertFalse(report["model_called"])
        self.assertFalse(report["environment_mutated"])

    def test_failed_native_process_cannot_claim_model_success(self):
        self._plan()
        ready = {"status": "ready_to_attempt", "blockers": [], "model_called": False, "environment_mutated": False}
        failed = subprocess.CompletedProcess(["fixture-native-process"], 7, stdout="", stderr="fixture failure")
        with patch.object(b, "preflight", return_value=ready), patch.object(b, "_run_native_process", return_value=failed) as native:
            report = b.execute(self.run_dir, python_executable=sys.executable)
        native.assert_called_once()
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["child_exit_code"], 7)
        self._assert_no_success_metric(report)

    def _assert_no_success_metric(self, value):
        if isinstance(value, dict):
            for key, item in value.items():
                if key in {"success_rate", "strict_success_rate", "purchase_success_rate", "strict_success"}:
                    self.assertTrue(item is None or item is False)
                self._assert_no_success_metric(item)
        elif isinstance(value, list):
            for item in value:
                self._assert_no_success_metric(item)

    def test_existing_plan_or_execution_output_is_not_overwritten(self):
        self.run_dir.mkdir()
        with self.assertRaises(ValueError):
            self._plan()
        self.run_dir.rmdir()
        self._plan()
        existing = self.run_dir / "run.json"
        existing.write_text('{"retain": true}\n', encoding="utf-8")
        with patch.object(b, "_run_native_process") as native:
            with self.assertRaises(ValueError):
                b.execute(self.run_dir, python_executable=sys.executable)
        native.assert_not_called()
        self.assertEqual(json.loads(existing.read_text()), {"retain": True})

    def test_relative_venv_interpreter_keeps_its_path_when_child_changes_directory(self):
        plan = self._plan()
        interpreter = self.root / "venv/bin/python"
        interpreter.parent.mkdir(parents=True)
        interpreter.symlink_to(sys.executable)
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            completed = subprocess.CompletedProcess([], 0, stdout="", stderr="")
            with patch.object(b, "validate_plan", return_value=plan), patch.object(b.subprocess, "run", return_value=completed) as child:
                b._run_native_process(self.run_dir, "venv/bin/python")
            self.assertEqual(child.call_args.args[0][0], os.path.join(os.path.realpath(self.root), "venv", "bin", "python"))
            self.assertEqual(child.call_args.kwargs["cwd"], Path(os.path.realpath(self.upstream)))
            self.assertNotIn("--api-key", child.call_args.args[0])
        finally:
            os.chdir(previous)


if __name__ == "__main__":
    unittest.main()
