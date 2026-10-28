"""Pure preregistered selection and pairing checks; no models or API calls."""
import hashlib
import unittest

from scripts.compare_context import CONTROL, CANDIDATE, paired_summary, schedule, select_context


CASES = [f"case-{i:04x}" for i in range(6)]


def arm(scores, tokens=100, *, mode=CONTROL):
    return [{"task_id": case_id, "run": {"task_id": case_id, "status": "completed", "source": "browser_model",
             "model": {"provider": "api", "name": "labelled-mock-model"},
             "result": {"success": score}, "usage": {"input_tokens": tokens},
             "prompt_hash": "a" * 64, "tools_hash": "b" * 64, "protocol_version": "test-protocol-v1",
             "runtime": {"max_steps": 35, "max_seconds": 180, "max_output_tokens": 512, "context_mode": mode},
             "context_policy": {"unit": "serialized_json_characters", "max": 96000,
                                "overflow": "stop_without_truncation", "mode": mode},
             "benchmark_scope": "local_synthetic_website",
             "benchmark_case": {"id": case_id, "split": "development", "dataset_fingerprint": "c" * 64,
                                "catalog_fingerprint": hashlib.sha256(case_id.encode()).hexdigest()}}}
            for case_id, score in zip(CASES, scores)]


class ContextExperimentTests(unittest.TestCase):
    def test_candidate_requires_equal_or_better_success_and_strictly_lower_known_cost(self):
        control = arm([True, True, True, False, True, True], 100)
        candidate = arm([True, True, True, False, True, True], 99, mode=CANDIDATE)
        decision = select_context(control, candidate, CASES)
        self.assertEqual(decision["chosen_context"], CANDIDATE)
        self.assertEqual(decision["control_input_tokens"], 600)
        self.assertEqual(decision["candidate_input_tokens"], 594)
        self.assertEqual(select_context(control, arm([True] * 6, 100, mode=CANDIDATE), CASES)["chosen_context"], CONTROL)
        self.assertEqual(select_context(control, arm([False] * 6, 1, mode=CANDIDATE), CASES)["chosen_context"], CONTROL)

    def test_unknown_result_or_token_cannot_make_candidate_win(self):
        control = arm([True] * 6, 100)
        unknown_score = arm([True, True, None, True, True, True], 1, mode=CANDIDATE)
        self.assertEqual(select_context(control, unknown_score, CASES)["chosen_context"], CONTROL)
        unknown_usage = arm([True] * 6, 1, mode=CANDIDATE)
        unknown_usage[0]["run"]["usage"]["input_tokens"] = None
        result = select_context(control, unknown_usage, CASES)
        self.assertFalse(result["all_input_usages_known"])
        self.assertEqual(result["chosen_context"], CONTROL)

    def test_missing_duplicate_or_non_model_cases_cannot_count_as_complete(self):
        control = arm([True] * 6, 100)
        for candidate in (arm([True] * 5, 1, mode=CANDIDATE), [*arm([True] * 5, 1, mode=CANDIDATE), arm([True] * 6, 1, mode=CANDIDATE)[0]]):
            with self.subTest(candidate=candidate):
                self.assertFalse(select_context(control, candidate, CASES)["complete_known_candidate"])
        candidate = arm([True] * 6, 1, mode=CANDIDATE)
        candidate[0]["run"]["source"] = "api_mock_browser_contract"
        self.assertEqual(select_context(control, candidate, CASES)["chosen_context"], CONTROL)

    def test_pairing_preserves_unknowns_and_measures_wins_losses_and_ties(self):
        control = arm([True, False, True, False, None, True])
        candidate = arm([False, True, True, False, True, None])
        values = paired_summary(control, candidate, CASES)
        self.assertEqual(values["candidate_wins"], 1)
        self.assertEqual(values["candidate_losses"], 1)
        self.assertEqual(values["ties"], 2)
        self.assertEqual(values["unknown_pairs"], 2)
        self.assertEqual(values["known_pairs"], 4)

    def test_protocol_drift_cannot_select_candidate_even_when_cheaper(self):
        mutations = (
            ("model", "name", "another-model"),
            (None, "prompt_hash", "d" * 64),
            (None, "tools_hash", "d" * 64),
            (None, "protocol_version", "changed-protocol"),
            ("runtime", "max_seconds", 181),
            ("context_policy", "max", 98000),
            ("benchmark_case", "dataset_fingerprint", "d" * 64),
            ("benchmark_case", "catalog_fingerprint", "d" * 64),
            ("benchmark_case", "split", "heldout"),
        )
        for section, key, value in mutations:
            for entire_arm in (False, True):
                with self.subTest(section=section, key=key, entire_arm=entire_arm):
                    candidate = arm([True] * 6, 1, mode=CANDIDATE)
                    for entry in candidate if entire_arm else candidate[:1]:
                        destination = entry["run"] if section is None else entry["run"][section]
                        destination[key] = value
                    decision = select_context(arm([True] * 6, 100), candidate, CASES)
                    self.assertEqual(decision["chosen_context"], CONTROL)
                    self.assertFalse(decision["provenance_compatible"])
                    self.assertTrue(decision["reason"].startswith("incompatible_provenance:"))

    def test_missing_or_malformed_provenance_falls_back_without_raising(self):
        for field in ("model", "runtime", "context_policy", "benchmark_case", "prompt_hash", "protocol_version"):
            for malformed in (None, [], {}):
                with self.subTest(field=field, malformed=malformed):
                    candidate = arm([True] * 6, 1, mode=CANDIDATE)
                    candidate[0]["run"][field] = malformed
                    decision = select_context(arm([True] * 6, 100), candidate, CASES)
                    self.assertEqual(decision["chosen_context"], CONTROL)
                    self.assertFalse(decision["provenance_compatible"])
                    self.assertIn("incompatible_provenance", decision["reason"])

    def test_context_modes_must_match_their_arms(self):
        for section, key, value in (("runtime", "context_mode", CONTROL),
                                    ("context_policy", "mode", CONTROL)):
            with self.subTest(section=section):
                candidate = arm([True] * 6, 1, mode=CANDIDATE)
                candidate[0]["run"][section][key] = value
                decision = select_context(arm([True] * 6, 100), candidate, CASES)
                self.assertEqual(decision["chosen_context"], CONTROL)
                self.assertIn("incorrect_arm_context_mode", decision["reason"])

    def test_pair_specific_catalogs_are_allowed_when_both_arms_match(self):
        control = arm([True] * 6, 100)
        candidate = arm([True] * 6, 1, mode=CANDIDATE)
        self.assertEqual(len({entry["run"]["benchmark_case"]["catalog_fingerprint"] for entry in control}), 6)
        decision = select_context(control, candidate, CASES)
        self.assertTrue(decision["provenance_compatible"])
        self.assertEqual(decision["chosen_context"], CANDIDATE)
        self.assertEqual(decision["reason"], "candidate_meets_development_rule")

    def test_malformed_expected_cases_and_usage_cannot_select_candidate(self):
        control = arm([True] * 6, 100)
        for invalid in (None, [None] * 6, CASES[:-1], [{}] * 6):
            with self.subTest(invalid=invalid):
                decision = select_context(control, arm([True] * 6, 1, mode=CANDIDATE), invalid)
                self.assertEqual(decision["chosen_context"], CONTROL)
                self.assertIn("invalid_expected_cases", decision["reason"])
        candidate = arm([True] * 6, 1, mode=CANDIDATE)
        candidate[0]["run"]["usage"] = []
        decision = select_context(control, candidate, CASES)
        self.assertEqual(decision["chosen_context"], CONTROL)
        self.assertEqual(decision["reason"], "input_usage_unknown")

    def test_arm_order_alternates_and_control_selection_skips_duplicates(self):
        values = schedule(CASES)
        self.assertEqual(len(values), 12)
        self.assertEqual([v["context_mode"] for v in values[:4]], [CONTROL, CANDIDATE, CANDIDATE, CONTROL])
        self.assertEqual([v["task_id"] for v in values[:4]], [CASES[0], CASES[0], CASES[1], CASES[1]])
        full = schedule(CASES, candidate=CONTROL)
        self.assertEqual(len(full), 6)
        self.assertTrue(all(v["context_mode"] == CONTROL for v in full))


if __name__ == "__main__":
    unittest.main()
