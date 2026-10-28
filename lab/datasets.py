"""Split checks and action-only SFT export; token masking belongs to the trainer."""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
import json
import re
import unicodedata
from typing import Any

from .evaluation import _finite, _infra


PRIVATE_KEYS = {
    "goal", "gold", "reward", "reward_detail", "reward_details", "goal_options",
    "instruction_simple", "_trace_target", "trace_target", "target_product",
    "target_asin", "gold_product", "gold_asin", "hidden_answer", "private_answer",
    "reasoning_content", "reasoning", "analysis", "chain_of_thought",
}
ACTION_TOOLS = {
    "search_products", "open_product", "select_option", "view_description",
    "view_features", "view_reviews", "view_attributes", "next_page", "prev_page",
    "back_to_search", "buy_now", "finish_without_purchase",
}


def _normalize_query(text: Any) -> str:
    text = unicodedata.normalize("NFKC", str(text or "")).casefold()
    return "".join(char for char in text if char.isalnum())


def _shingles(text: str) -> set[str]:
    return {text[index:index + 3] for index in range(max(1, len(text) - 2))} if text else set()


def validate_splits(splits: dict[str, list[dict]]) -> dict:
    """Check duplicate tasks and flag similar queries or shared products."""
    ids: dict[str, set[str]] = defaultdict(set)
    queries: dict[str, list[dict]] = defaultdict(list)
    products: dict[str, set[str]] = defaultdict(set)
    errors = []
    warnings = []
    counts = {}
    for split, entries in splits.items():
        if not isinstance(entries, list):
            errors.append({"split": split, "reason": "split_must_be_a_list"})
            continue
        counts[split] = len(entries)
        seen = set()
        for index, record in enumerate(entries):
            if not isinstance(record, dict):
                errors.append({"split": split, "index": index, "reason": "record_must_be_an_object"})
                continue
            raw_id = record.get("task_id", record.get("id"))
            if raw_id is None or not str(raw_id).strip():
                errors.append({"split": split, "index": index, "reason": "missing_task_id"})
                continue
            task_id = str(raw_id).strip()
            if task_id in seen:
                errors.append({"split": split, "task_id": task_id, "reason": "duplicate_task_within_split"})
            seen.add(task_id)
            ids[task_id].add(split)
            query = _normalize_query(record.get("query", record.get("instruction")))
            if query:
                queries[query].append({"split": split, "task_id": task_id})
            prod_ids = [record.get("product_id"), record.get("asin")]
            if isinstance(record.get("product_ids"), list):
                prod_ids.extend(record["product_ids"])
            for product_id in prod_ids:
                if product_id is not None and str(product_id).strip():
                    products[str(product_id)].add(split)
    task_dups = [{"task_id": task_id, "splits": sorted(parts)}
                     for task_id, parts in ids.items() if len(parts) > 1]
    duplicates = [{"normalized_query": query, "records": records}
                  for query, records in queries.items() if len({item["split"] for item in records}) > 1]
    queries_in = list(queries.items())
    similar = []
    # Cap pairwise comparisons; exact duplicate checks still cover all queries.
    query_cap = 3000
    if len(queries_in) > query_cap:
        warnings.append("Similarity scan limited to first 3000 unique queries; exact ID/query checks remain complete.")
    queries_in = queries_in[:query_cap]
    shingles = {query: _shingles(query) for query, _ in queries_in}
    for index, (left, left_rows) in enumerate(queries_in):
        left_set = shingles[left]
        if len(left) < 8:
            continue
        for right, right_rows in queries_in[index + 1:]:
            if len(right) < 8 or min(len(left), len(right)) / max(len(left), len(right)) < 0.75:
                continue
            if not any(a["split"] != b["split"] for a in left_rows for b in right_rows):
                continue
            right_set = shingles[right]
            score = len(left_set & right_set) / len(left_set | right_set) if left_set | right_set else 0
            if score >= 0.80:
                similar.append({"left": left_rows, "right": right_rows, "similarity": round(score, 4)})
    prod_dups = [{"product_id": product_id, "splits": sorted(parts)}
                        for product_id, parts in products.items() if len(parts) > 1]
    if prod_dups:
        warnings.append("Shared products are a generalization risk, not automatically invalid task splits.")
    if similar:
        warnings.append("Near-duplicate queries require manual template/semantic review.")
    if not queries:
        warnings.append("No query text was supplied; query/template leakage could not be assessed.")
    return {
        "schema_version": "shopping-lab.splits.v1",
        "valid": not (errors or task_dups or duplicates),
        "counts": counts, "task_id_overlaps": task_dups,
        "query_duplicates": duplicates, "similar_queries": similar,
        "product_overlaps": prod_dups, "errors": errors, "warnings": warnings,
        "similarity_method": "NFKC/casefold alphanumeric normalization, character-trigram Jaccard >= 0.80",
        "similarity_unique_queries_checked": len(queries_in),
    }


def _scrub(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: _scrub(item) for key, item in value.items()
                if str(key).casefold() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str) and value.lstrip().startswith(("{", "[")):
        try:
            parsed = json.loads(value)
        except (ValueError, TypeError):
            return value
        return json.dumps(_scrub(parsed), ensure_ascii=False)
    return value


def _calls(message: dict) -> list[dict]:
    calls = message.get("tool_calls")
    if not isinstance(calls, list) or len(calls) != 1:
        return []
    call = calls[0]
    if not isinstance(call, dict):
        return []
    function = call.get("function") or {}
    if function.get("name") not in ACTION_TOOLS:
        return []
    arguments = function.get("arguments", "{}")
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except ValueError:
            return []
    if not isinstance(arguments, dict):
        return []
    return [{"id": str(call.get("id") or "action"), "type": "function", "function": {
        "name": function["name"], "arguments": json.dumps(_scrub(arguments), ensure_ascii=False),
    }}]


def _safe_messages(messages: Any) -> list[dict]:
    if not isinstance(messages, list):
        return []
    safe = []
    for message in messages:
        if not isinstance(message, dict):
            return []
        role = message.get("role")
        if role in {"system", "user", "tool"}:
            content = _scrub(message.get("content"))
            if content is not None and not isinstance(content, str):
                content = json.dumps(content, ensure_ascii=False)
            cleaned = {"role": role, "content": content}
            if role == "tool":
                cleaned["tool_call_id"] = str(message.get("tool_call_id") or "")
            safe.append(cleaned)
        elif role == "assistant":
            calls = _calls(message)
            if calls:
                safe.append({"role": "assistant", "content": None, "tool_calls": calls})
            # Retain tool calls only.
    return safe


def _eligible(run: dict) -> str | None:
    if run.get("mode") != "shopsimulator":
        return "non_benchmark_environment"
    if run.get("source") != "live" or run.get("imported"):
        return "unverified_or_imported_source"
    if (run.get("model") or {}).get("provider") != "api":
        return "not_a_live_model"
    if run.get("status") != "completed":
        return "not_completed"
    if run.get("error") or _infra(run):
        return "execution_error"
    result = run.get("result") or {}
    if result.get("reward_valid") is not True or not _finite(result.get("reward")):
        return "unverified_terminal_reward"
    if result.get("strict_success") is not True and result.get("purchase_success") is not True:
        return "no_verified_success"
    if run.get("task_id") is None:
        return "missing_task_id"
    return None


def build_sft_records(trajectories: list[dict], split_manifest: dict | None = None) -> tuple[list[dict], dict]:
    """Export successful request/action pairs and record rejected trajectories."""
    split_info = validate_splits(split_manifest) if split_manifest is not None else None
    task_split = {}
    if split_manifest is not None and split_info and split_info["valid"]:
        for split, entries in split_manifest.items():
            for item in entries:
                task_split[str(item.get("task_id", item.get("id"))).strip()] = split
    output = []
    rejections = []
    accepted = 0
    task_ids = set()
    for trajectory in trajectories:
        run = trajectory.get("run") or {}
        reason = _eligible(run)
        task_id = str(run.get("task_id")).strip()
        if split_info and not split_info["valid"]:
            reason = "invalid_split_manifest"
        elif split_manifest is not None and task_id not in task_split:
            reason = "task_not_in_split_manifest"
        elif str(task_split.get(task_id, "")).casefold() in {"test", "testing", "eval", "evaluation", "benchmark", "holdout", "heldout", "held_out"}:
            reason = "held_out_test_task"
        if reason:
            rejections.append({"run_id": run.get("run_id"), "reason": reason})
            continue
        samples = []
        request = None
        pending = None
        executed = False
        for event in trajectory.get("events", []):
            payload = event.get("payload") or {}
            if event.get("type") == "model_request":
                request = payload
                pending = None
                executed = False
            elif event.get("type") == "model_response" and request is not None:
                message = payload.get("message") or {}
                calls = _calls(message)
                messages = _safe_messages(request.get("messages"))
                if calls and messages:
                    pending = {
                        "messages": messages + [{"role": "assistant", "content": None, "tool_calls": calls}],
                        "tools": _scrub(deepcopy(request.get("tools") or [])),
                        "metadata": {
                            "task_id": task_id, "run_id": run.get("run_id"),
                            "response_event_id": event.get("event_id"),
                            "split": task_split.get(task_id, "unassigned"),
                            "loss_mask_policy": "assistant_action_tokens_only_required",
                            "loss_mask_applied": False,
                        },
                    }
                request = None
            elif event.get("type") == "action" and pending is not None:
                action = pending["messages"][-1]["tool_calls"][0]["function"]
                # Match the response to the dispatched action.
                executed = (
                    payload.get("tool_name") == action["name"]
                    and payload.get("arguments") == json.loads(action["arguments"])
                )
            elif event.get("type") == "environment_result" and pending is not None:
                if executed and not payload.get("error"):
                    samples.append(pending)
                pending = None
                executed = False
            elif event.get("type") == "error":
                pending = None
                executed = False
        if not samples:
            rejections.append({"run_id": run.get("run_id"), "reason": "no_valid_request_action_pairs"})
            continue
        output.extend(samples)
        accepted += 1
        task_ids.add(task_id)
    return output, {
        "schema_version": "shopping-lab.sft-export.v1",
        "accepted_trajectories": accepted,
        "rejected_trajectories": len(rejections),
        "records": len(output), "task_ids": sorted(task_ids), "rejections": rejections,
        "split_validation": split_info,
        "loss_mask_policy": "assistant_action_tokens_only_required",
        "loss_mask_applied": False,
        "training_ready": False,
        "warnings": [
            "Apply the assistant-action token mask in the trainer.",
            "Check rendered messages, tool-result pairing and truncation before training.",
            "Review free text for private facts; field filtering only removes named keys.",
        ] + (["Assign train/validation splits before training."]
             if split_manifest is None else []),
    }
