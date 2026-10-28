"""Frozen procurement experiments, saved attempts, and offline comparisons."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from copy import deepcopy
import csv
import hashlib
import io
import itertools
import json
from pathlib import Path
import sys
import time
import zipfile

import httpx

from .benchmark import (ACTIVE, RUN_ID, SHOT, BenchmarkError, _base_url,
                        _distribution, _now, _request, _write, source_snapshot)

PLAN = "shoptrace.procurement-plan.v1"
STATE = "shoptrace.procurement-experiment.v1"
REPORT = "shoptrace.procurement-report.v1"
BENCH = "shoptrace-procurement-v1"
FAULTS = {"none", "ack_lost", "no_effect", "stockout"}
POLICIES = {"baseline", "verify", "adaptive"}
MODES = {"full_history", "latest_observation", "page_memory"}
BUDGETS = {"max_steps": (1, 100), "max_seconds": (1, 1800), "max_output_tokens": (32, 4096)}
CASE_KEYS = {"task_id", "seed", "fault", "recovery", "context_mode"}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BenchmarkError("Cannot read experiment JSON: " + Path(path).name) from exc


def validate_plan(plan):
    keys = {"schema_version", "name", "split", "benchmark", "model", "budgets", "cases"}
    if not isinstance(plan, dict) or set(plan) not in (keys, keys | {"selection"}):
        raise BenchmarkError("Plan fields do not match the procurement plan schema.")
    if plan["schema_version"] != PLAN or plan["benchmark"] != BENCH:
        raise BenchmarkError("Unknown plan schema or benchmark.")
    if not isinstance(plan["name"], str) or not 1 <= len(plan["name"]) <= 100:
        raise BenchmarkError("Plan name must contain 1–100 characters.")
    if not isinstance(plan["model"], str) or not 1 <= len(plan["model"]) <= 160:
        raise BenchmarkError("Plan must name the served model ID.")
    if plan["split"] not in {"development", "heldout"}:
        raise BenchmarkError("Plan split must be development or heldout.")
    cfg = plan["budgets"]
    if not isinstance(cfg, dict) or set(cfg) != set(BUDGETS):
        raise BenchmarkError("Plan must declare step, time, and output budgets.")
    for key, (lo, hi) in BUDGETS.items():
        if type(cfg[key]) is not int or not lo <= cfg[key] <= hi:
            raise BenchmarkError("Invalid plan budget: " + key)
    cases = plan["cases"]
    if not isinstance(cases, list) or not 1 <= len(cases) <= 200:
        raise BenchmarkError("Plan must contain 1–200 cases.")
    ids = set()
    prefix = "procure-dev-" if plan["split"] == "development" else "procure-test-"
    count = 6 if plan["split"] == "development" else 4
    tasks = {prefix + f"{i:02}" for i in range(1, count + 1)}
    for case in cases:
        if not isinstance(case, dict) or set(case) != CASE_KEYS:
            raise BenchmarkError("Case fields do not match the procurement schema.")
        if case["task_id"] not in tasks:
            raise BenchmarkError("Task does not belong to the declared split.")
        if type(case["seed"]) is not int or not 0 <= case["seed"] <= 2**31 - 1:
            raise BenchmarkError("Seed must be an integer from 0 to 2147483647.")
        if (case["fault"] not in FAULTS or case["recovery"] not in POLICIES
                or case["context_mode"] not in MODES):
            raise BenchmarkError("Unknown fault, recovery policy, or context mode.")
        key = digest(case)
        if key in ids:
            raise BenchmarkError("Duplicate case; use another seed for another planned attempt.")
        ids.add(key)
    if "selection" in plan:
        choice = plan["selection"]
        if (not isinstance(choice, dict) or set(choice) != {"factor", "selected", "report_hash", "source_fingerprint", "decision_hash"}
                or choice["factor"] not in {"recovery", "context_mode"}
                or any(not isinstance(choice[key], str) or len(choice[key]) != 64
                       for key in ("report_hash", "source_fingerprint", "decision_hash"))
                or any(case[choice["factor"]] != choice["selected"] for case in cases)):
            raise BenchmarkError("Invalid selection binding in plan.")
    return deepcopy(plan)


def case_id(case):
    return digest(case)[:20]


def _spec(plan, case):
    return {**case, **plan["budgets"], "provider": "api"}


def _hashes(folder):
    return [{"path": str(path.relative_to(folder)), "bytes": path.stat().st_size,
             "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
            for path in sorted(folder.rglob("*")) if path.is_file() and path.name != "evidence.json"]


def _check_files(folder):
    rows = read_json(folder / "evidence.json")
    if not isinstance(rows, list) or not rows:
        raise BenchmarkError("Attempt evidence manifest is empty.")
    paths = set()
    for row in rows:
        path = row.get("path") if isinstance(row, dict) else None
        if not isinstance(path, str) or Path(path).is_absolute() or ".." in Path(path).parts or path in paths:
            raise BenchmarkError("Invalid or duplicate evidence path.")
        paths.add(path)
        target = folder / path
        if (not target.is_file() or target.stat().st_size != row.get("bytes")
                or hashlib.sha256(target.read_bytes()).hexdigest() != row.get("sha256")):
            raise BenchmarkError("Attempt evidence differs from its manifest: " + path)
    if not {"request.json", "receipt.json", "result.json", "trajectory.json"} <= paths:
        raise BenchmarkError("Attempt evidence manifest is incomplete.")
    actual = {str(path.relative_to(folder)) for path in folder.rglob("*")
              if path.is_file() and path.name != "evidence.json"}
    if actual != paths:
        raise BenchmarkError("Attempt contains files outside its evidence manifest.")


def _check_run(run, run_id, spec, plan, ident):
    if not isinstance(run, dict):
        raise BenchmarkError("Workbench returned an invalid run.")
    if (run.get("run_id") != run_id or run.get("task_id") != spec["task_id"]
            or run.get("source") != "browser_model"
            or run.get("model") != {"provider": "api", "name": plan["model"]}):
        raise BenchmarkError("Run identity, model, or source differs from the plan.")
    cfg = run.get("runtime") or {}
    for key in (*BUDGETS, "context_mode", "recovery"):
        if cfg.get(key) != spec[key]:
            raise BenchmarkError("Run setting differs from the saved request: " + key)
    meta = run.get("benchmark_case") or {}
    if (meta.get("id") != spec["task_id"] or meta.get("split") != plan["split"]
            or meta.get("seed") != spec["seed"] or meta.get("fault") != spec["fault"]
            or meta.get("benchmark") != BENCH):
        raise BenchmarkError("Run benchmark case differs from the plan.")
    for key in ("catalog_fingerprint", "dataset_fingerprint", "benchmark_fingerprint"):
        if not isinstance(meta.get(key), str) or len(meta[key]) != 64:
            raise BenchmarkError("Run is missing its dataset or catalog fingerprint.")
    if run.get("source_fingerprint") != ident["server_source"]:
        raise BenchmarkError("Server source changed after the experiment was frozen.")
    if run.get("status") not in ACTIVE | {"completed", "failed", "cancelled", "budget_exceeded"}:
        raise BenchmarkError("Unknown run status.")


def _protocol(run):
    return {key: run.get(key) for key in ("protocol_version", "prompt_hash", "tools_hash")}


def _check_proto(state, run, spec):
    if not run.get("protocol_version"):
        if run.get("status") == "pending":
            return
        raise BenchmarkError("Run protocol identity is missing.")
    proto = _protocol(run)
    if any(not isinstance(value, str) or not value for value in proto.values()):
        raise BenchmarkError("Run prompt or tool fingerprint is missing.")
    key = spec["context_mode"] + "/" + spec["recovery"]
    known = state["protocols"].get(key)
    if known is not None and known != proto:
        raise BenchmarkError("Model protocol changed within an experiment arm.")
    state["protocols"][key] = proto
    data = (run.get("benchmark_case") or {}).get("dataset_fingerprint")
    if state.get("dataset") not in {None, data}:
        raise BenchmarkError("Dataset changed within the experiment.")
    state["dataset"] = data


def _collect(client, folder, run_id, spec, plan, ident):
    doc = _request(client, "GET", "/api/runs/" + run_id + "/export")
    run = doc.get("run")
    _check_run(run, run_id, spec, plan, ident)
    if run["status"] in ACTIVE:
        raise BenchmarkError("Run is still active; resume collection using the saved run ID.")
    events = doc.get("events")
    if not isinstance(events, list):
        raise BenchmarkError("Run export has no event list.")
    _write(folder / "trajectory.json", doc)
    _write(folder / "result.json", run)
    shots = folder / "screenshots"
    shots.mkdir(exist_ok=True)
    names = set()
    for event in events:
        if event.get("type") != "observation":
            continue
        name = ((event.get("payload") or {}).get("observation") or {}).get("screenshot")
        if not isinstance(name, str) or not SHOT.fullmatch(name):
            raise BenchmarkError("An observation is missing a valid screenshot name.")
        names.add(name)
    for name in sorted(names):
        try:
            resp = client.get(f"/api/runs/{run_id}/screenshots/{name}")
        except httpx.HTTPError as exc:
            raise BenchmarkError("Screenshot collection failed; resume the saved run.") from exc
        if resp.status_code != 200 or not resp.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BenchmarkError("Screenshot is unavailable or not a PNG.")
        (shots / name).write_bytes(resp.content)
    _write(folder / "evidence.json", _hashes(folder))
    return run


def _save(output, state):
    _write(output / "experiment.json", state)


def _identity(client, plan):
    status = _request(client, "GET", "/api/status")
    if status.get("model_name") != plan["model"]:
        raise BenchmarkError("Workbench model ID differs from the frozen plan.")
    if status.get("source_current") is False:
        raise BenchmarkError("Workbench source changed; restart the service before an experiment.")
    server = status.get("source_fingerprint")
    if not isinstance(server, str) or len(server) != 64:
        raise BenchmarkError("Workbench must expose a frozen source fingerprint.")
    choice = plan.get("selection")
    if choice and choice["source_fingerprint"] != server:
        raise BenchmarkError("Selected plan requires the source used for its development decision.")
    source = source_snapshot()
    return {"runner_source": source, "runner_hash": digest(source["files"]),
            "server_source": server, "server_files": status.get("source_files"),
            "model": plan["model"]}


def _load(output, plan=None):
    saved = validate_plan(read_json(output / "plan.json"))
    state = read_json(output / "experiment.json")
    ident = read_json(output / "identity.json")
    if (state.get("schema_version") != STATE or state.get("plan_hash") != digest(saved)
            or (plan is not None and saved != plan) or state.get("identity_hash") != digest(ident)):
        raise BenchmarkError("Frozen plan or identity differs from saved experiment state.")
    rows = state.get("attempts")
    if not isinstance(rows, list) or len(rows) != len(saved["cases"]):
        raise BenchmarkError("Saved experiment does not contain the complete case list.")
    for row, case in zip(rows, saved["cases"]):
        if row.get("id") != case_id(case) or row.get("spec") != _spec(saved, case):
            raise BenchmarkError("Saved case differs from the frozen plan.")
        if row.get("state") not in {"not_started", "submitting", "unknown", "acknowledged", "finished"}:
            raise BenchmarkError("Unknown attempt state.")
    return saved, state, ident


def run_plan(plan, output, *, base_url="http://127.0.0.1:8775", resume=False,
             client=None, sleep=time.sleep, poll=1):
    """Execute each planned case once; resume only acknowledged run IDs."""
    plan = validate_plan(read_json(plan) if not isinstance(plan, dict) else plan)
    if plan["split"] == "heldout" and "selection" not in plan:
        raise BenchmarkError("Heldout execution requires a plan bound by the select command.")
    base_url = _base_url(base_url)
    output = Path(output).resolve()
    if type(poll) not in {int, float} or poll < 0:
        raise BenchmarkError("Polling interval must be nonnegative.")
    if resume:
        plan, state, ident = _load(output, plan)
        if state["base_url"] != base_url:
            raise BenchmarkError("Resume requires the original workbench URL.")
    else:
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise BenchmarkError("Output already exists; use --resume with the unchanged plan.") from exc
        _write(output / "plan.json", plan, exclusive=True)
        ident = None
        state = {"schema_version": STATE, "plan_hash": digest(plan), "created_at": _now(),
                 "base_url": base_url, "status": "planned", "protocols": {}, "dataset": None,
                 "automatic_retries": 0, "attempts": [
                     {"id": case_id(case), "spec": _spec(plan, case), "state": "not_started"}
                     for case in plan["cases"]]}
    own = client is None
    if own:
        client = httpx.Client(base_url=base_url, trust_env=False, timeout=30, follow_redirects=False)
    current = None
    try:
        latest = _identity(client, plan)
        if ident is not None:
            if any(ident.get(key) != latest[key] for key in ("runner_hash", "server_source", "model")):
                raise BenchmarkError("Source or model changed; start a separate experiment directory.")
        else:
            ident = latest
            _write(output / "identity.json", ident, exclusive=True)
            state["identity_hash"] = digest(ident)
            _save(output, state)
        public = _request(client, "GET", "/api/procurement", params={"split": plan["split"]})
        tasks = public.get("tasks", public.get("cases", []))
        ids = {item.get("id") for item in tasks if isinstance(item, dict)}
        if {case["task_id"] for case in plan["cases"]} - ids:
            raise BenchmarkError("The workbench does not provide all planned tasks.")
        if not resume:
            _write(output / "task-metadata.json", public, exclusive=True)
        state["status"] = "running"
        state.pop("error", None)
        _save(output, state)
        for current in state["attempts"]:
            spec = current["spec"]
            folder = output / "attempts" / current["id"]
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "request.json"
            if path.exists():
                if read_json(path) != spec:
                    raise BenchmarkError("Saved request differs from its planned case.")
            else:
                if current["state"] != "not_started":
                    raise BenchmarkError("Submitted attempt is missing its saved request.")
                _write(path, spec, exclusive=True)
            if current["state"] == "finished":
                _check_files(folder)
                run = read_json(folder / "result.json")
                _check_run(run, current.get("run_id"), spec, plan, ident)
                _check_proto(state, run, spec)
                continue
            receipt = folder / "receipt.json"
            if current["state"] in {"submitting", "unknown"}:
                if not receipt.exists():
                    raise BenchmarkError("Task submission is uncertain. Inspect the server; it will not be sent again.")
                run = read_json(receipt)
                run_id = run.get("run_id")
                _check_run(run, run_id, spec, plan, ident)
                if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                    raise BenchmarkError("Saved receipt has no valid run ID.")
                current.update(state="acknowledged", run_id=run_id)
                _save(output, state)
            if current["state"] == "not_started":
                current.update(state="submitting", submitted_at=_now())
                _save(output, state)
                try:
                    run = _request(client, "POST", "/api/runs", json=spec)
                except BenchmarkError:
                    current["state"] = "unknown"
                    raise
                run_id = run.get("run_id")
                if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                    current["state"] = "unknown"
                    raise BenchmarkError("Submission returned no valid run ID; automatic replacement is disabled.")
                _write(receipt, run, exclusive=True)
                current.update(state="acknowledged", run_id=run_id)
                _save(output, state)
                _check_run(run, run_id, spec, plan, ident)
            else:
                run_id = current.get("run_id")
                if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                    raise BenchmarkError("Acknowledged attempt has no valid run ID.")
                run = _request(client, "GET", "/api/runs/" + run_id)
                _check_run(run, run_id, spec, plan, ident)
            _check_proto(state, run, spec)
            deadline = time.monotonic() + spec["max_seconds"] + 60
            while run["status"] in ACTIVE:
                if time.monotonic() >= deadline:
                    raise BenchmarkError("Polling stopped; resume the saved run ID after checking the server.")
                sleep(poll)
                run = _request(client, "GET", "/api/runs/" + run_id)
                _check_run(run, run_id, spec, plan, ident)
                _check_proto(state, run, spec)
                _write(folder / "latest-status.json", run)
            _write(folder / "result.json", run)
            run = _collect(client, folder, run_id, spec, plan, ident)
            _check_proto(state, run, spec)
            current.update(state="finished", completed_at=_now())
            _save(output, state)
        state["status"] = "completed"
        state["completed_at"] = _now()
        _save(output, state)
    except (BenchmarkError, KeyboardInterrupt) as exc:
        state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "stopped"
        state["error"] = "Interrupted; saved attempts can be resumed." if isinstance(exc, KeyboardInterrupt) else str(exc)
        if current and current.get("state") == "submitting":
            current["state"] = "unknown"
        _save(output, state)
        if ident is not None:
            try:
                report(output)
            except BenchmarkError:
                pass
        raise
    finally:
        if own:
            client.close()
    result = report(output)
    archive(output)
    return result


def _row(attempt, folder, plan, ident, state):
    spec = attempt["spec"]
    row = {"id": attempt["id"], "spec": spec, "state": attempt["state"],
           "run_id": attempt.get("run_id"), "status": None, "success": None,
           "reason": None, "evidence_complete": attempt["state"] == "finished"}
    path = folder / "result.json"
    if not path.exists():
        return row
    if row["evidence_complete"]:
        _check_files(folder)
    run = read_json(path)
    _check_run(run, row["run_id"], spec, plan, ident)
    _check_proto(state, run, spec)
    result = run.get("result") or {}
    if type(result.get("success")) not in {bool, type(None)}:
        raise BenchmarkError("Run success must be true, false, or null.")
    meta = run["benchmark_case"]
    metrics = result.get("metrics") or {}
    usage = run.get("usage") or {}
    for key in ("input_tokens", "output_tokens"):
        value = usage.get(key)
        if value is not None and (type(value) is not int or value < 0):
            raise BenchmarkError("Invalid measured token usage.")
    counts = Counter()
    memory = []
    shots = 0
    trace = folder / "trajectory.json"
    if trace.exists():
        doc = read_json(trace)
        if doc.get("run") != run:
            raise BenchmarkError("Trajectory and result records differ.")
        for event in doc.get("events", []):
            kind = event.get("type")
            payload = event.get("payload") or {}
            if kind == "recovery":
                counts[str(payload.get("state", payload.get("decision", payload.get("action", "unspecified"))))] += 1
            if kind == "memory":
                memory.append(payload)
            if kind == "observation":
                shots += 1
    row.update(status=run["status"], success=result.get("success"), reason=result.get("reason"),
               source=run["source"], model=run["model"], protocol=_protocol(run),
               source_fingerprint=run.get("source_fingerprint"), benchmark=meta,
               steps=run.get("steps"), model_calls=run.get("model_calls"),
               elapsed_seconds=run.get("elapsed_seconds"), usage=usage,
               duplicates=metrics.get("duplicates", result.get("duplicates")),
               fault_triggered=metrics.get("fault_triggered", result.get("fault_triggered")),
               recovery_guidance=dict(counts), memory_events=len(memory), screenshots=shots,
               **{key: metrics.get(key) for key in ("add_attempts", "add_retries", "duplicate_adds", "duplicate_peak", "uncertain_adds", "cart_checks", "inspected_products")})
    return row


def _counts(rows):
    known = [r for r in rows if type(r["success"]) is bool]
    passed = sum(r["success"] is True for r in known)
    size = len(rows)
    attempted = sum(r.get("run_id") is not None for r in rows)
    return {"planned": size, "attempted": attempted,
            "unknown_submissions": sum(r["state"] in {"unknown", "submitting"} for r in rows),
            "unstarted": sum(r["state"] == "not_started" for r in rows),
            "confirmed_attempt_rate": passed / attempted if attempted else None,
            "rate_denominator": "planned cases; confirmed_attempt_rate uses acknowledged run IDs",
            "finished": sum(r["status"] not in ACTIVE | {None} for r in rows),
            "passes": passed, "failures": len(known) - passed, "unknown": size - len(known),
            "confirmed_pass_rate": passed / size if size else None,
            "coverage": len(known) / size if size else None,
            "known_success_rate": passed / len(known) if known else None,
            "evidence_complete": sum(r["evidence_complete"] for r in rows),
            "statuses": dict(Counter(r["status"] or r["state"] for r in rows)),
            **{key: _distribution([r.get(key) for r in rows])
               for key in ("steps", "model_calls", "elapsed_seconds", "duplicates", "add_attempts", "add_retries", "duplicate_adds", "duplicate_peak", "uncertain_adds", "cart_checks", "inspected_products")},
            **{key: _distribution([(r.get("usage") or {}).get(key) for r in rows])
               for key in ("input_tokens", "output_tokens")},
            "fault_triggered": sum(r.get("fault_triggered") is True for r in rows),
            "fault_trigger_unknown": sum(type(r.get("fault_triggered")) is not bool for r in rows),
            "fault_planned": sum(r["spec"]["fault"] != "none" for r in rows),
            "screenshots": sum(r.get("screenshots", 0) for r in rows),
            "recovery_guidance": dict(sum((Counter(r.get("recovery_guidance", {})) for r in rows), Counter()))}


def _pairs(rows):
    paired = []
    for factor in ("recovery", "context_mode"):
        other = "context_mode" if factor == "recovery" else "recovery"
        for mode in sorted({r["spec"][other] for r in rows}):
            subset = [r for r in rows if r["spec"][other] == mode]
            arms = sorted({r["spec"][factor] for r in subset})
            for first, second in itertools.combinations(arms, 2):
                groups = []
                for arm in (first, second):
                    groups.append({(r["spec"]["task_id"], r["spec"]["seed"], r["spec"]["fault"]): r
                                   for r in subset if r["spec"][factor] == arm})
                left, right = groups
                keys = left.keys() | right.keys()
                counts = Counter()
                for key in keys:
                    if key not in left or key not in right:
                        counts["missing_pair"] += 1
                    elif type(left[key]["success"]) is not bool or type(right[key]["success"]) is not bool:
                        counts["unknown_pair"] += 1
                    else:
                        a, b = left[key]["success"], right[key]["success"]
                        counts["both_pass" if a and b else "first_only" if a else "second_only" if b else "both_fail"] += 1
                paired.append({"factor": factor, "fixed": {other: mode}, "first": first, "second": second,
                               "pairs": len(keys), **{key: counts[key] for key in
                                ("both_pass", "first_only", "second_only", "both_fail", "unknown_pair", "missing_pair")}})
    return paired


def report(output):
    """Rebuild JSON, CSV, and Markdown without network or model calls."""
    output = Path(output)
    plan, state, ident = _load(output)
    rows = [_row(item, output / "attempts" / item["id"], plan, ident, state)
            for item in state["attempts"]]
    groups = defaultdict(list)
    for row in rows:
        spec = row["spec"]
        groups[(spec["context_mode"], spec["recovery"], spec["fault"])].append(row)
    result = {"schema_version": REPORT, "plan": plan, "plan_hash": digest(plan),
              "identity": ident, "status": state["status"], "dataset": state.get("dataset"),
              "summary": _counts(rows), "groups": [
                  {"context_mode": key[0], "recovery": key[1], "fault": key[2], **_counts(group)}
                  for key, group in sorted(groups.items())], "pairs": _pairs(rows), "rows": rows,
              "notes": ["Rates describe the planned synthetic cases; failed and unknown attempts are retained.",
                        "Compare policies within matching task, seed, fault, model, context, and budget settings.",
                        "Missing token usage remains unknown. Repeated related cases are not independent samples.",
                        "A model ID and source fingerprint do not pin provider weights or service revisions."]}
    _write(output / "report.json", result)
    fields = ["id", "task_id", "seed", "fault", "recovery", "context_mode", "state", "run_id", "status", "success",
              "evidence_complete", "steps", "model_calls", "elapsed_seconds", "input_tokens", "output_tokens",
              "duplicates", "duplicate_adds", "duplicate_peak", "uncertain_adds", "add_attempts", "add_retries", "cart_checks", "inspected_products", "fault_triggered", "screenshots", "reason"]
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(buf, fieldnames=fields)
    writer.writeheader()
    for row in rows:
        flat = {**row, **row["spec"], **(row.get("usage") or {})}
        writer.writerow({key: flat.get(key) for key in fields})
    (output / "episodes.csv").write_text(buf.getvalue(), encoding="utf-8")
    stats = result["summary"]
    lines = ["# ShopTrace procurement experiment", "", plan["name"], "",
             f"Plan: `{result['plan_hash']}` · Split: {plan['split']} · Model: `{plan['model']}`", "",
             f"{stats['passes']}/{stats['planned']} confirmed passes; {stats['failures']} failures; "
             f"{stats['unknown']} unknown or unstarted. Complete evidence: {stats['evidence_complete']}/{stats['planned']}.", "",
             "| Context | Recovery | Fault | Pass / planned | Fail | Unknown | Calls | Input tokens |",
             "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: |"]
    for row in result["groups"]:
        calls = row["model_calls"]["sum"]
        tokens = row["input_tokens"]
        text = str(tokens["sum"]) if not tokens["missing_count"] else f"{tokens['sum']} known; {tokens['missing_count']} missing"
        lines.append(f"| {row['context_mode']} | {row['recovery']} | {row['fault']} | {row['passes']}/{row['planned']} | "
                     f"{row['failures']} | {row['unknown']} | {calls} | {text} |")
    lines += ["", "## Paired outcomes", "", "First/second refer to the named arms. Missing and unknown pairs remain separate.", ""]
    for pair in result["pairs"]:
        lines.append(f"- {pair['factor']}: {pair['first']} / {pair['second']} ({pair['fixed']}): "
                     f"both pass {pair['both_pass']}, first only {pair['first_only']}, second only {pair['second_only']}, "
                     f"both fail {pair['both_fail']}, unknown {pair['unknown_pair']}, missing {pair['missing_pair']}.")
    lines += ["", *result["notes"], "", "[Episodes](episodes.csv) · [Report data](report.json) · [Plan](plan.json)", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    from .benchmark import summarize
    attempts = []
    for item in state["attempts"]:
        folder = "attempts/" + item["id"]
        path = output / folder / "result.json"
        attempts.append({"task_id": item["spec"]["task_id"], "repeat": 1,
                         "folder": folder, "submission_state": item["state"],
                         "run_id": item.get("run_id"),
                         "run": read_json(path) if path.is_file() else None,
                         "evidence_collected": item["state"] == "finished"})
    _write(output / "batch.json", {"schema_version": "shoptrace.browser-batch.v1",
                                  "experiment_schema": STATE, "status": state["status"],
                                  "plan_hash": digest(plan), "runner_source": ident["runner_source"],
                                  "attempts": attempts, "statistics": summarize(attempts)})
    return result


def select(doc, factor, template, output):
    """Bind a next-stage plan to a completed development comparison."""
    doc = read_json(doc) if not isinstance(doc, dict) else doc
    plan, mapping = _report_map(doc)
    template = validate_plan(read_json(template) if not isinstance(template, dict) else template)
    if plan["split"] != "development" or factor not in {"recovery", "context_mode"}:
        raise BenchmarkError("Selection requires a development report and a supported comparison factor.")
    if any(template[key] != plan[key] for key in ("model", "benchmark", "budgets")):
        raise BenchmarkError("Next-stage model, benchmark, and budgets must match development.")
    rows = list(mapping.values())
    if any(not r.get("evidence_complete") or r.get("status") in ACTIVE | {None} for r in rows):
        raise BenchmarkError("Selection requires every planned attempt and its evidence.")
    src = (doc.get("identity") or {}).get("server_source")
    if (not src or any(r.get("source_fingerprint") != src or r.get("source") != "browser_model"
                       or r.get("model") != {"provider": "api", "name": plan["model"]} for r in rows)):
        raise BenchmarkError("Selection requires one frozen source revision.")
    control, candidate = ("verify", "adaptive") if factor == "recovery" else ("full_history", "page_memory")
    arms = [[r for r in rows if r["spec"][factor] == arm] for arm in (control, candidate)]
    if not all(arms):
        raise BenchmarkError("Selection report does not include both comparison arms.")
    keys = [{digest({k: v for k, v in r["spec"].items() if k != factor}) for r in arm} for arm in arms]
    if keys[0] != keys[1]:
        raise BenchmarkError("Selection requires complete matched cases.")
    left, right = [_counts(arm) for arm in arms]
    known = all(stats[key]["missing_count"] == 0 for stats in (left, right)
                for key in ("model_calls", "input_tokens", "output_tokens"))
    safe = right["passes"] >= left["passes"] and right["unknown"] <= left["unknown"]
    if factor == "context_mode":
        useful = known and right["input_tokens"]["sum"] < left["input_tokens"]["sum"]
    else:
        cost = lambda stats: (stats["model_calls"]["sum"],
                              stats["input_tokens"]["sum"] + stats["output_tokens"]["sum"])
        useful = right["passes"] > left["passes"]
        if known and right["passes"] == left["passes"]:
            a, b = cost(left), cost(right)
            useful = all(y <= x for x, y in zip(a, b)) and any(y < x for x, y in zip(a, b))
    selected = candidate if safe and useful else control
    decision = {"schema_version": "shoptrace.procurement-selection.v1", "factor": factor,
                "control": control, "candidate": candidate, "selected": selected,
                "reason": "Candidate met the saved success, coverage, and cost rule." if selected == candidate
                          else "Retained the control under the saved success, coverage, and cost rule.",
                "control_statistics": left, "candidate_statistics": right,
                "report_hash": digest(doc), "plan_hash": doc["plan_hash"],
                "source_fingerprint": src, "created_at": _now(),
                "rule": "No fewer confirmed passes or more unknown results; context needs fewer known input tokens; "
                        "recovery needs more passes, or equal passes with no greater calls/total tokens and a strict cost reduction."}
    template.pop("selection", None)
    if factor == "recovery":
        modes = {row["spec"]["context_mode"] for row in rows}
        if len(modes) != 1:
            raise BenchmarkError("Recovery selection requires one context mode held constant.")
        for case in template["cases"]:
            case["context_mode"] = next(iter(modes))
    for case in template["cases"]:
        case[factor] = selected
    template["selection"] = {"factor": factor, "selected": selected, "report_hash": digest(doc),
                             "source_fingerprint": src, "decision_hash": digest(decision)}
    validate_plan(template)
    output = Path(output)
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BenchmarkError("Selection output already exists.") from exc
    _write(output / "decision.json", decision, exclusive=True)
    _write(output / "selected-plan.json", template, exclusive=True)
    return decision


def archive(output):
    output = Path(output)
    files = [path for path in sorted(output.rglob("*")) if path.is_file()
             and path.name not in {"manifest.json", "evidence.zip"}]
    _write(output / "manifest.json", [{"path": str(path.relative_to(output)), "bytes": path.stat().st_size,
                                     "sha256": hashlib.sha256(path.read_bytes()).hexdigest()} for path in files])
    with zipfile.ZipFile(output / "evidence.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for path in [*files, output / "manifest.json"]:
            bundle.write(path, path.relative_to(output))


def _report_map(doc):
    if not isinstance(doc, dict) or doc.get("schema_version") != REPORT:
        raise BenchmarkError("Unsupported comparison report schema.")
    plan = validate_plan(doc.get("plan"))
    if doc.get("plan_hash") != digest(plan):
        raise BenchmarkError("Report plan fingerprint does not match its contents.")
    rows = doc.get("rows")
    if not isinstance(rows, list) or len(rows) != len(plan["cases"]):
        raise BenchmarkError("Comparison requires the complete planned case set.")
    expected = {case_id(case): _spec(plan, case) for case in plan["cases"]}
    result = {}
    for row in rows:
        key = row.get("id")
        if key not in expected or key in result or row.get("spec") != expected[key]:
            raise BenchmarkError("Report contains a missing, duplicate, or changed case.")
        if type(row.get("success")) not in {bool, type(None)}:
            raise BenchmarkError("Invalid report outcome.")
        if row.get("source") not in {None, "browser_model"}:
            raise BenchmarkError("Comparison requires model records, with other sources kept separate.")
        if row.get("model") not in (None, {"provider": "api", "name": plan["model"]}):
            raise BenchmarkError("Report model differs from its plan.")
        meta = row.get("benchmark")
        if meta is not None:
            spec = row["spec"]
            if (meta.get("id") != spec["task_id"] or meta.get("seed") != spec["seed"]
                    or meta.get("fault") != spec["fault"] or meta.get("split") != plan["split"]):
                raise BenchmarkError("Report benchmark case differs from its plan.")
        result[key] = row
    return plan, result


def compare(baseline, candidate, output=None):
    """Compare complete case sets under the same benchmark and settings."""
    first = read_json(baseline) if not isinstance(baseline, dict) else baseline
    second = read_json(candidate) if not isinstance(candidate, dict) else candidate
    left_plan, left = _report_map(first)
    right_plan, right = _report_map(second)
    for key in ("benchmark", "model", "budgets", "split"):
        if left_plan[key] != right_plan[key]:
            raise BenchmarkError("Comparison setting differs: " + key)
    if left.keys() != right.keys():
        raise BenchmarkError("Comparison requires identical complete case sets.")
    if (not first.get("dataset") or first.get("dataset") != second.get("dataset")):
        raise BenchmarkError("Comparison requires the same dataset fingerprint.")
    rows = []
    for key in sorted(left):
        a, b = left[key], right[key]
        for name in ("catalog_fingerprint", "dataset_fingerprint", "benchmark_fingerprint", "benchmark"):
            x = (a.get("benchmark") or {}).get(name)
            y = (b.get("benchmark") or {}).get(name)
            if not x or not y or x != y:
                raise BenchmarkError("Case benchmark identity is missing or differs: " + name)
        known = type(a["success"]) is bool and type(b["success"]) is bool
        complete = a.get("evidence_complete") is True and b.get("evidence_complete") is True
        source = all(row.get("source_fingerprint") and row.get("source") == "browser_model"
                     and row.get("model") == {"provider": "api", "name": left_plan["model"]}
                     for row in (a, b))
        reason = "regression" if a["success"] is True and b["success"] is False else (
            "blocked" if not known or not complete or not source else
            "improved" if a["success"] is False and b["success"] is True else "unchanged")
        rows.append({"id": key, "task_id": a["spec"]["task_id"], "baseline": a["success"],
                     "candidate": b["success"], "outcome": reason})
    counts = Counter(row["outcome"] for row in rows)
    status = "regression" if counts["regression"] else "blocked" if counts["blocked"] else "pass"
    result = {"schema_version": "shoptrace.procurement-comparison.v1", "status": status,
              "counts": dict(counts), "cases": rows, "baseline_plan": first["plan_hash"],
              "candidate_plan": second["plan_hash"],
              "note": "A non-regressing repeated run does not establish statistical equivalence or generalization."}
    if output is not None:
        output = Path(output)
        try:
            output.mkdir(parents=True, exist_ok=False)
        except FileExistsError as exc:
            raise BenchmarkError("Comparison output already exists.") from exc
        _write(output / "comparison.json", result)
        lines = ["# ShopTrace regression check", "", f"Result: **{status}**", "",
                 "| Case | Before | After | Outcome |", "| --- | --- | --- | --- |"]
        lines += [f"| {row['id']} | {row['baseline']} | {row['candidate']} | {row['outcome']} |" for row in rows]
        lines += ["", result["note"], ""]
        (output / "comparison.md").write_text("\n".join(lines), encoding="utf-8")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    subs = parser.add_subparsers(dest="command", required=True)
    run = subs.add_parser("run", help="Run or resume one fixed plan")
    run.add_argument("--plan", type=Path, required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--base-url", default="http://127.0.0.1:8775")
    run.add_argument("--resume", action="store_true")
    offline = subs.add_parser("report", help="Rebuild saved results without model calls")
    offline.add_argument("--output", type=Path, required=True)
    offline.add_argument("--archive", action="store_true")
    choice = subs.add_parser("select", help="Freeze a next-stage plan from a development comparison")
    choice.add_argument("--report", type=Path, required=True)
    choice.add_argument("--factor", choices=["recovery", "context_mode"], required=True)
    choice.add_argument("--template", type=Path, required=True)
    choice.add_argument("--output", type=Path, required=True)
    diff = subs.add_parser("compare", help="Check complete matching reports for regressions")
    diff.add_argument("--baseline", type=Path, required=True)
    diff.add_argument("--candidate", type=Path, required=True)
    diff.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == "run":
            result = run_plan(args.plan, args.output, base_url=args.base_url, resume=args.resume)
        elif args.command == "report":
            result = report(args.output)
            if args.archive:
                archive(args.output)
        elif args.command == "select":
            result = select(args.report, args.factor, args.template, args.output)
            print(json.dumps({"output": str(args.output), "selected": result["selected"], "reason": result["reason"]}))
            return 0
        else:
            result = compare(args.baseline, args.candidate, args.output)
            print(json.dumps({"output": str(args.output), "status": result["status"], "counts": result["counts"]}))
            return {"pass": 0, "regression": 1, "blocked": 2}[result["status"]]
    except KeyboardInterrupt:
        return 130
    except (BenchmarkError, OSError) as exc:
        text = str(exc) if isinstance(exc, BenchmarkError) else type(exc).__name__
        print(json.dumps({"status": "error", "message": text}), file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "summary": result["summary"]}, ensure_ascii=False, indent=2))
    stats = result["summary"]
    return 0 if stats["passes"] == stats["planned"] == stats["evidence_complete"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
