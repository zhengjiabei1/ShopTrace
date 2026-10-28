"""Import upstream observations and tool actions; omit private fields and free text.

The source schema is pinned by UPSTREAM_COMMIT.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
from pathlib import Path
from typing import Any

from .storage import SCHEMA_VERSION, Store, sanitize, utcnow

UPSTREAM_COMMIT = "9140f0d699b2feef7d11b8cbbcadbf612877096b"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_EVENTS = 10000
FINISHED = {"completed", "failed", "cancelled", "budget_exceeded"}
ANNOTATION_CATEGORIES = {"retrieval", "evidence", "memory", "specification", "tool", "environment", "scoring", "other"}
PRIVATE_KEYS = {"goal", "goal_options", "instruction_simple", "gold", "target", "target_asin", "_trace_target", "raw_observation", "audit_only_raw_observation", "reasoning_content", "reasoning", "thinking"}


def _public(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _public(v) for k, v in value.items() if str(k).lower() not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return sanitize(value)


def _number(value):
    return value if type(value) in {int, float} and math.isfinite(value) else None


def _text(value):
    return value if isinstance(value, str) else ""


def _tool_parts(call, name=None, parameters=None):
    function = call.get("function") if isinstance(call, dict) else None
    function = function if isinstance(function, dict) else {}
    name = name if isinstance(name, str) else function.get("name", "")
    raw = parameters if isinstance(parameters, dict) else function.get("arguments", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return _text(name), {}, "unparseable_arguments"
    if not isinstance(raw, dict):
        return _text(name), {}, "arguments_not_object"
    return _text(name), _public(raw), None


def _result(terminal):
    detail = terminal.get("reward_detail")
    detail = detail if isinstance(detail, dict) else {}
    valid = detail.get("reward_valid") is True
    kind = detail.get("reward_type")
    return {"reward": _number(terminal.get("reward")), "reward_valid": valid,
            "reward_type": kind if isinstance(kind, str) else None,
            "strict_success": (kind == "gold_purchase") if valid and isinstance(kind, str) else None,
            "purchase_success": detail.get("purchase_success") if valid and type(detail.get("purchase_success")) is bool else None,
            "termination_reason": _text(detail.get("termination_reason") or terminal.get("termination_reason")) or None}


def normalize_upstream(document: dict) -> dict:
    """Convert raw or normalized traces to lab v1; leave missing requests/budgets unset."""
    if not isinstance(document, dict):
        raise ValueError("Trajectory must be a JSON object")
    normalized = document.get("schema_version") == "shopping-normalized-trajectory-v1"
    raw = (isinstance(document.get("initial_result"), dict)
           and isinstance(document.get("steps"), list)
           and isinstance(document.get("messages"), list))
    if not normalized and not raw:
        raise ValueError("Unsupported upstream trajectory; expected collector raw or normalized-v1")
    task_id = document.get("task_id")
    if isinstance(task_id, bool) or not isinstance(task_id, (int, str)) or not str(task_id).isdecimal():
        raise ValueError("Upstream task_id must be a nonnegative integer")
    initial = document.get("initial_result") if raw else {}
    query = _text(document.get("actor_query")) if normalized else _text(initial.get("instruction"))
    if not query.strip():
        raise ValueError("Trajectory is missing an actor-visible task")
    src_steps = document.get("events") if normalized else document["steps"]
    if not isinstance(src_steps, list) or len(src_steps) > MAX_EVENTS // 2:
        raise ValueError("Invalid trajectory step count")
    terminal = document.get("terminal") if normalized else document.get("terminal_result")
    terminal = terminal if isinstance(terminal, dict) else {}
    errors = document.get("errors") if normalized else {}
    errors = errors if isinstance(errors, dict) else {}
    has_error = bool(document.get("error") or errors.get("trajectory_error") or document.get("release_error") or errors.get("release_error"))
    done = terminal.get("done") is True or terminal.get("over") is True
    result = _result(terminal)
    env_meta = document.get("environment") if normalized else initial
    env_meta = env_meta if isinstance(env_meta, dict) else {}
    warnings = ["Imported records are shown separately and excluded from evaluation and SFT.",
                "Original budgets and full model requests are missing.",
                "Private fields and assistant free text were omitted."]
    events = []

    def add(kind, payload):
        sequence = len(events) + 1
        events.append({"event_id": f"e{sequence:06d}", "sequence": sequence,
                       "type": kind, "payload": _public(payload), "created_at": document.get("created_at")})

    add("observation", {"step": 0, "query": query, "observation": "Initial page missing; showing actor observations from the source."})
    executed = 0
    # Keep guard events at their original step.
    guards: dict[int, list] = {}
    if raw:
        blocked = document.get("blocked_tool_calls", [])
        if not isinstance(blocked, list):
            raise ValueError("blocked_tool_calls must be a list")
        for item in blocked:
            if not isinstance(item, dict) or type(item.get("step_index")) is not int or not 0 <= item["step_index"] <= len(src_steps):
                raise ValueError("Guard record has no valid step position")
            guards.setdefault(item["step_index"], []).append(item)

    def guard(item, step):
        name, arguments, parse_err = _tool_parts(item.get("tool_call"), item.get("tool_name"), item.get("parameters"))
        add("error", {"step": step, "kind": "guard_rejection", "tool_name": name,
                      "arguments": arguments, "message": _text(item.get("guard_reason") or item.get("reason")),
                      "parse_error": parse_err, "source_event_id": item.get("event_id")})

    for index in range(len(src_steps) + 1):
        for item in guards.get(index, []):
            guard(item, executed)
        if index == len(src_steps):
            continue
        item = src_steps[index]
        if not isinstance(item, dict):
            raise ValueError("Each step must be an object")
        if normalized and item.get("event_type") == "guard_rejection":
            guard(item, executed)
            continue
        if normalized and item.get("event_type") != "tool_step":
            raise ValueError("Normalized trajectory contains an unsupported event_type")
        name, arguments, parse_err = _tool_parts(item.get("tool_call"), item.get("tool_name"), item.get("parameters"))
        add("action", {"step": executed, "tool_name": name, "arguments": arguments,
                       "env_action": _text(item.get("env_action")), "parse_error": parse_err,
                       "source_event_id": item.get("event_id"), "source_step_index": index})
        executed += 1
        obs_key = "actor_visible_observation" if normalized else "observation"
        obs = item.get(obs_key)
        if not isinstance(obs, str):
            raise ValueError("Step is missing actor-visible observation text")
        # Terminal observations may print private reward or target details.
        last_step = item.get("done") is True or item.get("over") is True
        add("environment_result", {"step": executed, "observation": "Terminal page omitted; see the result panel for scores." if last_step else obs,
                                   "done": last_step, "result": result if last_step else None})
    status = "completed" if done and not has_error else "failed"
    if isinstance(document.get("status"), str) and document["status"] in {"cancelled", "budget_exceeded"}:
        status = document["status"]
    run = {"task_id": str(task_id), "mode": "shopsimulator", "source": "imported",
           "model": {"provider": "api", "name": "Imported model (source unverified)", "stage": "other"},
           "runtime": {}, "strategy": "default", "status": status, "query": query,
           "steps": executed, "model_calls": None, "elapsed_seconds": None,
           "usage": {"input_tokens": None, "output_tokens": None}, "result": result,
           "environment_version": env_meta.get("environment_version"),
           "reward_version": env_meta.get("reward_version"), "upstream_commit": None,
           "termination_reason": result.get("termination_reason") or "imported_terminal" if done else "imported_incomplete",
           "error": {"kind": "imported_error", "message": "Source record contains errors; private details were omitted."} if has_error else None,
           "provenance": {"verified": False, "original_run_id": document.get("trajectory_id"),
                          "format_contract_commit": UPSTREAM_COMMIT,
                          "source_format": "shopping-normalized-trajectory-v1" if normalized else "collector_raw",
                          "runtime_recorded": False, "warnings": warnings}}
    add("status", {"status": status, "imported": True})
    return {"schema_version": SCHEMA_VERSION, "run": _public(run), "events": events, "annotations": []}


def validate_envelope(document: dict) -> dict:
    """Validate a native lab envelope before any persistence."""
    if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("Unsupported Lab trajectory schema_version")
    try:
        json.dumps(document, allow_nan=False)
    except (ValueError, TypeError) as exc:
        raise ValueError("Trajectory contains non-JSON values or non-finite numbers") from exc
    run = document.get("run")
    events = document.get("events")
    if not isinstance(run, dict) or not isinstance(events, list) or len(events) > MAX_EVENTS:
        raise ValueError("Lab trajectory requires a run object and events list")
    if not isinstance(run.get("task_id"), str) or not 1 <= len(run["task_id"]) <= 100 or not isinstance(run.get("mode"), str) or run["mode"] not in {"fixture", "shopsimulator"}:
        raise ValueError("Invalid Lab task ID or mode")
    model = run.get("model")
    if not isinstance(model, dict) or not isinstance(model.get("provider"), str) or model["provider"] not in {"api", "scripted"} or not isinstance(model.get("name"), str) or not 1 <= len(model["name"]) <= 200:
        raise ValueError("Invalid Lab model fields")
    if not isinstance(model.get("stage", "other"), str) or model.get("stage", "other") not in {"base", "sft", "rl", "other"}:
        raise ValueError("Invalid Lab model stage")
    if not isinstance(run.get("runtime"), dict) or run.get("strategy", "default") != "default":
        raise ValueError("Invalid Lab run configuration")
    for field in ("max_steps", "max_seconds", "max_calls", "max_output_tokens"):
        value = run["runtime"].get(field)
        if field in run["runtime"] and (_number(value) is None or value <= 0):
            raise ValueError("Budgets must be finite positive numbers; omit unknown budgets")
    for field in ("steps", "model_calls"):
        if run.get(field) is not None and (type(run[field]) is not int or run[field] < 0):
            raise ValueError("Run counts must be nonnegative integers or null")
    if run.get("elapsed_seconds") is not None and (_number(run["elapsed_seconds"]) is None or run["elapsed_seconds"] < 0):
        raise ValueError("Elapsed time must be finite and nonnegative, or null")
    usage = run.get("usage")
    if usage is not None and (not isinstance(usage, dict) or any(value is not None and (type(value) is not int or value < 0) for value in usage.values())):
        raise ValueError("usage must be an object or null; token counts must be nonnegative integers or null")
    for field in ("error", "release_error"):
        error = run.get(field)
        if error is not None and (not isinstance(error, dict) or any(key in error and (not isinstance(error[key], str) or len(error[key]) > (100 if key == "kind" else 2000)) for key in ("kind", "message"))):
            raise ValueError("error must be an object or null; kind/message must be bounded strings")
    if run.get("status") is not None and not isinstance(run["status"], str):
        raise ValueError("status must be a string")
    result = run.get("result", {})
    if not isinstance(result, dict):
        raise ValueError("Invalid Lab result fields")
    if any(result.get(key) is not None and type(result[key]) is not bool for key in ("strict_success", "purchase_success", "fixture_success", "fixture_purchase")):
        raise ValueError("Success flags must be boolean or null")
    if result.get("reward") is not None and _number(result["reward"]) is None:
        raise ValueError("Reward must be a finite number or null")
    if result.get("reward_valid") is not None and type(result["reward_valid"]) is not bool:
        raise ValueError("reward_valid must be boolean or null")
    seen, previous = set(), 0
    for event in events:
        if not isinstance(event, dict) or not isinstance(event.get("payload"), dict) or not isinstance(event.get("type"), str) or not 1 <= len(event["type"]) <= 100:
            raise ValueError("Invalid Lab event structure")
        if type(event.get("sequence")) is not int or event["sequence"] <= previous or not isinstance(event.get("event_id"), str) or not 1 <= len(event["event_id"]) <= 100 or event["event_id"] in seen:
            raise ValueError("Event sequences must increase strictly and event_id must be unique")
        previous = event["sequence"]
        seen.add(event["event_id"])
    notes = document.get("annotations", [])
    if not isinstance(notes, list) or len(notes) > MAX_EVENTS:
        raise ValueError("annotations must be a bounded list")
    for annotation in notes:
        if (not isinstance(annotation, dict) or not isinstance(annotation.get("event_id"), str)
                or annotation["event_id"] not in seen or not isinstance(annotation.get("category"), str)
                or annotation["category"] not in ANNOTATION_CATEGORIES
                or not isinstance(annotation.get("note"), str) or not 1 <= len(annotation["note"]) <= 2000):
            raise ValueError("Annotations require a valid event, a supported category and a 1-2000 character note")
    return deepcopy(document)


def persist_import(store: Store, envelope: dict) -> dict:
    """Store an imported envelope under a new ID."""
    document = validate_envelope(envelope)
    original = document["run"]
    run = store.create_run({key: original.get(key) for key in ("task_id", "mode", "model", "runtime", "strategy")}, source="imported")
    run_id = run["run_id"]
    allowed = {"query", "environment_version", "reward_version", "upstream_commit", "data_version", "tool_version", "prompt_version", "observation_version", "status", "termination_reason", "steps", "model_calls", "elapsed_seconds", "usage", "result", "error", "release_error", "runtime", "seed", "protocol", "protocol_id", "protocol_hash", "reproducibility"}
    updates = {key: deepcopy(value) for key, value in original.items()
               if key in allowed or str(key).endswith(("_hash", "_version", "_commit"))}
    if updates.get("status") not in FINISHED:
        updates.update(status="failed", termination_reason="imported_incomplete")
    provenance = original.get("provenance")
    provenance = provenance if isinstance(provenance, dict) else {}
    updates.update(completed_at=utcnow(), provenance={**_public(provenance), "verified": False,
                   "original_run_id": provenance.get("original_run_id", original.get("run_id")),
                   "original_source": original.get("source")})
    store.update_run(run_id, **_public(updates))
    mapping = {}
    for event in document["events"]:
        saved = store.append_event(run_id, event["type"], {**_public(event["payload"]), "imported_event_id": event["event_id"]})
        mapping[event["event_id"]] = saved["event_id"]
    notes = document.get("annotations")
    if isinstance(notes, list):
        for annotation in notes:
            if not isinstance(annotation, dict) or annotation.get("event_id") not in mapping:
                continue
            if annotation.get("category") not in ANNOTATION_CATEGORIES or not isinstance(annotation.get("note"), str):
                continue
            store.annotate(run_id, mapping[annotation["event_id"]], annotation["category"], annotation["note"][:2000])
    return store.get_run(run_id)


def load_documents(path: str | Path) -> list[dict]:
    path = Path(path)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ValueError("Trajectory file must not exceed 16 MiB")
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".jsonl":
        documents = [json.loads(line) for line in text.splitlines() if line.strip()]
    else:
        document = json.loads(text)
        documents = document if isinstance(document, list) else [document]
    if not documents or not all(isinstance(item, dict) for item in documents):
        raise ValueError("Expected one or more JSON trajectory objects")
    return documents


def prepare_import(document: dict) -> dict:
    if not isinstance(document, dict):
        raise ValueError("Trajectory must be a JSON object")
    if document.get("schema_version") == SCHEMA_VERSION:
        return validate_envelope(document)
    return normalize_upstream(document)
