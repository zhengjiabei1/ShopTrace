"""Offline API preflight contracts; these tests make no live model requests."""
import contextlib
import io
import json
import os
import unittest
from unittest.mock import patch

import httpx

from webagent.cli import main
from webagent.preflight import check_model, configuration

TEST_KEY = "private-preflight-test-key"


def tool_response(stage, *, content=None):
    return httpx.Response(200, json={"choices": [{"message": {
        "role": "assistant", "content": content, "reasoning_content": "private-fake-reasoning",
        "tool_calls": [{"id": f"call-{stage}", "type": "function", "function": {
            "name": "preflight_step", "arguments": json.dumps({"stage": stage})}}],
    }}]})


class ConfigurationTests(unittest.TestCase):
    def test_profile_never_replaces_existing_or_explicit_model(self):
        with patch.dict(os.environ, {"LLM_BASE_URL": "", "LLM_MODEL": ""}):
            self.assertEqual(configuration(deepseek=True), {
                "base_url": "https://api.deepseek.com/v1", "model": "deepseek-flash"})
        with patch.dict(os.environ, {"LLM_BASE_URL": "https://different.example/v1", "LLM_MODEL": "deepseek-v4.1-flash"}):
            self.assertEqual(configuration(deepseek=True)["model"], "deepseek-v4.1-flash")
            self.assertEqual(configuration(deepseek=True)["base_url"], "https://different.example/v1")
            self.assertEqual(configuration(deepseek=True, model="explicit-id")["model"], "explicit-id")

    def test_cli_missing_key_is_safe_and_does_not_connect_workbench(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": ""}), \
                patch("webagent.cli._client", side_effect=AssertionError("must not connect")), \
                patch("webagent.preflight.APIModel", side_effect=AssertionError("must not create API client")), \
                contextlib.redirect_stdout(output):
            result = main(["check-model", "--deepseek"])
        self.assertEqual(result, 1)
        report = json.loads(output.getvalue())
        self.assertEqual(report["code"], "missing_api_key")
        self.assertFalse(report["network_probed"])


class ModelPreflightTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        environment = patch.dict(os.environ, {"LLM_BASE_URL": "", "LLM_MODEL": "", "LLM_API_KEY": TEST_KEY})
        environment.start()
        self.addCleanup(environment.stop)

    async def test_missing_key_stops_before_transport_or_client(self):
        with patch.dict(os.environ, {"LLM_API_KEY": ""}), \
                patch("webagent.preflight.APIModel", side_effect=AssertionError("client must not be constructed")):
            report = await check_model(deepseek=True)
        self.assertEqual(report["code"], "missing_api_key")
        self.assertFalse(report["network_probed"])
        self.assertEqual(report["model_calls"], 0)

    async def test_invalid_endpoint_is_rejected_without_network(self):
        for url in ["https://user:password@api.deepseek.com/v1", "http://remote.example/v1", "https://api.deepseek.com:invalid/v1"]:
            with self.subTest(url=url), patch("webagent.preflight.APIModel", side_effect=AssertionError("client must not be constructed")):
                report = await check_model(base_url=url, model="deepseek-flash")
                self.assertEqual(report["code"], "invalid_configuration")

    async def test_models_then_exact_two_round_history_and_safe_diagnostics(self):
        requests = []

        def provider(request):
            self.assertEqual(request.headers["Authorization"], "Bearer " + TEST_KEY)
            requests.append(request)
            if request.url.path == "/v1/models":
                self.assertEqual(request.method, "GET")
                return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
            self.assertEqual(request.url.path, "/v1/chat/completions")
            body = json.loads(request.content)
            self.assertEqual(body["thinking"], {"type": "disabled"})
            self.assertNotIn("parallel_tool_calls", body)
            self.assertEqual(body["max_tokens"], 128)
            stage = len(requests) - 1
            if stage == 2:
                self.assertEqual(body["messages"][-2]["tool_calls"][0]["id"], "call-1")
                self.assertEqual(body["messages"][-1]["role"], "tool")
                self.assertEqual(body["messages"][-1]["tool_call_id"], "call-1")
                self.assertNotIn("reasoning_content", body["messages"][-2])
            return tool_response(stage, content="provider-private-content")

        report = await check_model(deepseek=True, transport=httpx.MockTransport(provider))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["scope"], "connectivity_tool_contract")
        self.assertTrue(report["models_verified"])
        self.assertEqual(report["model_calls"], 2)
        self.assertEqual(report["tool_rounds_verified"], 2)
        self.assertEqual(report["shopping_tasks_completed"], 0)
        self.assertTrue(report["offline_test_transport"])
        self.assertEqual(len(requests), 3)
        serialized = json.dumps(report)
        for private in [TEST_KEY, "api.deepseek.com", "provider-private-content", "private-fake-reasoning"]:
            self.assertNotIn(private, serialized)

    async def test_account_model_mismatch_does_not_guess_or_call_generation(self):
        requests = []

        def provider(request):
            requests.append(request)
            return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})

        report = await check_model(deepseek=True, model="deepseek-v4.1-flash", transport=httpx.MockTransport(provider))
        self.assertEqual(report["code"], "model_not_available")
        self.assertEqual(report["model_calls"], 0)
        self.assertEqual(len(requests), 1)

    async def test_authentication_error_omits_provider_body_and_stops(self):
        requests = []

        def provider(request):
            requests.append(request)
            return httpx.Response(401, text=TEST_KEY + " provider-debug-body")

        report = await check_model(deepseek=True, transport=httpx.MockTransport(provider))
        self.assertEqual(report["code"], "authentication_failed")
        self.assertEqual(report["http_status"], 401)
        self.assertEqual(len(requests), 1)
        self.assertNotIn(TEST_KEY, json.dumps(report))
        self.assertNotIn("provider-debug-body", json.dumps(report))

    async def test_provider_format_and_tool_stage_failures_are_distinct(self):
        malformed = await check_model(deepseek=True, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"data": "bad-list"})))
        self.assertEqual(malformed["code"], "invalid_models_response")

        def wrong_stage(request):
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
            return tool_response(2)

        report = await check_model(deepseek=True, transport=httpx.MockTransport(wrong_stage))
        self.assertEqual(report["code"], "invalid_tool_response")
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(report["tool_rounds_verified"], 0)

    async def test_forbidden_does_not_misdiagnose_key_as_invalid(self):
        report = await check_model(deepseek=True, transport=httpx.MockTransport(
            lambda _: httpx.Response(403, text="network-policy-private-body")))
        self.assertEqual(report["code"], "access_denied")
        self.assertEqual(report["http_status"], 403)
        self.assertEqual(report["model_calls"], 0)

    async def test_connection_failure_has_no_retry_or_exception_url(self):
        requests = []

        def provider(request):
            requests.append(request)
            raise httpx.ConnectError("private-url.example/" + TEST_KEY, request=request)

        report = await check_model(deepseek=True, transport=httpx.MockTransport(provider))
        self.assertEqual(report["code"], "connection_failed")
        self.assertEqual(len(requests), 1)
        self.assertNotIn("private-url", json.dumps(report))
        self.assertNotIn(TEST_KEY, json.dumps(report))

    async def test_generation_http_error_reports_only_status_no_retry(self):
        requests = []

        def provider(request):
            requests.append(request)
            if request.method == "GET":
                return httpx.Response(200, json={"data": [{"id": "deepseek-flash"}]})
            return httpx.Response(429, text="provider-private-debug-" + TEST_KEY)

        report = await check_model(deepseek=True, transport=httpx.MockTransport(provider))
        self.assertEqual(report["code"], "provider_http_error")
        self.assertEqual(report["http_status"], 429)
        self.assertEqual(report["model_calls"], 1)
        self.assertEqual(len(requests), 2)
        self.assertNotIn(TEST_KEY, json.dumps(report))

    async def test_opencode_go_skips_missing_model_list_and_keeps_session_header(self):
        requests = []
        session = "private-opencode-session"

        def provider(request):
            requests.append(request)
            self.assertEqual(request.headers["x-opencode-session"], session)
            if request.method == "GET":
                self.assertTrue(request.url.path.endswith("/models"))
                return httpx.Response(404, json={"error": "not found"})
            self.assertFalse(request.url.path.endswith("/models"))
            stage = len(requests) - 1
            return tool_response(stage)

        with patch.dict(os.environ, {"LLM_API_KEY": TEST_KEY, "OPENCODE_SESSION_ID": session}):
            report = await check_model(base_url="https://opencode.ai/inference/go/openai/v1",
                                       model="deepseek-v4.1-flash",
                                       transport=httpx.MockTransport(provider))
        self.assertEqual(report["status"], "passed")
        self.assertEqual(report["code"], "tool_contract_verified")
        self.assertFalse(report["models_verified"])
        self.assertFalse(report["models_endpoint_available"])
        self.assertEqual(report["model_calls"], 2)
        self.assertEqual(report["tool_rounds_verified"], 2)
        self.assertEqual(len(requests), 3)
        self.assertNotIn(TEST_KEY, json.dumps(report))
        self.assertNotIn(session, json.dumps(report))


if __name__ == "__main__":
    unittest.main()
