"""Checks for metric coverage, fixed denominators and comparison eligibility."""

import copy
import unittest

from lab.evaluation import compare_runs, summarize


def live_run(run_id="run-1", **updates):
    run = {
        "run_id": run_id,
        "task_id": "42",
        "mode": "shopsimulator",
        "source": "live",
        "environment_version": "shopsimulator-environment-v2.1",
        "upstream_commit": "a" * 40,
        "model": {"provider": "api", "name": "test-model", "stage": "sft"},
        "runtime": {
            "max_steps": 35,
            "max_seconds": 120,
            "max_calls": 35,
            "max_output_tokens": 512,
        },
        "strategy": "default",
        "status": "completed",
        "steps": 7,
        "model_calls": 7,
        "elapsed_seconds": 4.5,
        "usage": {"input_tokens": None, "output_tokens": None},
        "result": {
            "reward": 1.0,
            "reward_valid": True,
            "strict_success": True,
            "purchase_success": True,
        },
        "error": None,
    }
    run.update(updates)
    return run


class EvaluationTests(unittest.TestCase):
    def test_unknown_results_and_infrastructure_failures_remain_in_denominator(self):
        success = live_run("success")
        failure = live_run("task-failure", result={
            "reward": -0.85, "reward_valid": True,
            "strict_success": False, "purchase_success": False,
        })
        unknown = live_run("unknown", result={
            "reward": None, "reward_valid": False,
            "strict_success": None, "purchase_success": None,
        })
        infrastructure = live_run("infrastructure", status="failed", result={},
                                  error={"kind": "environment", "message": "offline"})
        report = summarize([success, failure, unknown, infrastructure])
        self.assertEqual(report["status"], "available")
        self.assertEqual(report["formal_run_count"], 4)
        self.assertEqual(len(report["groups"]), 1)
        group = report["groups"][0]
        metric = group["strict_success"]
        self.assertEqual(metric["numerator"], 1)
        self.assertEqual(metric["denominator"], 4)
        self.assertEqual(metric["known"], 2)
        self.assertEqual(metric["unknown"], 2)
        self.assertAlmostEqual(metric["value"], 0.25)
        self.assertEqual(group["infrastructure_errors"], 1)
        self.assertEqual(group["valid_execution_success"]["denominator"], 3)
        self.assertEqual(group["reward"]["known"], 2)

    def test_fixture_imported_scripted_and_unfinished_are_not_formal_evidence(self):
        fixture = live_run("fixture", mode="fixture", source="synthetic_fixture")
        imported = live_run("imported", source="imported")
        scripted = live_run("scripted", model={
            "provider": "scripted", "name": "fixture-driver", "stage": "other",
        })
        report = summarize([
            live_run(), fixture, imported, scripted,
            live_run("pending", status="pending"), live_run("running", status="running"),
        ])
        self.assertEqual(report["total_runs"], 6)
        self.assertEqual(report["formal_run_count"], 1)
        self.assertEqual(report["groups"][0]["strict_success"]["denominator"], 1)
        empty = summarize([fixture, imported, scripted])
        self.assertEqual(empty["status"], "not_available")
        self.assertEqual(empty["formal_run_count"], 0)

    def test_different_models_and_budgets_do_not_share_one_protocol_group(self):
        base = live_run("base")
        model_change = copy.deepcopy(base)
        model_change["run_id"] = "model-change"
        model_change["model"]["name"] = "different-model"
        budget_change = copy.deepcopy(base)
        budget_change["run_id"] = "budget-change"
        budget_change["runtime"]["max_steps"] = 70
        report = summarize([base, model_change, budget_change])
        self.assertEqual(len(report["groups"]), 3)
        self.assertEqual({g["total_started"] for g in report["groups"]}, {1})

    def test_compare_reports_budget_change_and_rejects_different_tasks(self):
        left = live_run("left")
        right = copy.deepcopy(left)
        right["run_id"] = "right"
        right["runtime"]["max_steps"] = 70
        comparison = compare_runs(left, right)
        self.assertFalse(comparison["comparable"])
        self.assertTrue(any("max_steps" in d["field"] for d in comparison["differences"]))
        right = live_run("other-task", task_id="43")
        self.assertFalse(compare_runs(left, right)["comparable"])

    def test_comparison_cannot_certify_unknown_environment_version(self):
        left = live_run("left")
        right = live_run("right")
        self.assertTrue(compare_runs(left, right)["comparable"])
        left.pop("environment_version")
        right.pop("environment_version")
        comparison = compare_runs(left, right)
        self.assertFalse(comparison["comparable"])
        self.assertTrue(comparison["warnings"])

    def test_invalid_reward_cannot_certify_a_contradictory_success_label(self):
        contradictory = live_run(result={
            "reward": 1.0, "reward_valid": False,
            "strict_success": True, "purchase_success": True,
        })
        group = summarize([contradictory])["groups"][0]
        for name in ("strict_success", "purchase_success"):
            metric = group[name]
            self.assertEqual(metric["numerator"], 0)
            self.assertEqual(metric["denominator"], 1)
            self.assertEqual(metric["known"], 0)
            self.assertEqual(metric["unknown"], 1)


if __name__ == "__main__":
    unittest.main()
