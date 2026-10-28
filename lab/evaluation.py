"""Summaries and protocol comparisons for recorded shopping runs."""

from __future__ import annotations

from collections import Counter, defaultdict
from hashlib import sha256
import json
import math
from statistics import mean
from typing import Any


FINAL_STATUSES = {"completed", "failed", "cancelled", "budget_exceeded"}
INFRA_KINDS = {
    "environment", "environment_error", "env_error", "model", "model_error",
    "transport", "transport_error", "protocol", "protocol_error",
    "infrastructure", "infrastructure_error", "http", "http_error",
    "configuration", "configuration_error", "credential_error",
    "runtime", "release", "environment_cleanup", "model_cleanup", "interrupted",
}


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _infra(run: dict) -> bool:
    error = run.get("error") or {}
    kind = str(error.get("kind", "")).lower()
    return kind in INFRA_KINDS or kind.startswith(("http_", "infrastructure_"))


def _outcome(run: dict, field: str) -> bool | None:
    result = run.get("result") or {}
    # Success labels require a valid terminal reward.
    if result.get("reward_valid") is not True:
        return None
    value = result.get(field)
    return value if isinstance(value, bool) else None


def _rate(values: list[Any], denominator: int | None = None) -> dict:
    known = [value for value in values if isinstance(value, bool)]
    count = len(values) if denominator is None else denominator
    numerator = sum(value is True for value in known)
    return {
        "numerator": numerator,
        "denominator": count,
        "value": numerator / count if count and known else None,
        "known": len(known),
        "unknown": len(values) - len(known),
        "coverage": len(known) / len(values) if values else None,
        "known_only_value": numerator / len(known) if known else None,
        "possible_upper_value": (numerator + len(values) - len(known)) / count if count else None,
        "interpretation": "confirmed_success_fraction; a lower bound when outcomes are unknown",
    }


def _measure(values: list[Any]) -> dict:
    known = [float(value) for value in values if _finite(value)]
    return {
        "mean": mean(known) if known else None,
        "known": len(known), "unknown": len(values) - len(known),
    }


def _token_measure(runs: list[dict], field: str) -> dict:
    values = [(run.get("usage") or {}).get(field) for run in runs]
    known = [value for value in values if _finite(value) and value >= 0]
    return {
        "total": sum(known) if len(known) == len(values) and values else None,
        "known_sum": sum(known), "known": len(known),
        "unknown": len(values) - len(known),
    }


def _protocol(run: dict) -> dict:
    return {
        "mode": run.get("mode"),
        "environment_version": run.get("environment_version"),
        "upstream_commit": run.get("upstream_commit"),
        "data_version": run.get("data_version"),
        "tool_version": run.get("tool_version"),
        "prompt_version": run.get("prompt_version"),
        "observation_version": run.get("observation_version"),
        "environment_observation_version": run.get("environment_observation_version"),
        "observation_adapter_version": run.get("observation_adapter_version"),
        "protocol_version": run.get("protocol_version"),
        "reward_version": run.get("reward_version"),
        "prompt_hash": run.get("prompt_hash"),
        "tools_hash": run.get("tools_hash"),
        "context_policy": run.get("context_policy"),
        "strategy": run.get("strategy", "default"),
        "runtime": run.get("runtime") or {},
    }


def _group_summary(runs: list[dict], protocol: dict) -> dict:
    results = [run.get("result") or {} for run in runs]
    valid = [run for run in runs if not _infra(run)]
    rewards = [
        result.get("reward") if result.get("reward_valid") is True else None
        for result in results
    ]
    model = runs[0].get("model") or {}
    digest_src = {"protocol": protocol, "model": model}
    complete = bool(protocol.get("environment_version") and protocol.get("upstream_commit"))
    return {
        "protocol_id": sha256(json.dumps(digest_src, sort_keys=True).encode()).hexdigest()[:16],
        "protocol": protocol, "protocol_complete": complete,
        "model": model,
        "total_started": len(runs),
        "run_ids": [run.get("run_id") for run in runs],
        "task_ids": sorted({str(run.get("task_id")) for run in runs}),
        "status_counts": dict(Counter(run.get("status", "unknown") for run in runs)),
        "strict_success": _rate([_outcome(run, "strict_success") for run in runs]),
        "purchase_success": _rate([_outcome(run, "purchase_success") for run in runs]),
        "valid_execution_success": _rate([
            _outcome(run, "strict_success") for run in valid
        ]),
        "infrastructure_errors": sum(_infra(run) for run in runs),
        "infrastructure_error_rate": sum(_infra(run) for run in runs) / len(runs),
        "invalid_or_unknown_rewards": sum(result.get("reward_valid") is not True for result in results),
        "inconsistent_success_labels": sum(
            result.get("reward_valid") is not True
            and (result.get("strict_success") is True or result.get("purchase_success") is True)
            for result in results
        ),
        "reward": _measure(rewards),
        "steps": _measure([run.get("steps") for run in runs]),
        "elapsed_seconds": _measure([run.get("elapsed_seconds") for run in runs]),
        "model_calls": _measure([run.get("model_calls") for run in runs]),
        "tokens": {
            "input_tokens": _token_measure(runs, "input_tokens"),
            "output_tokens": _token_measure(runs, "output_tokens"),
        },
        "cost": None,
        "constraint_satisfaction": {
            "status": "not_available",
            "reason": "User-constraint checks are unavailable.",
        },
        "warnings": ([] if complete else [
            "Environment version or upstream commit is missing."
        ]) + ([
            "Repeated runs count as separate attempts."
        ] if len({str(run.get("task_id")) for run in runs}) < len(runs) else []),
    }


def summarize(runs: list[dict]) -> dict:
    """Group finished API attempts; list other sources and unfinished runs separately."""
    excluded: Counter = Counter()
    grouped: dict[str, list[dict]] = defaultdict(list)
    reasons = []
    for run in runs:
        source = run.get("source")
        provider = (run.get("model") or {}).get("provider")
        if source == "imported" or run.get("imported"):
            reason = "imported"
        elif run.get("mode") == "fixture" or source == "synthetic_fixture":
            reason = "fixture"
        elif provider == "scripted":
            reason = "scripted"
        elif run.get("mode") != "shopsimulator" or source != "live" or provider != "api":
            reason = "unverified_provenance"
        elif run.get("status") not in FINAL_STATUSES:
            reason = "not_started" if run.get("status") == "pending" else "unfinished"
        else:
            reason = None
        if reason:
            excluded[reason] += 1
            reasons.append({"run_id": run.get("run_id"), "reason": reason})
            continue
        key = json.dumps({"protocol": _protocol(run), "model": run.get("model") or {}}, sort_keys=True)
        grouped[key].append(run)
    groups = [_group_summary(items, _protocol(items[0])) for items in grouped.values()]
    count = sum(group["total_started"] for group in groups)
    return {
        "schema_version": "shopping-lab.evaluation.v1",
        "status": "available" if count else "not_available",
        "total_runs": len(runs), "formal_run_count": count,
        "excluded": dict(excluded), "excluded_runs": reasons, "groups": groups,
        "diagnostics": {
            "fixture_runs": sum(run.get("mode") == "fixture" for run in runs),
            "imported_runs": sum(run.get("source") == "imported" or bool(run.get("imported")) for run in runs),
            "scripted_runs": sum((run.get("model") or {}).get("provider") == "scripted" for run in runs),
            "status_counts": dict(Counter(run.get("status", "unknown") for run in runs)),
        },
        "notes": [
            "Success uses recorded terminal outcomes.",
            "Unknown outcomes remain in the end-to-end denominator.",
            "Results are grouped by model, strategy, budget and environment protocol.",
            "Metrics describe the recorded attempts.",
            "Stage labels identify the configured model category.",
        ],
    }


def _flatten(value: dict, prefix: str = "") -> dict:
    result = {}
    for key, item in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            result.update(_flatten(item, path))
        else:
            result[path] = item
    return result


def compare_runs(left: dict, right: dict) -> dict:
    """Compare outcomes and flag protocol differences."""
    relevant = ("task_id", "query", "mode", "model", "runtime", "strategy", "environment_version",
                "upstream_commit", "data_version", "tool_version", "prompt_version", "observation_version",
                "environment_observation_version", "observation_adapter_version", "protocol_version",
                "reward_version", "prompt_hash", "tools_hash", "context_policy")
    a = _flatten({key: left.get(key) for key in relevant})
    b = _flatten({key: right.get(key) for key in relevant})
    diffs = [{"field": key, "left": a.get(key), "right": b.get(key)}
                   for key in sorted(a.keys() | b.keys()) if a.get(key) != b.get(key)]
    warnings = []
    comparable = True
    fixed_keys = set(relevant) - {"model", "strategy", "runtime", "context_policy"}
    if not left.get("task_id") or not right.get("task_id"):
        comparable = False
        warnings.append("Task identity is missing.")
    for difference in diffs:
        if (difference["field"] in fixed_keys or difference["field"] in {"runtime", "context_policy"}
                or difference["field"].startswith(("runtime.", "context_policy."))):
            comparable = False
            warnings.append(f"Protocol differs: {difference['field']}.")
    if left.get("mode") == "fixture" or right.get("mode") == "fixture":
        warnings.append("Synthetic fixture comparison.")
    for run in (left, right):
        if run.get("source") == "imported" or run.get("imported"):
            warnings.append("Imported history is excluded from API summary metrics.")
        if not run.get("environment_version") or not run.get("upstream_commit"):
            comparable = False
            warnings.append("Environment version or upstream commit is missing.")
    model_diff = any(item["field"].startswith("model.") for item in diffs)
    strat_diff = any(item["field"] == "strategy" for item in diffs)
    if model_diff and strat_diff:
        warnings.append("Model and strategy both changed; their effects cannot be separated.")
    efficiency = {}
    for field in ("steps", "elapsed_seconds", "model_calls"):
        x, y = left.get(field), right.get(field)
        efficiency[field] = {"left": x, "right": y, "right_minus_left": y - x if _finite(x) and _finite(y) else None}
    warnings.append("This comparison covers one task pair; step numbers may refer to different states.")
    return {
        "schema_version": "shopping-lab.comparison.v1",
        "comparable": comparable,
        "left_run_id": left.get("run_id"), "right_run_id": right.get("run_id"),
        "differences": diffs, "warnings": list(dict.fromkeys(warnings)),
        "outcomes": {side: {"status": run.get("status"), "result": run.get("result") or {},
                             "source": run.get("source"), "error": run.get("error")}
                     for side, run in (("left", left), ("right", right))},
        "efficiency": efficiency,
    }
