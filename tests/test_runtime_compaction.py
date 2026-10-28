"""Observation compaction, progress signatures, and browser recovery."""
from __future__ import annotations

import asyncio
import json
import time
import unittest

from webagent.browser import BrowserError
from webagent.runtime import (ProgressGuard, _recover_observe, compact_observation,
                              observation_signature, action_signature)


class ObservationCompactionTests(unittest.TestCase):
    def test_large_observation_drops_context_first_and_keeps_evidence(self):
        observation = {
            "url": "https://example.test/entry",
            "text": "x" * 12000,
            "elements": [
                {"ref": f"e{index}", "role": "button", "name": "Action", "context": "c" * 700,
                 "options": [{"value": str(value)} for value in range(100)]}
                for index in range(300)
            ],
            "truncated": False,
        }
        before = len(json.dumps(observation, ensure_ascii=False))
        compact = compact_observation(observation, limit=30000)
        after = len(json.dumps(compact, ensure_ascii=False))
        self.assertGreater(before, 30000)
        self.assertLessEqual(after, 30000)
        self.assertIsNot(compact, observation)
        self.assertIn("element_context", compact["model_view"]["dropped"])
        self.assertTrue(compact["truncated"])
        self.assertIn("context", observation["elements"][0])
        self.assertEqual(observation["text"], "x" * 12000)

    def test_small_observation_is_returned_unchanged(self):
        observation = {"url": "https://example.test/", "text": "short", "elements": []}
        self.assertIs(compact_observation(observation, limit=30000), observation)

    def test_signatures_ignore_nothing_relevant_and_detect_page_change(self):
        first = {"url": "https://example.test/a", "text": "same", "total_interactive_elements": 3}
        second = {"url": "https://example.test/a", "text": "same", "total_interactive_elements": 3}
        changed = {"url": "https://example.test/b", "text": "same", "total_interactive_elements": 3}
        self.assertEqual(observation_signature(first), observation_signature(second))
        self.assertNotEqual(observation_signature(first), observation_signature(changed))
        self.assertEqual(action_signature({"type": "scroll", "direction": "down"}),
                         action_signature({"direction": "down", "type": "scroll"}))


class ProgressGuardTests(unittest.TestCase):
    def test_same_click_with_waits_triggers_hint_then_stop(self):
        guard = ProgressGuard()
        observation = {"url": "https://example.test/", "text": "same", "total_interactive_elements": 2}
        click = {"type": "click", "ref": "e1"}
        wait = {"type": "wait", "milliseconds": 1000}
        outcomes = []
        for _ in range(8):
            outcomes.append(guard.update(click, observation))
            guard.update(wait, observation)
        self.assertEqual(outcomes[2][0], "hint")
        self.assertEqual(outcomes[-1][0], "stop")
        self.assertGreaterEqual(guard.target_repeat, 8)

    def test_changing_refs_with_the_same_target_name_still_stop(self):
        guard = ProgressGuard()
        outcomes = []
        for index in range(8):
            ref = f"obs-{index + 1:04d}-e1"
            observation = {"url": "https://example.test/", "text": "same",
                           "total_interactive_elements": 2,
                           "elements": [{"ref": ref, "name": "Compute", "role": "button"}]}
            outcomes.append(guard.update({"type": "click", "ref": ref}, observation, observation))
        self.assertEqual(outcomes[2][0], "hint")
        self.assertEqual(outcomes[-1][0], "stop")

    def test_different_target_resets_the_target_count(self):
        guard = ProgressGuard()
        observation = {"url": "https://example.test/", "text": "same", "total_interactive_elements": 2}
        for _ in range(3):
            guard.update({"type": "click", "ref": "e1"}, observation)
        guard.update({"type": "click", "ref": "e2"}, observation)
        self.assertEqual(guard.target_repeat, 1)

    def test_repeated_scroll_with_unchanged_page_stops(self):
        guard = ProgressGuard()
        observation = {"url": "https://example.test/", "text": "same", "total_interactive_elements": 2}
        action = {"type": "scroll", "direction": "down"}
        outcomes = [guard.update(action, observation) for _ in range(6)]
        self.assertIsNone(outcomes[1])
        self.assertEqual(outcomes[5][0], "stop")


class _RecoverySession:
    def __init__(self, *, observe_failures=0, back_fails=False):
        self.observe_failures = observe_failures
        self.back_fails = back_fails
        self.observations = 0
        self.actions = []

    async def observe(self):
        self.observations += 1
        if self.observations <= self.observe_failures:
            raise BrowserError("document is changing")
        return {"url": "https://example.test/", "text": "recovered"}

    async def act(self, action):
        self.actions.append(action)
        if self.back_fails:
            raise BrowserError("back failed")
        return {"url": "https://example.test/", "text": "after back"}


class BrowserRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def _recover(self, session):
        return await _recover_observe(session, time.monotonic() + 10, asyncio.Event())

    async def test_retries_observation_once_before_going_back(self):
        session = _RecoverySession(observe_failures=1)
        observation, attempts = await self._recover(session)
        self.assertEqual(attempts, 1)
        self.assertEqual(observation["text"], "recovered")
        self.assertEqual(session.actions, [])

    async def test_goes_back_when_observation_keeps_failing(self):
        session = _RecoverySession(observe_failures=10)
        observation, attempts = await self._recover(session)
        self.assertEqual(attempts, 2)
        self.assertEqual(session.actions, [{"type": "back"}])
        self.assertEqual(observation["text"], "after back")

    async def test_final_observation_attempt_when_back_fails(self):
        session = _RecoverySession(observe_failures=2, back_fails=True)
        observation, attempts = await self._recover(session)
        self.assertEqual(attempts, 3)
        self.assertEqual(observation["text"], "recovered")


if __name__ == "__main__":
    unittest.main()
