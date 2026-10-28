"""Context/recovery contracts using a labelled fake model, never a live score."""
import asyncio
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from lab.storage import Store
from webagent.runtime import BrowserRuntime, model_context


def call(name, args, ident):
    return {"id": ident, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


class ContextTests(unittest.TestCase):
    def test_compact_context_keeps_current_dom_and_outcomes_without_old_dom(self):
        history = [{"role": "system", "content": "system"}, {"role": "user", "content": "OLD-DOM"}]
        observation = {"text": "CURRENT-DOM", "elements": [{"ref": "new-ref", "name": "current button"}]}
        journal = [{"step": 1, "outcome": "rejected", "error": "target became stale"}]
        compact = model_context(history, "same user goal", observation, journal, "latest_observation")
        serialized = json.dumps(compact)
        self.assertIn("CURRENT-DOM", serialized)
        self.assertIn("new-ref", serialized)
        self.assertIn("target became stale", serialized)
        self.assertNotIn("OLD-DOM", serialized)
        self.assertEqual(history[1]["content"], "OLD-DOM")
        self.assertIs(model_context(history, "goal", observation, journal), history)
        with self.assertRaises(ValueError):
            model_context(history, "goal", observation, journal, "invented")


class RecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_parallel_decision_rejected_then_model_recovers_with_specific_feedback(self):
        for mode in ["full_history", "latest_observation"]:
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as directory:
                class Page:
                    async def evaluate(self, code):
                        return {"url": "http://127.0.0.1/sandbox/product?id=h-cloud", "cart_count": 0, "cart": [],
                                "detail": {"product_id": "h-cloud", "text": "Wireless Not provided"}}

                class Session:
                    def __init__(self, *args, **kwargs):
                        self.page, self.actions = Page(), []

                    async def start(self):
                        return {"text": "Wireless Not provided; Shopping list 0", "elements": [], "url": "http://127.0.0.1/sandbox/product?id=h-cloud"}

                    async def observe(self):
                        return await self.start()

                    async def act(self, action):
                        self.actions.append(action)
                        raise AssertionError("Invalid parallel calls must never reach the browser")

                    async def close(self):
                        pass

                class Model:
                    def __init__(self):
                        self.inputs = []

                    async def generate(self, messages, tools, max_tokens):
                        self.inputs.append(deepcopy(messages))
                        if len(self.inputs) == 1:
                            calls = [call("browser_wait", {"milliseconds": 0}, "first"), call("browser_back", {}, "second")]
                        else:
                            feedback = json.dumps(messages)
                            if "exactly one browser function call" not in feedback:
                                raise AssertionError("The model must receive the actual rejection reason")
                            calls = [call("finish", {"reason": "blocked", "evidence": ["Wireless not provided; list empty"]}, "finish")]
                        return {"message": {"role": "assistant", "content": None, "tool_calls": calls},
                                "usage": {"input_tokens": 20, "output_tokens": 5}}

                    async def close(self):
                        pass

                session, model = Session(), Model()
                spec = {"mode": "browser", "task_id": "browser-headphones-unknown", "query": "Check missing connection information",
                        "model": {"provider": "api", "name": "labelled-mock-model"},
                        "runtime": {"max_steps": 35, "max_seconds": 20, "max_output_tokens": 128, "context_mode": mode},
                        "browser": {"start_url": "http://127.0.0.1/sandbox/", "allowed_origins": ["http://127.0.0.1"]}}
                store = Store(directory)
                run = store.create_run(spec, source="api_mock_browser_contract")
                runtime = BrowserRuntime(store, Path(directory) / "shots", session_factory=lambda *a, **kw: session,
                                         model_factory=lambda spec: model)
                await runtime.run(run["run_id"], spec, asyncio.Event())
                final = store.get_run(run["run_id"])
                self.assertEqual(final["status"], "completed", final)
                self.assertTrue(final["result"]["success"])
                self.assertEqual(final["model_calls"], 2)
                self.assertEqual(session.actions, [])
                events = store.events(run["run_id"])
                errors = [e for e in events if e["type"] == "error"]
                self.assertEqual(len(errors), 1)
                self.assertFalse(errors[0]["payload"]["action_retried"])
                self.assertEqual(final["context_policy"]["mode"], mode)


if __name__ == "__main__":
    unittest.main()
