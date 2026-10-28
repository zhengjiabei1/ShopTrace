from __future__ import annotations

import asyncio
import json
import os
import unittest
from copy import deepcopy
from unittest.mock import patch

import httpx

from lab.environments import FixtureEnvironment, ShopSimulatorEnvironment, public_state
from lab.models import APIModel, ModelError, ScriptedModel
from lab.runtime import ActionError, Runtime, parse_action


class MemoryStore:
    def __init__(self):
        self.runs = {"test": {"run_id": "test"}}
        self.events = []

    def get_run(self, run_id):
        return deepcopy(self.runs[run_id])

    def update_run(self, run_id, **changes):
        self.runs[run_id].update(deepcopy(changes))

    def append_event(self, run_id, event_type, payload):
        event = {"event_id": f"e{len(self.events) + 1:04}", "type": event_type, "payload": deepcopy(payload)}
        self.events.append(event)
        return event


def spec(task_id="fixture-001", **runtime):
    return {"task_id": task_id, "mode": "fixture", "model": {"provider": "scripted", "name": "fixture-script", "stage": "other"}, "runtime": {"max_steps": 35, "max_calls": 35, "max_seconds": 30, "max_output_tokens": 512, **runtime}, "strategy": "default"}


def call(name, args):
    return {"role": "assistant", "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_all_fixtures_terminate_without_claiming_real_scores(self):
        for task_id in ("fixture-001", "fixture-002", "fixture-003"):
            store = MemoryStore()
            await Runtime(store).run("test", spec(task_id), asyncio.Event())
            result = store.runs["test"]
            self.assertEqual(result["status"], "completed", result)
            self.assertTrue(result["result"]["fixture_success"])
            self.assertIsNone(result["result"]["reward"])
            self.assertIsNone(result["result"]["strict_success"])
            self.assertFalse(result["result"]["reward_valid"])
            self.assertIsNone(result["usage"]["input_tokens"])
            self.assertEqual(result["steps"], sum(e["type"] == "action" for e in store.events))
            requests = [e for e in store.events if e["type"] == "model_request"]
            self.assertEqual(len(requests), result["model_calls"])
            self.assertIn("messages", requests[0]["payload"])

    async def test_provenance_identifies_own_protocol_and_no_unverified_environment_version(self):
        store = MemoryStore()
        await Runtime(store).run("test", spec(), asyncio.Event())
        run = store.runs["test"]
        self.assertEqual(run["protocol_version"], "shopping-lab-runtime-v1")
        self.assertEqual(run["observation_adapter_version"], "shopping-lab-public-json-v1")
        self.assertEqual(len(run["prompt_hash"]), 64)
        self.assertEqual(len(run["tools_hash"]), 64)
        self.assertEqual(run["environment_observation_version"], "shopping-observation-v2")
        self.assertIsNone(run["environment_version"])
        self.assertIsNone(run["reward_version"])

    async def test_model_failure_usage_is_unknown_not_zero(self):
        class BrokenModel(ScriptedModel):
            async def generate(self, *args):
                raise ModelError("provider unavailable")
        store = MemoryStore()
        await Runtime(store, model_factory=lambda _: BrokenModel("fixture-001")).run("test", spec(), asyncio.Event())
        self.assertEqual(store.runs["test"]["model_calls"], 1)
        self.assertEqual(store.runs["test"]["usage"], {"input_tokens": None, "output_tokens": None})

    async def test_context_limit_terminates_without_model_call_or_silent_truncation(self):
        store = MemoryStore()
        await Runtime(store).run("test", spec(max_context_chars=10), asyncio.Event())
        run = store.runs["test"]
        self.assertEqual(run["status"], "budget_exceeded")
        self.assertEqual(run["termination_reason"], "context_budget")
        self.assertEqual(run["model_calls"], 0)
        self.assertEqual(run["steps"], 0)
        self.assertEqual(run["context_policy"]["overflow_action"], "terminate_without_truncation")
        observation = next(event for event in store.events if event["type"] == "observation")
        self.assertIn("Actor-visible shopping state", observation["payload"]["observation"])

    async def test_step_budget_keeps_partial_trajectory(self):
        store = MemoryStore()
        await Runtime(store).run("test", spec(max_steps=1), asyncio.Event())
        self.assertEqual(store.runs["test"]["status"], "budget_exceeded")
        self.assertEqual(store.runs["test"]["steps"], 1)
        self.assertEqual(store.runs["test"]["model_calls"], 1)
        self.assertTrue(any(e["type"] == "environment_result" for e in store.events))

    async def test_no_scripted_real_environment_execution(self):
        store = MemoryStore()
        config = spec()
        config["mode"] = "shopsimulator"
        await Runtime(store).run("test", config, asyncio.Event())
        self.assertEqual(store.runs["test"]["status"], "failed")
        self.assertEqual(store.runs["test"]["error"]["kind"], "configuration")
        self.assertEqual(store.runs["test"]["model_calls"], 0)

    async def test_multiple_calls_never_execute_environment_action(self):
        class InvalidModel(ScriptedModel):
            async def generate(self, *args):
                response = await super().generate(*args)
                response["message"]["tool_calls"] *= 2
                return response
        store = MemoryStore()
        await Runtime(store, model_factory=lambda _: InvalidModel("fixture-001")).run("test", spec(), asyncio.Event())
        self.assertEqual(store.runs["test"]["status"], "failed")
        self.assertEqual(store.runs["test"]["error"]["kind"], "invalid_action")
        self.assertEqual(store.runs["test"]["steps"], 0)
        self.assertFalse(any(e["type"] == "action" for e in store.events))

    async def test_cancellation_interrupts_wait_and_releases(self):
        class SlowEnvironment(FixtureEnvironment):
            released = False
            async def reset(self, task_id):
                await asyncio.sleep(30)
            async def release(self):
                self.released = True
        environment = SlowEnvironment()
        store = MemoryStore()
        cancelled = asyncio.Event()
        execution = asyncio.create_task(Runtime(store, environment_factory=lambda _: environment).run("test", spec(), cancelled))
        await asyncio.sleep(0.01)
        cancelled.set()
        await asyncio.wait_for(execution, timeout=1)
        self.assertTrue(environment.released)
        self.assertEqual(store.runs["test"]["status"], "cancelled")
        self.assertEqual(store.runs["test"]["steps"], 0)

    async def test_time_budget_interrupts_inflight_action_without_retry(self):
        class SlowEnvironment(FixtureEnvironment):
            attempts = 0
            released = False
            async def step(self, action):
                self.attempts += 1
                await asyncio.sleep(30)
            async def release(self):
                self.released = True
        environment = SlowEnvironment()
        store = MemoryStore()
        await Runtime(store, environment_factory=lambda _: environment).run("test", spec(max_seconds=0.02), asyncio.Event())
        self.assertEqual(store.runs["test"]["status"], "budget_exceeded")
        self.assertEqual(environment.attempts, 1)
        self.assertTrue(environment.released)
        self.assertEqual(store.runs["test"]["steps"], 0)

    async def test_release_failure_preserves_primary_error(self):
        class BrokenEnvironment(FixtureEnvironment):
            async def step(self, action):
                raise ModelError("primary failure")
            async def release(self):
                raise ModelError("release failure")
        store = MemoryStore()
        await Runtime(store, environment_factory=lambda _: BrokenEnvironment()).run("test", spec(), asyncio.Event())
        self.assertEqual(store.runs["test"]["error"]["message"], "primary failure")
        self.assertEqual(store.runs["test"]["release_error"]["message"], "release failure")

    async def test_api_usage_and_credentials_are_separate(self):
        received = []
        def handler(request):
            received.append(request)
            return httpx.Response(200, json={"choices": [{"message": {**call("search_products", {"query": "headphones"}), "reasoning_content": "not retained"}}], "usage": {"prompt_tokens": 12, "completion_tokens": 3}})
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1", "LLM_MODEL": "test-model", "LLM_API_KEY": "secret-test-token"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            response = await model.generate([{"role": "user", "content": "hello"}], [], 50)
            await model.close()
        self.assertEqual(str(received[0].url), "https://model.example/v1/chat/completions")
        self.assertEqual(received[0].headers["authorization"], "Bearer secret-test-token")
        self.assertNotIn("secret-test-token", received[0].content.decode())
        self.assertNotIn("reasoning_content", response["message"])
        self.assertEqual(response["usage"], {"input_tokens": 12, "output_tokens": 3})

    async def test_private_environment_fields_never_reach_actor(self):
        payloads = []
        state = {"observation_version": "shopping-observation-v2", "page_type": "search_home", "search_available": True, "actions": [], "goal": "PRIVATE-GOAL", "reward": 1}
        def handler(request):
            payload = json.loads(request.content)
            payloads.append(payload)
            if payload["action"] == "reset":
                return httpx.Response(200, json={"result": {"env_idx": 3, "instruction": "Find headphones", "instruction_simple": "PRIVATE-SIMPLE", "goal_options": "PRIVATE-OPTIONS", "_trace_target": "PRIVATE-TARGET", "observation_state": state}})
            if payload["action"] == "interact":
                return httpx.Response(200, json={"result": {"over": True, "done": False, "instruction": "PRIVATE-TERMINAL-REWARD", "goal": "PRIVATE-GOAL", "reward": 1, "reward_detail": {"reward_valid": True, "reward_type": "gold_purchase", "purchase_success": True, "termination_reason": "gold_purchase"}}})
            return httpx.Response(200, json={"result": {}})
        environment = ShopSimulatorEnvironment(transport=httpx.MockTransport(handler))
        frame = await environment.reset("42")
        self.assertEqual(frame["query"], "Find headphones")
        self.assertNotIn("PRIVATE", json.dumps(frame))
        frame = await environment.step("search[headphones]")
        self.assertTrue(frame["done"])
        self.assertNotIn("PRIVATE", frame["observation"])
        self.assertTrue(frame["result"]["strict_success"])
        await environment.release()
        await environment.close()
        self.assertEqual(payloads, [{"action": "reset", "idx": 42}, {"action": "interact", "env_idx": 3, "response": "search[headphones]"}, {"action": "release_one", "env_idx": 3}])


class GuardTests(unittest.TestCase):
    def test_real_lowercase_navigation_labels_are_dispatched_verbatim(self):
        cases = [("view_features", "features"), ("back_to_search", "back to search"), ("next_page", "next >"), ("buy_now", "buy now")]
        for name, advertised in cases:
            with self.subTest(tool=name):
                parsed, action = parse_action(call(name, {}), {"actions": [advertised]})
                self.assertEqual(parsed["tool_name"], name)
                self.assertEqual(action, f"click[{advertised}]")

    def test_product_ids_and_option_values_remain_case_sensitive(self):
        with self.assertRaises(ActionError):
            parse_action(call("open_product", {"asin": "demo-product"}), {"products": [{"asin": "DEMO-PRODUCT"}], "actions": ["DEMO-PRODUCT"]})
        with self.assertRaises(ActionError):
            parse_action(call("select_option", {"value": "xl"}), {"available_options": {"size": ["XL"]}, "actions": ["XL"]})

    def test_unknown_product_is_rejected(self):
        with self.assertRaises(ActionError):
            parse_action(call("open_product", {"asin": "GUESSED"}), {"products": [{"asin": "VISIBLE"}], "actions": ["VISIBLE"]})

    def test_option_must_be_public_and_actionable(self):
        with self.assertRaises(ActionError):
            parse_action(call("select_option", {"value": "red"}), {"available_options": {"Color": ["red"]}, "actions": ["blue"]})

    def test_prompt_state_removes_nested_answer_fields(self):
        state = public_state({"observation_version": "shopping-observation-v2", "page_type": "product_detail", "product": {"asin": "P1", "title": "Product", "target_asin": "PRIVATE"}, "selected_options": {"Color": "red", "answer": "PRIVATE"}, "reward_detail": {"reward": 1}})
        self.assertNotIn("PRIVATE", json.dumps(state))
        self.assertEqual(state["selected_options"], {"Color": "red"})


if __name__ == "__main__":
    unittest.main()
