"""Independent public HTTP adapter and isolated synthetic development environment."""
from __future__ import annotations

import json
import math
import os
from copy import deepcopy
from urllib.parse import urlparse

import httpx

from .fixtures import get_task


class EnvironmentError(RuntimeError):
    pass


class EnvironmentProtocolError(EnvironmentError):
    pass


# Only these public fields may enter model observations.
STATE_KEYS = {"observation_version", "page_type", "search_available", "actions", "query", "normalized_query", "page", "total_pages", "total_results", "rank_start", "rank_end", "selected_price", "selected_options", "available_options", "subpage", "content"}
PRODUCT_KEYS = {"asin", "title", "brand", "category", "price", "key_attributes", "rank"}


def public_state(raw: dict) -> dict:
    if not isinstance(raw, dict):
        raise EnvironmentProtocolError("environment is missing structured public observation_state")
    version = raw.get("observation_version")
    if version != "shopping-observation-v2":
        raise EnvironmentProtocolError("environment observation version differs from the pinned v2 contract")
    state = {key: _public_value(raw[key]) for key in STATE_KEYS if key in raw}
    for key in ("product",):
        if key in raw:
            if not isinstance(raw[key], dict):
                raise EnvironmentProtocolError("public product must be an object")
            state[key] = {k: _public_value(v) for k, v in raw[key].items() if k in PRODUCT_KEYS}
    if "products" in raw:
        if not isinstance(raw["products"], list):
            raise EnvironmentProtocolError("public products must be a list")
        state["products"] = [{k: _public_value(v) for k, v in p.items() if k in PRODUCT_KEYS} for p in raw["products"] if isinstance(p, dict)]
    if state.get("page_type") not in {"search_home", "search_results", "product_detail", "information_subpage", "terminal"}:
        raise EnvironmentProtocolError("unsupported public page type")
    return state


def _public_value(value):
    if isinstance(value, dict):
        # Copy public option axes and values only.
        blocked = {"goal", "goal_options", "instruction_simple", "target_asin", "target", "answer", "gold", "reward", "reward_detail", "_trace_target"}
        return {str(k): _public_value(v) for k, v in value.items() if str(k).lower() not in blocked}
    if isinstance(value, list):
        return [_public_value(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise EnvironmentProtocolError("unsupported public state value")


def render_state(state: dict) -> str:
    return "[Actor-visible shopping state]\n" + json.dumps(state, ensure_ascii=False, sort_keys=True)


def terminal_result(raw: dict) -> dict:
    detail = raw.get("reward_detail") or {}
    if not isinstance(detail, dict):
        detail = {}
    reward = raw.get("reward")
    if not isinstance(reward, (int, float)) or isinstance(reward, bool) or not math.isfinite(reward):
        reward = None
    valid = detail.get("reward_valid") is True
    reward_tag = detail.get("reward_type")
    return {"reward": reward, "reward_valid": valid, "reward_type": reward_tag if isinstance(reward_tag, str) else None, "reward_version": detail.get("reward_version") if isinstance(detail.get("reward_version"), str) else None, "strict_success": (reward_tag == "gold_purchase") if valid else None, "purchase_success": detail.get("purchase_success") if valid and type(detail.get("purchase_success")) is bool else None, "termination_reason": detail.get("termination_reason") if isinstance(detail.get("termination_reason"), str) else None}


class ShopSimulatorEnvironment:
    def __init__(self, *, transport=None):
        base_url = os.getenv("SHOPSIM_BASE_URL", "http://127.0.0.1:5700").rstrip("/")
        parsed = urlparse(base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise EnvironmentError("SHOPSIM_BASE_URL must be an HTTP(S) base URL without embedded credentials")
        self.client = httpx.AsyncClient(base_url=base_url + "/", timeout=30, transport=transport, follow_redirects=False)
        self.env_idx = None
        self.done = False
        self.query = ""

    async def _call(self, payload: dict) -> dict:
        try:
            response = await self.client.post("api/shop_agent", json=payload)
        except httpx.HTTPError as exc:
            raise EnvironmentError("ShopSimulator request failed; action outcome may be unknown; no retry performed") from exc
        if response.status_code != 200:
            raise EnvironmentError(f"ShopSimulator returned HTTP {response.status_code}; response body omitted")
        try:
            result = response.json()["result"]
        except (KeyError, ValueError, TypeError) as exc:
            raise EnvironmentProtocolError("ShopSimulator response must contain object result") from exc
        if not isinstance(result, dict):
            raise EnvironmentProtocolError("ShopSimulator result must be an object")
        if result.get("error"):
            raise EnvironmentError("ShopSimulator reported an environment error; private response omitted")
        return result

    async def reset(self, task_id: str) -> dict:
        try:
            idx = int(task_id)
            if idx < 0:
                raise ValueError()
        except (TypeError, ValueError) as exc:
            raise EnvironmentError("ShopSimulator requires a non-negative predefined task_id") from exc
        raw = await self._call({"action": "reset", "idx": idx})
        if type(raw.get("env_idx")) is not int:
            raise EnvironmentProtocolError("reset did not return integer env_idx")
        self.env_idx = raw["env_idx"]
        self.query = raw.get("instruction", "")
        if not isinstance(self.query, str) or not self.query.strip():
            raise EnvironmentProtocolError("reset did not return public instruction")
        return self._frame(raw)

    async def step(self, action: str) -> dict:
        if self.env_idx is None or self.done:
            raise EnvironmentError("environment is not available for another action")
        raw = await self._call({"action": "interact", "env_idx": self.env_idx, "response": action})
        return self._frame(raw)

    def _frame(self, raw: dict) -> dict:
        self.done = raw.get("done") is True or raw.get("over") is True
        # Terminal pages may contain private reward text.
        if self.done:
            state = {"observation_version": "shopping-observation-v2", "page_type": "terminal", "actions": [], "search_available": False}
        else:
            state = public_state(raw.get("observation_state"))
        return {"query": self.query, "observation": render_state(state), "state": state, "done": self.done, "result": terminal_result(raw) if self.done else None}

    async def release(self):
        if self.env_idx is not None:
            await self._call({"action": "release_one", "env_idx": self.env_idx})
            self.env_idx = None

    async def close(self):
        await self.client.aclose()


class FixtureEnvironment:
    """Synthetic state machine for fixture tests."""
    def __init__(self):
        self.task = None
        self.state = None
        self.selected = []
        self.done = False
        self.result = None

    async def reset(self, task_id: str) -> dict:
        self.task = get_task(task_id)
        self.state = {"observation_version": "shopping-observation-v2", "page_type": "search_home", "search_available": True, "actions": []}
        return self._frame()

    async def step(self, action: str) -> dict:
        if self.done:
            raise EnvironmentError("fixture already terminated")
        if action.startswith("search["):
            self.selected = []
            self.state = {"observation_version": "shopping-observation-v2", "page_type": "search_results", "query": action[7:-1], "products": deepcopy(self.task["products"]), "search_available": True, "actions": [p["asin"] for p in self.task["products"]]}
        elif action.startswith("finish["):
            self._finish(False)
        elif action.startswith("click["):
            target = action[6:-1]
            product = next((p for p in self.task["products"] if p["asin"] == target), None)
            if product:
                self.selected = []
                self.state = {"observation_version": "shopping-observation-v2", "page_type": "product_detail", "product": deepcopy(product), "selected_options": {}, "available_options": {"Options": self.task["options"]}, "selected_price": product["price"], "actions": ["Features", "Description", "Reviews", "Attributes", "Buy Now", "Back to Search", *self.task["options"]], "search_available": False}
            elif target == "Buy Now":
                success = self.state.get("product", {}).get("asin") == self.task["product"] and set(self.selected) == set(self.task["options"]) and not self.task.get("abstain")
                self._finish(success)
            elif target in self.task["options"]:
                if target not in self.selected:
                    self.selected.append(target)
                self.state["selected_options"] = {str(index + 1): value for index, value in enumerate(self.selected)}
            elif target in {"Features", "Description", "Reviews", "Attributes"}:
                self.state["page_type"] = "information_subpage"
                self.state["subpage"] = target
                self.state["content"] = "Fixture product features: " + ", ".join(self.state["product"]["key_attributes"])
            elif target == "Back to Search":
                self.state = {"observation_version": "shopping-observation-v2", "page_type": "search_home", "search_available": True, "actions": []}
            else:
                raise EnvironmentError("unsupported fixture action")
        else:
            raise EnvironmentError("invalid fixture action")
        return self._frame()

    def _finish(self, purchased: bool):
        self.done = True
        self.result = {"reward": None, "reward_valid": False, "strict_success": None, "purchase_success": None, "fixture_success": bool(purchased or self.task.get("abstain")), "fixture_purchase": purchased, "termination_reason": "fixture_purchase" if purchased else "fixture_abstain", "source": "synthetic_fixture"}
        self.state = {"observation_version": "shopping-observation-v2", "page_type": "terminal", "search_available": False, "actions": []}

    def _frame(self):
        return {"query": self.task["query"], "observation": render_state(self.state), "state": deepcopy(self.state), "done": self.done, "result": deepcopy(self.result)}

    async def release(self):
        return None

    async def close(self):
        return None
