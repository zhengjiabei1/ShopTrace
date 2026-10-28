"""One-step model clients; credentials never enter trajectory records."""
from __future__ import annotations

import json
import os
from urllib.parse import urlparse

import httpx

from .fixtures import scripted_plan


class ModelError(RuntimeError):
    pass


class ModelProtocolError(ModelError):
    pass


class ScriptedModel:
    """Fixed actions for fixture tests."""
    def __init__(self, task_id: str):
        self.plan = scripted_plan(task_id)
        self.index = 0

    async def generate(self, messages: list, tools: list, max_output_tokens: int) -> dict:
        if self.index >= len(self.plan):
            raise ModelProtocolError("fixture script exhausted before environment completion")
        name, arguments = self.plan[self.index]
        self.index += 1
        return {
            "message": {"role": "assistant", "content": "Scripted test action.", "tool_calls": [{"id": f"fixture-call-{self.index}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)}}]},
            "usage": {"input_tokens": None, "output_tokens": None},
        }

    async def close(self):
        return None


class APIModel:
    def __init__(self, name: str | None = None, *, transport=None, base_url: str | None = None):
        base_url = (base_url if base_url is not None else os.getenv("LLM_BASE_URL", "")).strip().rstrip("/")
        self.name = name or os.getenv("LLM_MODEL", "").strip()
        key = os.getenv("LLM_API_KEY", "").strip()
        opencode_session = os.getenv("OPENCODE_SESSION_ID", "").strip()
        if not base_url or not self.name:
            raise ModelError("configure LLM_BASE_URL and LLM_MODEL before API execution")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ModelError("LLM_BASE_URL must be an HTTP(S) base URL without credentials, query or fragment")
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ModelError("non-local model services require HTTPS (use a local SSH tunnel for remote HTTP)")
        if not key and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ModelError("configure LLM_API_KEY for the remote model service")
        is_opencode = parsed.hostname == "opencode.ai"
        if is_opencode and not opencode_session:
            raise ModelError("configure OPENCODE_SESSION_ID for the OpenCode Go endpoint")
        # DeepSeek and OpenCode Go require thinking disabled for required tool calls.
        # Their APIs omit parallel_tool_calls; the runtime enforces one action.
        self.request_options = ({"tool_choice": "required", "thinking": {"type": "disabled"}}
                                if parsed.hostname in {"api.deepseek.com", "opencode.ai"}
                                else {"tool_choice": "required", "parallel_tool_calls": False})
        headers = {"Authorization": f"Bearer {key}"} if key else {}
        if is_opencode:
            headers["x-opencode-session"] = opencode_session
        self.client = httpx.AsyncClient(base_url=base_url + "/", headers=headers, timeout=httpx.Timeout(30), transport=transport, follow_redirects=False)

    async def generate(self, messages: list, tools: list, max_output_tokens: int) -> dict:
        try:
            response = await self.client.post("chat/completions", json={"model": self.name, "messages": messages, "tools": tools, "temperature": 0, "max_tokens": max_output_tokens, **self.request_options})
        except httpx.HTTPError as exc:
            raise ModelError("model service connection failed; no automatic retry was performed") from exc
        if response.status_code != 200:
            raise ModelError(f"model service returned HTTP {response.status_code}; provider response body omitted")
        try:
            data = response.json()
            incoming = data["choices"][0]["message"]
            if not isinstance(incoming, dict):
                raise ValueError("invalid message")
            # Keep only chat content and tool calls.
            message = {key: incoming[key] for key in ("role", "content", "tool_calls") if key in incoming}
            message["role"] = "assistant"
            if message.get("content") is not None and not isinstance(message.get("content"), str):
                raise ValueError("unsupported assistant content")
            usage = data.get("usage") or {}
            return {"message": message, "usage": {"input_tokens": _tokens(usage.get("prompt_tokens")), "output_tokens": _tokens(usage.get("completion_tokens"))}}
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ModelProtocolError("model service response does not match the chat-completions protocol") from exc

    async def close(self):
        await self.client.aclose()


def _tokens(value):
    return value if type(value) is int and value >= 0 else None
