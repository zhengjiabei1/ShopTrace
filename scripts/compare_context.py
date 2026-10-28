#!/usr/bin/env python3
"""Preregistered context comparison with a frozen heldout decision."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from lab.storage import sanitize
from webagent.benchmark import BenchmarkError, run_batch, source_snapshot, summarize
from webagent.cases import split_manifest

CONTROL = "full_history"
CANDIDATE = "latest_observation"
LIMITS = {"max_steps": 35, "max_seconds": 180, "max_output_tokens": 512}
RULE = ("Choose latest_observation only when both development arms have six complete known results, "
        "candidate successes are at least control successes, all input token usages are known, and "
        "candidate total input tokens are strictly smaller; otherwise retain full_history.")


def now():
    return datetime.now(timezone.utc).isoformat()


def write(path, value, *, exclusive=False):
    raw = json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(raw)
    else:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(raw, encoding="utf-8")
        temporary.replace(path)


def _run(entry):
    value = entry.get("run") if isinstance(entry, dict) else None
    return value if isinstance(value, dict) else {}


def _case_id(entry):
    return (entry.get("task_id") if isinstance(entry, dict) else None) or _run(entry).get("task_id")


def _score(entry):
    run = _run(entry)
    result, model = run.get("result"), run.get("model")
    if not isinstance(result, dict) or not isinstance(model, dict):
        return None
    score = result.get("success")
    if (run.get("status") in {None, "pending", "running"} or run.get("source") != "browser_model"
            or model.get("provider") != "api" or type(score) is not bool):
        return None
    return score


def _token_total(entries):
    usages = [_run(entry).get("usage") for entry in entries]
    if any(not isinstance(usage, dict) for usage in usages):
        return None
    values = [usage.get("input_tokens") for usage in usages]
    if any(type(value) is not int or value < 0 for value in values):
        return None
    return sum(values)



def _comparison_provenance(control, candidate):
    """Check that development arms differ only in context mode."""
    common = None
    catalogs = {}
    for mode, entries in ((CONTROL, control), (CANDIDATE, candidate)):
        catalogs[mode] = {}
        for entry in entries:
            run = _run(entry)
            model, runtime = run.get("model"), run.get("runtime")
            policy, case = run.get("context_policy"), run.get("benchmark_case")
            if not all(isinstance(value, dict) for value in (model, runtime, policy, case)):
                return "missing_or_malformed_protocol_metadata"
            if not isinstance(model.get("name"), str) or not model["name"].strip():
                return "missing_model_identity"
            if model.get("provider") != "api" or run.get("source") != "browser_model":
                return "non_live_model_source"
            if any(not isinstance(run.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", run[key])
                   for key in ("prompt_hash", "tools_hash")):
                return "missing_or_malformed_prompt_tools_fingerprint"
            if not isinstance(run.get("protocol_version"), str) or not run["protocol_version"].strip():
                return "missing_protocol_version"
            if runtime.get("context_mode", CONTROL) != mode or policy.get("mode") != mode:
                return "incorrect_arm_context_mode"
            if entry.get("context_mode", mode) != mode:
                return "incorrect_receipt_context_mode"
            if any(type(runtime.get(key)) is not int or runtime[key] != expected
                   for key, expected in LIMITS.items()):
                return "execution_limits_differ_from_plan"
            if (policy.get("unit") != "serialized_json_characters"
                    or type(policy.get("max")) is not int or policy["max"] <= 0
                    or policy.get("overflow") != "stop_without_truncation"):
                return "missing_or_malformed_context_budget"
            task_id = _case_id(entry)
            if not isinstance(task_id, str) or run.get("task_id") != task_id or case.get("id") != task_id:
                return "mismatched_case_identity"
            if case.get("split") != "development":
                return "selection_requires_development_split"
            if any(not isinstance(case.get(key), str) or not re.fullmatch(r"[0-9a-f]{64}", case[key])
                   for key in ("dataset_fingerprint", "catalog_fingerprint")):
                return "missing_or_malformed_dataset_catalog_fingerprint"
            if run.get("benchmark_scope") != "local_synthetic_website":
                return "incorrect_benchmark_scope"
            signature = {"model": model, "prompt_hash": run["prompt_hash"], "tools_hash": run["tools_hash"],
                         "protocol_version": run["protocol_version"],
                         "runtime": {key: value for key, value in runtime.items() if key != "context_mode"},
                         "context_policy": {key: value for key, value in policy.items() if key != "mode"},
                         "dataset_fingerprint": case["dataset_fingerprint"]}
            try:
                signature = json.dumps(signature, sort_keys=True, allow_nan=False)
            except (ValueError, TypeError):
                return "malformed_protocol_metadata"
            if common is not None and signature != common:
                return "incompatible_model_protocol_budget_or_dataset"
            common = signature
            catalogs[mode][task_id] = case["catalog_fingerprint"]
    if catalogs[CONTROL] != catalogs[CANDIDATE]:
        return "paired_catalog_fingerprints_differ"
    return None


def select_context(control, candidate, case_ids):
    """Select from development results only."""
    valid_ids = (isinstance(case_ids, (list, tuple)) and len(case_ids) == 6
                 and all(isinstance(case_id, str) and case_id for case_id in case_ids)
                 and len(set(case_ids)) == 6)
    expected = set(case_ids) if valid_ids else set()

    def complete(entries):
        ids = [_case_id(entry) for entry in entries]
        return (valid_ids and all(isinstance(case_id, str) for case_id in ids)
                and len(ids) == len(case_ids) == 6 and len(ids) == len(set(ids)) and set(ids) == expected
                and all(_score(entry) is not None for entry in entries))

    ctrl_done, cand_done = complete(control), complete(candidate)
    ctrl_ok = sum(_score(entry) is True for entry in control)
    cand_ok = sum(_score(entry) is True for entry in candidate)
    ctrl_tok, cand_tok = _token_total(control), _token_total(candidate)
    known_tok = ctrl_tok is not None and cand_tok is not None
    issue = _comparison_provenance(control, candidate) if valid_ids else "invalid_expected_cases"
    compatible = issue is None
    pick_cand = (ctrl_done and cand_done and compatible and known_tok
                 and cand_ok >= ctrl_ok and cand_tok < ctrl_tok)
    if not compatible:
        reason = "incompatible_provenance: " + issue
    elif not ctrl_done or not cand_done:
        reason = "incomplete_or_unknown_development_results"
    elif not known_tok:
        reason = "input_usage_unknown"
    elif cand_ok < ctrl_ok:
        reason = "candidate_successes_below_control"
    elif cand_tok >= ctrl_tok:
        reason = "input_tokens_not_reduced"
    else:
        reason = "candidate_meets_development_rule"
    return {"selection_rule": RULE, "chosen_context": CANDIDATE if pick_cand else CONTROL,
            "reason": reason, "provenance_compatible": compatible,
            "complete_known_control": ctrl_done, "complete_known_candidate": cand_done,
            "control_successes": ctrl_ok, "candidate_successes": cand_ok,
            "control_input_tokens": ctrl_tok, "candidate_input_tokens": cand_tok,
            "all_input_usages_known": known_tok,
            "decision_basis": "development split only; heldout results were not accessed",
            "statistical_claim": "descriptive selection on six authored cases; no significance or causal improvement claim"}


def paired_summary(control, candidate, case_ids):
    """Count paired wins, losses, ties, and unknowns."""
    left = {_case_id(entry): entry for entry in control}
    right = {_case_id(entry): entry for entry in candidate}
    counts = {"candidate_wins": 0, "candidate_losses": 0, "ties": 0, "unknown_pairs": 0}
    pairs = []
    for case_id in case_ids:
        a = _score(left.get(case_id, {}))
        b = _score(right.get(case_id, {}))
        if a is None or b is None:
            outcome = "unknown"
            counts["unknown_pairs"] += 1
        elif a == b:
            outcome = "tie"
            counts["ties"] += 1
        elif b:
            outcome = "candidate_win"
            counts["candidate_wins"] += 1
        else:
            outcome = "candidate_loss"
            counts["candidate_losses"] += 1
        pairs.append({"case_id": case_id, "control_success": a, "candidate_success": b, "outcome": outcome})
    return {**counts, "pair_count": len(case_ids), "known_pairs": len(case_ids) - counts["unknown_pairs"], "pairs": pairs}


def schedule(case_ids, *, candidate=CANDIDATE):
    """Alternate arm order by case index."""
    result = []
    for index, case_id in enumerate(case_ids):
        arms = [CONTROL] if candidate == CONTROL else ([CONTROL, candidate] if index % 2 == 0 else [candidate, CONTROL])
        result.extend({"task_id": case_id, "context_mode": arm, "case_index": index} for arm in arms)
    return result


def frozen_source():
    snapshot = source_snapshot()
    snapshot["files"]["scripts/compare_context.py"] = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
    return snapshot


def metrics(entries):
    values = summarize(entries)
    # Use stored run results for all metrics.
    return values


def run_experiment(base_url, output, *, preregistration=None):
    output = Path(output).resolve()
    if output.exists():
        raise BenchmarkError("Experiment output already exists; no task will be resubmitted.")
    manifest = split_manifest()
    case_ids = {split: [item["case_id"] for item in manifest["splits"][split]] for split in ("development", "heldout")}
    if (any(len(ids) != 6 or len(ids) != len(set(ids)) for ids in case_ids.values())
            or set(case_ids["development"]) & set(case_ids["heldout"])):
        raise BenchmarkError("Experiment requires disjoint development and heldout splits of six cases each.")
    reg_path = Path(preregistration) if preregistration is not None else output.parent / "experiment-plan.json"
    registered = None
    reg_sha = None
    if reg_path.is_file():
        raw = reg_path.read_bytes()
        registered = json.loads(raw)
        reg_sha = hashlib.sha256(raw).hexdigest()
        if registered.get("per_attempt_limits") != LIMITS:
            raise BenchmarkError("Preregistered execution limits differ from the fixed experiment limits.")
        mechanism = registered.get("mechanism") or {}
        if mechanism.get("control") != CONTROL or mechanism.get("candidate") != CANDIDATE:
            raise BenchmarkError("Preregistered context arms differ from this implementation.")
    src_before = frozen_source()
    plan = {"schema_version": "shoptrace.context-experiment-plan.v1", "created_at": now(),
            "limits": LIMITS, "control": CONTROL, "candidate": CANDIDATE, "selection_rule": RULE,
            "case_ids": case_ids, "development_order": schedule(case_ids["development"]),
            "heldout_order_if_candidate_selected": schedule(case_ids["heldout"]),
            "heldout_order_if_control_selected": schedule(case_ids["heldout"], candidate=CONTROL),
            "maximum_task_attempts": 24, "minimum_task_attempts": 18, "automatic_task_retries": 0,
            "preregistration_sha256": reg_sha, "preregistration": registered,
            "source_before": src_before}
    output.mkdir(parents=True, exist_ok=False)
    write(output / "plan.json", plan, exclusive=True)
    write(output / "dataset-manifest.json", manifest, exclusive=True)
    state = {"schema_version": "shoptrace.context-experiment.v1", "created_at": now(), "status": "development_running",
             "attempts": [], "phase_statistics": {}, "source_before": src_before,
             "dataset_manifest": manifest, "registered_plan_sha256": hashlib.sha256((output / "plan.json").read_bytes()).hexdigest(),
             "automatic_task_retries": 0, "maximum_task_attempts": 24,
             "limitations": ["Six authored synthetic cases per split; effects are descriptive only.",
                              "No post-heldout prompt changes or selection changes are allowed.",
                              "No automatic retry replaces any failed or unknown attempt."]}

    def save():
        phase_vals = {}
        for phase in ("development", "heldout"):
            arms = {}
            for arm in (CONTROL, CANDIDATE):
                entries = [a for a in state["attempts"] if a["phase"] == phase and a["context_mode"] == arm]
                if entries:
                    arms[arm] = metrics(entries)
            if arms:
                phase_vals[phase] = arms
        state["phase_statistics"] = phase_vals
        write(output / "experiment.json", state)

    def assert_frozen():
        current = frozen_source()
        if current["files"] != src_before["files"]:
            raise BenchmarkError("Execution source changed during the experiment; no further tasks will be submitted.")

    def assert_server_provenance(receipt):
        run = _run(receipt)
        metadata = run.get("benchmark_case") or {}
        exp_case = next(item for item in manifest["splits"][receipt["phase"]] if item["case_id"] == receipt["task_id"])
        if (metadata.get("id") != receipt["task_id"] or metadata.get("split") != receipt["phase"]
                or metadata.get("dataset_fingerprint") != manifest["dataset_sha256"]
                or metadata.get("catalog_fingerprint") != exp_case["catalog_sha256"]):
            raise BenchmarkError("Server case/dataset provenance differs from the preregistered split manifest.")
        signature = {"model": run.get("model"), "prompt_hash": run.get("prompt_hash"),
                     "tools_hash": run.get("tools_hash"), "protocol_version": run.get("protocol_version")}
        if not all(signature.get(name) for name in ("prompt_hash", "tools_hash", "protocol_version")):
            raise BenchmarkError("Server protocol fingerprints are missing; experiment cannot isolate context representation.")
        if state.get("server_protocol") is None:
            state["server_protocol"] = signature
        elif signature != state["server_protocol"]:
            raise BenchmarkError("Server model/prompt/tools changed between experiment attempts; no further tasks will be submitted.")

    def execute(phase, order):
        for spec in order:
            assert_frozen()
            case_id, arm = spec["task_id"], spec["context_mode"]
            relative = f"{phase}/{case_id}/{arm}"
            receipt = {"phase": phase, "task_id": case_id, "context_mode": arm, "case_index": spec["case_index"],
                       "folder": relative, "submission_state": "not_started"}
            state["attempts"].append(receipt)
            save()
            print(json.dumps({"phase": phase, "case_id": case_id, "context_mode": arm, "status": "starting"}), flush=True)
            try:
                batch = run_batch(base_url, [case_id], 1, output / relative, context_mode=arm, **LIMITS)
            except (BenchmarkError, KeyboardInterrupt):
                saved = output / relative / "batch.json"
                if saved.is_file():
                    batch = json.loads(saved.read_text())
                    if batch.get("attempts"):
                        receipt.update(batch["attempts"][0])
                        receipt["batch_attempt_folder"] = receipt["folder"]
                        receipt["folder"] = relative + "/" + receipt["batch_attempt_folder"]
                        receipt["batch_folder"] = relative
                        receipt["phase"], receipt["context_mode"] = phase, arm
                save()
                raise
            if len(batch.get("attempts", [])) != 1:
                raise BenchmarkError("Sub-batch attempt count differs from the frozen experiment plan.")
            receipt.update(batch["attempts"][0])
            receipt["batch_attempt_folder"] = receipt["folder"]
            receipt["folder"] = relative + "/" + receipt["batch_attempt_folder"]
            receipt.update(batch_folder=relative, phase=phase, context_mode=arm,
                           subbatch_statistics=batch.get("statistics"))
            save()
            assert_server_provenance(receipt)
            assert_frozen()
            print(json.dumps({"phase": phase, "case_id": case_id, "context_mode": arm,
                              "success": (_run(receipt).get("result") or {}).get("success"),
                              "model_calls": _run(receipt).get("model_calls")}), flush=True)

    save()
    try:
        execute("development", plan["development_order"])
        control = [a for a in state["attempts"] if a["phase"] == "development" and a["context_mode"] == CONTROL]
        candidate = [a for a in state["attempts"] if a["phase"] == "development" and a["context_mode"] == CANDIDATE]
        decision = {**select_context(control, candidate, case_ids["development"]), "decided_at": now(),
                    "development_snapshot_sha256": hashlib.sha256(json.dumps(state["attempts"], ensure_ascii=False, sort_keys=True).encode()).hexdigest(),
                    "heldout_attempts_at_decision": 0}
        # Save the decision before heldout execution.
        write(output / "decision.json", decision, exclusive=True)
        state["decision"] = decision
        state["development_paired"] = paired_summary(control, candidate, case_ids["development"])
        state["status"] = "heldout_running"
        save()
        chosen = json.loads((output / "decision.json").read_text())["chosen_context"]
        test_order = schedule(case_ids["heldout"], candidate=chosen)
        execute("heldout", test_order)
        full = [a for a in state["attempts"] if a["phase"] == "heldout" and a["context_mode"] == CONTROL]
        selected = [a for a in state["attempts"] if a["phase"] == "heldout" and a["context_mode"] == chosen]
        state["heldout_paired"] = (paired_summary(full, selected, case_ids["heldout"]) if chosen != CONTROL else {
            "comparison_executed": False, "reason": "Chosen policy is full_history; duplicate control runs were skipped.",
            "single_policy_attempts": len(full), "fabricated_pairs": 0})
        state["status"] = "completed"
        state["completed_at"] = now()
    except (BenchmarkError, KeyboardInterrupt) as exc:
        state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "stopped"
        state["error"] = "Interrupted; original attempts retained and no task was retried." if isinstance(exc, KeyboardInterrupt) else str(exc)
        state["source_after"] = frozen_source()
        state["source_files_unchanged"] = state["source_after"]["files"] == src_before["files"]
        save()
        raise
    state["source_after"] = frozen_source()
    state["source_files_unchanged"] = state["source_after"]["files"] == src_before["files"]
    state["actual_task_attempts"] = len(state["attempts"])
    assert state["actual_task_attempts"] <= 24
    save()
    return state


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:8781")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--plan", type=Path)
    args = parser.parse_args(argv)
    try:
        state = run_experiment(args.base_url, args.output, preregistration=args.plan)
    except KeyboardInterrupt:
        return 130
    except (BenchmarkError, OSError, ValueError) as exc:
        print(json.dumps({"status": "error", "message": str(exc) if isinstance(exc, BenchmarkError) else type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "status": state["status"], "decision": state["decision"],
                      "actual_task_attempts": state["actual_task_attempts"], "source_files_unchanged": state["source_files_unchanged"]}, indent=2))
    return 0 if state["source_files_unchanged"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
