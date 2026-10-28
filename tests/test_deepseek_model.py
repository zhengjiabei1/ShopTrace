"""Mocked provider-contract checks; these do not call or benchmark DeepSeek."""
import json
import os
import unittest
from unittest.mock import patch

import httpx

from lab.models import APIModel, ModelError, ModelProtocolError


def assistant(name, call_id):
    return {"role": "assistant", "content": None, "tool_calls": [{
        "id": call_id, "type": "function", "function": {
            "name": name, "arguments": "{}"}}]}


class DeepSeekModelTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tool_rounds_use_nonthinking_without_retaining_reasoning(self):
        requests = []
        secret = "mock-provider-credential"
        reasoning = "private-provider-reasoning"
        tools = [{"type": "function", "function": {"name": "browser_back",
                  "parameters": {"type": "object", "properties": {}}}}]

        def handler(request):
            payload = json.loads(request.content)
            requests.append(payload)
            self.assertEqual(str(request.url), "https://api.deepseek.com/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer " + secret)
            self.assertNotIn(secret, request.content.decode())
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertEqual(payload["tool_choice"], "required")
            self.assertNotIn("parallel_tool_calls", payload)
            self.assertEqual(payload["model"], "deepseek-flash")
            self.assertEqual(payload["tools"], tools)
            if len(requests) == 2:
                self.assertEqual(payload["messages"][-2], assistant("browser_back", "call-one"))
                self.assertEqual(payload["messages"][-1], {
                    "role": "tool", "tool_call_id": "call-one", "content": "visible DOM after going back"})
                self.assertNotIn(reasoning, request.content.decode())
                self.assertNotIn("reasoning_content", request.content.decode())
            message = assistant("browser_back" if len(requests) == 1 else "finish",
                                "call-one" if len(requests) == 1 else "call-two")
            return httpx.Response(200, json={"choices": [{"message": {
                **message, "reasoning_content": reasoning}}],
                "usage": {"prompt_tokens": 20, "completion_tokens": 5}})

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": secret}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                history = [{"role": "user", "content": "Use the visible browser controls."}]
                first = await model.generate(history, tools, 128)
                history.extend([first["message"], {"role": "tool", "tool_call_id": "call-one",
                                "content": "visible DOM after going back"}])
                second = await model.generate(history, tools, 128)
            finally:
                await model.close()
        self.assertEqual(len(requests), 2)
        self.assertEqual(second["message"], assistant("finish", "call-two"))
        self.assertEqual(second["usage"], {"input_tokens": 20, "output_tokens": 5})
        self.assertNotIn(reasoning, json.dumps([first, second]))
        self.assertNotIn(secret, json.dumps([first, second]))

    async def test_generic_and_lookalike_hosts_keep_original_request_profile(self):
        for base in ("https://model.example/v1", "https://api.deepseek.com.example/v1"):
            with self.subTest(base=base):
                requests = []

                def handler(request):
                    requests.append(json.loads(request.content))
                    return httpx.Response(200, json={"choices": [{"message": assistant("finish", "one")} ]})

                with patch.dict(os.environ, {"LLM_BASE_URL": base, "LLM_MODEL": "generic-model",
                                "LLM_API_KEY": "mock-key"}):
                    model = APIModel(transport=httpx.MockTransport(handler))
                    try:
                        await model.generate([], [], 64)
                    finally:
                        await model.close()
                self.assertEqual(requests[0], {"model": "generic-model", "messages": [], "tools": [],
                    "tool_choice": "required", "temperature": 0, "max_tokens": 64,
                    "parallel_tool_calls": False})

    async def test_configured_model_identifier_is_not_silently_changed(self):
        requests = []

        def handler(request):
            requests.append(json.loads(request.content))
            return httpx.Response(200, json={"choices": [{"message": assistant("finish", "one")} ]})

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "user-selected-identifier", "LLM_API_KEY": "mock-key"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                await model.generate([], [], 64)
            finally:
                await model.close()
        self.assertEqual(requests[0]["model"], "user-selected-identifier")

    async def test_explicit_base_url_does_not_mutate_process_configuration(self):
        requests = []

        def handler(request):
            requests.append(request)
            return httpx.Response(200, json={"choices": [{"message": assistant("finish", "one")}]})

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://model.example/v1",
                        "LLM_MODEL": "configured-model", "LLM_API_KEY": "mock-key"}):
            model = APIModel("deepseek-flash", base_url="https://api.deepseek.com/v1",
                             transport=httpx.MockTransport(handler))
            try:
                await model.generate([], [], 64)
            finally:
                await model.close()
            self.assertEqual(os.environ["LLM_BASE_URL"], "https://model.example/v1")
            self.assertEqual(os.environ["LLM_MODEL"], "configured-model")
        self.assertEqual(requests[0].url.host, "api.deepseek.com")
        self.assertEqual(json.loads(requests[0].content)["thinking"], {"type": "disabled"})

    async def test_missing_remote_key_stops_before_a_request(self):
        requests = []
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": ""}):
            with self.assertRaisesRegex(ModelError, "configure LLM_API_KEY"):
                APIModel(transport=httpx.MockTransport(lambda request: requests.append(request)))
        self.assertEqual(requests, [])

    async def test_provider_error_omits_response_body(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(400, text="private-provider-body and mock-credential")

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "mock-credential"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ModelError) as raised:
                    await model.generate([], [], 64)
            finally:
                await model.close()
        self.assertEqual(len(calls), 1)
        self.assertIn("HTTP 400", str(raised.exception))
        self.assertNotIn("private-provider-body", str(raised.exception))
        self.assertNotIn("mock-credential", str(raised.exception))

    async def test_connection_error_is_sanitized_and_not_retried(self):
        calls = []

        def handler(request):
            calls.append(request)
            raise httpx.ConnectError("private-provider-diagnostic mock-credential", request=request)

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "mock-credential"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ModelError) as raised:
                    await model.generate([], [], 64)
            finally:
                await model.close()
        self.assertEqual(len(calls), 1)
        self.assertIn("no automatic retry", str(raised.exception))
        self.assertNotIn("private-provider-diagnostic", str(raised.exception))
        self.assertNotIn("mock-credential", str(raised.exception))

    async def test_malformed_provider_json_is_not_reported_as_success(self):
        def handler(request):
            return httpx.Response(200, json={"choices": [{"message": "private-provider-body"}]})

        with patch.dict(os.environ, {"LLM_BASE_URL": "https://api.deepseek.com/v1",
                        "LLM_MODEL": "deepseek-flash", "LLM_API_KEY": "mock-key"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                with self.assertRaises(ModelProtocolError) as raised:
                    await model.generate([], [], 64)
            finally:
                await model.close()
        self.assertNotIn("private-provider-body", str(raised.exception))

    async def test_opencode_go_uses_session_header_and_nonthinking_body(self):
        requests = []

        def handler(request):
            requests.append(request)
            self.assertEqual(str(request.url), "https://opencode.ai/inference/go/openai/v1/chat/completions")
            self.assertEqual(request.headers["authorization"], "Bearer opencode-key")
            self.assertEqual(request.headers["x-opencode-session"], "session-value")
            payload = json.loads(request.content)
            self.assertEqual(payload["thinking"], {"type": "disabled"})
            self.assertEqual(payload["tool_choice"], "required")
            self.assertNotIn("parallel_tool_calls", payload)
            self.assertNotIn("opencode-key", request.content.decode())
            self.assertNotIn("session-value", request.content.decode())
            return httpx.Response(200, json={"choices": [{"message": assistant("finish", "one")} ]})

        with patch.dict(os.environ, {
                "LLM_BASE_URL": "https://opencode.ai/inference/go/openai/v1",
                "LLM_MODEL": "deepseek-v4.1-flash",
                "LLM_API_KEY": "opencode-key",
                "OPENCODE_SESSION_ID": "session-value"}):
            model = APIModel(transport=httpx.MockTransport(handler))
            try:
                await model.generate([], [], 64)
            finally:
                await model.close()
        self.assertEqual(len(requests), 1)

    async def test_opencode_go_requires_a_session_header(self):
        requests = []
        with patch.dict(os.environ, {
                "LLM_BASE_URL": "https://opencode.ai/inference/go/openai/v1",
                "LLM_MODEL": "deepseek-v4.1-flash",
                "LLM_API_KEY": "opencode-key",
                "OPENCODE_SESSION_ID": ""}):
            with self.assertRaisesRegex(ModelError, "OPENCODE_SESSION_ID"):
                APIModel(transport=httpx.MockTransport(lambda request: requests.append(request)))
        self.assertEqual(requests, [])


if __name__ == "__main__":
    unittest.main()
