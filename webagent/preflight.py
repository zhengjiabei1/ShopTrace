"""Model availability and two-round tool preflight."""
from __future__ import annotations

import asyncio
import json
import os
import re
from urllib.parse import urlsplit

import httpx

from lab.models import APIModel, ModelError, ModelProtocolError

DEEPSEEK_BASE_URL = "https://api.deepseek.com/v1"
DEEPSEEK_MODEL = "deepseek-flash"
TOOL_NAME = "preflight_step"
TOOLS = [{"type": "function", "function": {
    "name": TOOL_NAME,
    "description": "Acknowledge the requested connectivity-check stage. This does not execute a browser task.",
    "parameters": {"type": "object", "properties": {"stage": {"type": "integer", "enum": [1, 2]}},
                   "required": ["stage"], "additionalProperties": False},
}}]


def _http_error_code(status):
    # A 403 may reflect account or network policy restrictions.
    return "authentication_failed" if status == 401 else "access_denied" if status == 403 else "provider_http_error"


def configuration(*, deepseek=False, base_url=None, model=None):
    """Resolve arguments, environment, then profile defaults."""
    return {
        "base_url": (base_url if base_url is not None else os.environ.get("LLM_BASE_URL")
                     or (DEEPSEEK_BASE_URL if deepseek else "")).strip().rstrip("/"),
        "model": (model if model is not None else os.environ.get("LLM_MODEL")
                  or (DEEPSEEK_MODEL if deepseek else "")).strip(),
    }


def _validate_call(message, expected_stage):
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        raise ModelProtocolError("expected one preflight tool call")
    call = calls[0]
    if (not isinstance(call, dict) or call.get("type") != "function"
            or not isinstance(call.get("id"), str) or not 1 <= len(call["id"]) <= 256):
        raise ModelProtocolError("invalid preflight tool call")
    function = call.get("function")
    if not isinstance(function, dict) or function.get("name") != TOOL_NAME:
        raise ModelProtocolError("unexpected preflight tool")
    try:
        arguments = json.loads(function["arguments"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ModelProtocolError("invalid preflight arguments") from exc
    if (not isinstance(arguments, dict) or set(arguments) != {"stage"}
            or type(arguments["stage"]) is not int or arguments["stage"] != expected_stage):
        raise ModelProtocolError("unexpected preflight stage")
    return call["id"]


async def check_model(*, deepseek=False, base_url=None, model=None, transport=None):
    """Check models and two tool rounds without retries or task scoring.

    Reads LLM_API_KEY; diagnostics omit credentials and provider payloads.
    ``transport`` selects a labelled offline test provider.
    """
    config = configuration(deepseek=deepseek, base_url=base_url, model=model)
    report = {"schema_version": "shoptrace-webagent-model-check-v1",
              "scope": "connectivity_tool_contract", "status": "blocked", "code": None,
              "network_probed": False, "models_verified": False, "model_calls": 0,
              "tool_rounds_verified": 0, "browser_started": False, "shopping_tasks_completed": 0,
              "offline_test_transport": transport is not None}

    def finish(code, *, status="blocked", http_status=None):
        report.update(code=code, status=status)
        if http_status is not None:
            report["http_status"] = http_status
        return report

    if not config["base_url"] or not config["model"]:
        return finish("missing_configuration")
    try:
        parsed = urlsplit(config["base_url"])
        _ = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or (parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"})):
            return finish("invalid_configuration")
    except ValueError:
        return finish("invalid_configuration")
    if not os.environ.get("LLM_API_KEY", "").strip() and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
        return finish("missing_api_key")
    if parsed.hostname == "opencode.ai" and not os.environ.get("OPENCODE_SESSION_ID", "").strip():
        return finish("missing_opencode_session")

    client = None
    try:
        client = APIModel(config["model"], base_url=config["base_url"], transport=transport)
        async with asyncio.timeout(60):
            report["network_probed"] = True
            response = await client.client.get("models")
            if response.status_code != 200:
                # OpenCode Go exposes chat completions but not a model list.
                # The two tool rounds still verify the selected model.
                if parsed.hostname == "opencode.ai" and response.status_code == 404:
                    report["models_endpoint_available"] = False
                else:
                    return finish(_http_error_code(response.status_code),
                                  status="failed", http_status=response.status_code)
            else:
                try:
                    data = response.json()["data"]
                    if not isinstance(data, list) or any(not isinstance(item, dict) or not isinstance(item.get("id"), str) for item in data):
                        raise ValueError()
                except (KeyError, TypeError, ValueError):
                    return finish("invalid_models_response", status="failed")
                if not any(item["id"] == config["model"] for item in data):
                    return finish("model_not_available", status="failed")
                report["models_verified"] = True
                report["models_endpoint_available"] = True
            messages = [
                {"role": "system", "content": "This is an API tool-connectivity check. Call preflight_step exactly once with stage 1. After its tool result, call it exactly once with stage 2. Do not perform any other task."},
                {"role": "user", "content": "Start the two-stage check by calling preflight_step with stage 1."},
            ]
            prev_id = None
            for stage in (1, 2):
                report["model_calls"] += 1
                generated = await client.generate(messages, TOOLS, 128)
                message = generated["message"]
                call_id = _validate_call(message, stage)
                if call_id == prev_id:
                    raise ModelProtocolError("duplicate tool call id")
                report["tool_rounds_verified"] += 1
                messages.append(message)
                messages.append({"role": "tool", "tool_call_id": call_id,
                                 "content": json.dumps({"accepted_stage": stage, "next_stage": stage + 1 if stage == 1 else None})})
                prev_id = call_id
            return finish("tool_contract_verified", status="passed")
    except ModelProtocolError:
        return finish("invalid_tool_response", status="failed")
    except (httpx.HTTPError, TimeoutError):
        return finish("connection_failed", status="failed")
    except ModelError as exc:
        # Retain only APIModel's status code, never provider details.
        match = re.search(r"returned HTTP (\d{3});", str(exc))
        if match:
            code = int(match.group(1))
            return finish(_http_error_code(code),
                          status="failed", http_status=code)
        return finish("connection_failed", status="failed")
    finally:
        if client is not None:
            await client.close()
