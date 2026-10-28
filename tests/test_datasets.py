"""Checks for split leakage and evidence-gated action-only training exports."""

import copy
import json
import unittest

from lab.datasets import build_sft_records, validate_splits


def accepted_trajectory(run_id="run-1"):
    return {
        "run": {
            "run_id": run_id, "task_id": "42", "mode": "shopsimulator",
            "source": "live", "status": "completed",
            "model": {"provider": "api", "name": "teacher", "stage": "other"},
            "result": {"reward": 1.0, "reward_valid": True,
                       "strict_success": True, "purchase_success": True},
            "error": None,
        },
        "events": [
            {"type": "model_request", "payload": {
                "messages": [
                    {"role": "system", "content": "Choose one shopping tool."},
                    {"role": "user", "content": "Find a red cup under 20 yuan."},
                ],
                "tools": [{"type": "function", "function": {
                    "name": "search_products", "parameters": {
                        "type": "object", "properties": {"query": {"type": "string"}},
                        "required": ["query"],
                    },
                }}],
            }},
            {"type": "model_response", "payload": {"message": {
                "role": "assistant", "content": "Hidden reasoning must not be exported.",
                "reasoning_content": "private chain of thought",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {
                    "name": "search_products", "arguments": '{"query":"red cup"}',
                }}],
            }}},
            {"type": "action", "payload": {
                "step": 0, "tool_name": "search_products",
                "arguments": {"query": "red cup"}, "env_action": "search[red cup]",
            }},
            {"type": "environment_result", "payload": {
                "step": 0, "observation": "Search results for red cup",
                "state": {}, "done": False, "result": None,
            }},
        ],
    }


class DatasetTests(unittest.TestCase):
    def test_cross_split_ids_and_normalized_queries_are_leakage(self):
        report = validate_splits({
            "train": [{"task_id": 1, "query": "\uff32\uff45\uff44 cup"}],
            "validation": [{"task_id": "1", "query": "red   cup"}],
        })
        self.assertFalse(report["valid"])
        self.assertTrue(report["task_id_overlaps"])
        self.assertTrue(report["query_duplicates"])

    def test_shared_product_is_a_reported_risk_and_missing_task_id_is_invalid(self):
        report = validate_splits({
            "train": [{"task_id": "a", "query": "red cup", "asin": "item-1"}],
            "test": [{"task_id": "b", "query": "blue coat", "product_id": "item-1"}],
        })
        self.assertTrue(report["valid"])
        self.assertTrue(report["product_overlaps"])
        missing = validate_splits({"train": [{"query": "No task id"}]})
        self.assertFalse(missing["valid"])

    def test_export_preserves_tool_actions_without_assistant_reasoning(self):
        trajectory = accepted_trajectory()
        records, report = build_sft_records([trajectory])
        self.assertEqual(report["accepted_trajectories"], 1)
        self.assertEqual(len(records), 1)
        record = records[0]
        assistant = record["messages"][-1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["tool_calls"][0]["function"]["name"], "search_products")
        self.assertNotIn("Hidden reasoning", json.dumps(records))
        self.assertNotIn("private chain", json.dumps(records))
        self.assertEqual(record["metadata"]["loss_mask_policy"],
                         "assistant_action_tokens_only_required")
        self.assertIn("42", report["task_ids"])

    def test_fixture_imported_unknown_reward_and_infrastructure_are_rejected(self):
        fixture = accepted_trajectory("fixture")
        fixture["run"].update(mode="fixture", source="synthetic_fixture")
        imported = accepted_trajectory("imported")
        imported["run"]["source"] = "imported"
        unknown = accepted_trajectory("unknown")
        unknown["run"]["result"] = {"reward_valid": False, "strict_success": None}
        infrastructure = accepted_trajectory("infrastructure")
        infrastructure["run"]["error"] = {"kind": "environment", "message": "failure"}
        records, report = build_sft_records([fixture, imported, unknown, infrastructure])
        self.assertEqual(records, [])
        self.assertEqual(report["accepted_trajectories"], 0)
        self.assertEqual(report["rejected_trajectories"], 4)
        self.assertEqual(len(report["rejections"]), 4)

    def test_export_does_not_mutate_original_raw_trajectory(self):
        trajectory = accepted_trajectory()
        original = copy.deepcopy(trajectory)
        build_sft_records([trajectory])
        self.assertEqual(trajectory, original)

    def test_successful_test_task_is_still_excluded_from_training_export(self):
        records, report = build_sft_records(
            [accepted_trajectory()],
            split_manifest={
                "train": [{"task_id": "1", "query": "Blue jacket"}],
                "test": [{"task_id": "42", "query": "Red cup"}],
            },
        )
        self.assertEqual(records, [])
        self.assertEqual(report["rejected_trajectories"], 1)
        self.assertEqual(report["rejections"][0]["reason"], "held_out_test_task")

    def test_unexecuted_model_output_is_not_a_training_action(self):
        trajectory = accepted_trajectory()
        trajectory["events"] = [event for event in trajectory["events"]
                                if event["type"] != "environment_result"]
        records, report = build_sft_records([trajectory])
        self.assertEqual(records, [])
        self.assertEqual(report["accepted_trajectories"], 0)
        self.assertEqual(report["rejected_trajectories"], 1)


if __name__ == "__main__":
    unittest.main()
