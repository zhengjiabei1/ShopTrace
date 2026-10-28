import copy
import json
from pathlib import Path
import tempfile
import unittest

from lab.importer import normalize_upstream, persist_import, prepare_import, load_documents
from lab.storage import Store


def upstream():
    return {"trajectory_id": "old-1", "task_id": 10, "status": "done", "done": True,
            "initial_result": {"instruction": "Buy a black cup", "goal_options": ["PRIVATE"], "goal": "PRIVATE"},
            "messages": [{"role": "assistant", "reasoning_content": "PRIVATE", "content": "PRIVATE"}],
            "steps": [{"tool_name": "search_products", "parameters": {"query": "black cup"},
                       "env_action": "search[black cup]", "observation": "Visible product \u00c5", "raw_observation": "PRIVATE", "done": False},
                      {"tool_name": "buy_now", "parameters": {}, "env_action": "click[Buy Now]",
                       "observation": "PRIVATE terminal gold", "done": True}],
            "terminal_result": {"done": True, "over": True, "reward": 1,
                                "reward_detail": {"reward_valid": True, "reward_type": "gold_purchase", "purchase_success": True},
                                "goal": "PRIVATE"}}


class ImporterTests(unittest.TestCase):
    def test_raw_actor_visible_only_and_unknown_budgets(self):
        document = normalize_upstream(upstream())
        text = json.dumps(document, ensure_ascii=False)
        self.assertNotIn("PRIVATE", text)
        self.assertIn("Visible product \u00c5", text)
        self.assertEqual(document["run"]["runtime"], {})
        self.assertIsNone(document["run"]["model_calls"])
        self.assertTrue(document["run"]["result"]["strict_success"])
        self.assertEqual(document["run"]["source"], "imported")
        self.assertFalse(any(e["type"] == "model_request" for e in document["events"]))

    def test_missing_reward_is_unknown(self):
        raw = upstream()
        raw["terminal_result"] = {"done": True}
        result = normalize_upstream(raw)["run"]["result"]
        self.assertIsNone(result["reward"])
        self.assertIsNone(result["strict_success"])
        self.assertFalse(result["reward_valid"])

    def test_rejects_unknown_format_and_bad_guard_order(self):
        with self.assertRaises(ValueError):
            normalize_upstream({"task_id": 10, "events": []})
        raw = upstream()
        raw["blocked_tool_calls"] = [{"step_index": 999}]
        with self.assertRaises(ValueError):
            normalize_upstream(raw)

    def test_normalized_format(self):
        source = {"schema_version": "shopping-normalized-trajectory-v1", "task_id": 10,
                  "actor_query": "Buy a cup", "events": [{"event_type": "guard_rejection", "event_id": "e0001", "tool_name": "open_product", "parameters": {"asin": "A"}, "guard_reason": "not visible"},
                                                          {"event_type": "tool_step", "tool_name": "search_products", "parameters": {"query": "cup"}, "actor_visible_observation": "Candidate A", "done": False}],
                  "terminal": {}, "environment": {"environment_version": "v2"}}
        result = normalize_upstream(source)
        self.assertEqual(result["events"][1]["payload"]["kind"], "guard_rejection")
        self.assertEqual(result["run"]["status"], "failed")

    def test_persist_never_trusts_source_or_reuses_id(self):
        envelope = normalize_upstream(upstream())
        envelope["run"].update(run_id="forged", source="live")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            run = persist_import(store, envelope)
            self.assertEqual(run["source"], "imported")
            self.assertNotEqual(run["run_id"], "forged")
            self.assertFalse(run["provenance"]["verified"])
            self.assertEqual(len(store.events(run["run_id"])), len(envelope["events"]))

    def test_rejects_before_persisting(self):
        envelope = normalize_upstream(upstream())
        envelope["events"][1]["sequence"] = envelope["events"][0]["sequence"]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with self.assertRaises(ValueError):
                persist_import(store, envelope)
            self.assertEqual(store.list_runs(), [])

    def test_rejects_nonfinite_and_malformed_metadata(self):
        envelope = normalize_upstream(upstream())
        envelope["run"]["elapsed_seconds"] = float("nan")
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            with self.assertRaises(ValueError):
                persist_import(store, envelope)
            self.assertEqual(store.list_runs(), [])

    def test_malformed_run_fields_rejected_atomically(self):
        mutations = [
            ("usage", "invalid"), ("usage", {"input_tokens": -1}),
            ("usage", {"input_tokens": 1.5}), ("usage", {"input_tokens": True}),
            ("error", "invalid"), ("error", {"kind": []}), ("error", {"message": "x" * 2001}),
            ("steps", 0.5), ("steps", -1), ("model_calls", True),
            ("elapsed_seconds", -1), ("runtime", {"max_steps": 0}),
            ("runtime", {"max_seconds": "10"}), ("mode", []),
            ("status", []), ("model", {"provider": [], "name": "test"}),
        ]
        for field, value in mutations:
            with self.subTest(field=field, value=value), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                envelope = normalize_upstream(upstream())
                envelope["run"][field] = value
                with self.assertRaises(ValueError):
                    persist_import(store, envelope)
                self.assertEqual(store.list_runs(), [])

    def test_bad_result_events_and_annotations_rejected(self):
        mutations = [
            lambda row: row["run"]["result"].update(reward_valid="yes"),
            lambda row: row["run"]["result"].update(fixture_success=1),
            lambda row: row["events"][0].update(type="x" * 101),
            lambda row: row["events"][0].update(event_id="x" * 101),
            lambda row: row["events"][0]["payload"].update(number=float("inf")),
            lambda row: row.update(annotations="invalid"),
            lambda row: row.update(annotations=[{"event_id": "missing", "category": "other", "note": "test"}]),
            lambda row: row.update(annotations=[{"event_id": row["events"][0]["event_id"], "category": [], "note": "test"}]),
        ]
        for mutate in mutations:
            with self.subTest(mutate=mutate), tempfile.TemporaryDirectory() as directory:
                store = Store(directory)
                envelope = normalize_upstream(upstream())
                mutate(envelope)
                with self.assertRaises(ValueError):
                    persist_import(store, envelope)
                self.assertEqual(store.list_runs(), [])

    def test_nullable_values_and_protocol_hashes_preserved(self):
        envelope = normalize_upstream(upstream())
        envelope["run"].update(usage=None, error=None, steps=None, model_calls=None,
                               elapsed_seconds=None, seed=42, data_hash="abc", prompt_hash="def")
        envelope["run"]["result"].update(reward_valid=None, fixture_success=None)
        with tempfile.TemporaryDirectory() as directory:
            run = persist_import(Store(directory), envelope)
            self.assertEqual(run["seed"], 42)
            self.assertEqual(run["data_hash"], "abc")
            self.assertEqual(run["prompt_hash"], "def")

    def test_nonobject_prepare_is_value_error(self):
        for document in (None, [], "invalid"):
            with self.assertRaises(ValueError):
                prepare_import(document)

    def test_jsonl_loading(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "raw.jsonl"
            path.write_text(json.dumps(upstream(), ensure_ascii=False) + "\n", encoding="utf-8")
            result = load_documents(path)
            self.assertEqual(len(result), 1)
            self.assertEqual(prepare_import(result[0])["run"]["task_id"], "10")


if __name__ == "__main__":
    unittest.main()
