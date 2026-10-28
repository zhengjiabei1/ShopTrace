"""Finite API batches with preserved attempts and offline reports."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import html
import json
import math
from pathlib import Path
import re
import statistics
import subprocess
import sys
import time
from urllib.parse import urlsplit
import zipfile

import httpx

from lab.storage import sanitize

ACTIVE = {"pending", "running"}
SCHEMA = "shoptrace.browser-batch.v1"
RUN_ID = re.compile(r"[0-9a-f]{32}")
SHOT = re.compile(r"(?:obs-\d{4,6}|step-\d{3,6})\.png")


class BenchmarkError(ValueError):
    """Credential-free workbench diagnostic."""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _write(path, value, *, exclusive=False):
    raw = json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    if exclusive:
        with path.open("x", encoding="utf-8") as stream:
            stream.write(raw)
    else:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(raw, encoding="utf-8")
        temporary.replace(path)


def _base_url(value):
    try:
        parsed = urlsplit(value.strip().rstrip("/"))
        _ = parsed.port
        if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}
                or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path):
            raise ValueError()
    except ValueError as exc:
        raise BenchmarkError("Batch control requires a loopback HTTP(S) workbench URL without a path or credentials.") from exc
    return value.strip().rstrip("/")


def _request(client, method, path, **kwargs):
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise BenchmarkError("Workbench connection failed; task creation was not automatically retried.") from exc
    if response.status_code >= 300:
        raise BenchmarkError(f"Workbench returned HTTP {response.status_code}; response body omitted.")
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise BenchmarkError("Workbench returned invalid JSON.") from exc


def source_snapshot():
    root = Path(__file__).resolve().parents[1]
    try:
        commit = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=root, stderr=subprocess.DEVNULL, text=True).strip()
    except (OSError, subprocess.CalledProcessError):
        commit = None
    files = ["webagent/runtime.py", "webagent/browser.py", "webagent/tasks.py", "webagent/app.py",
             "webagent/benchmark.py", "webagent/cases.py", "webagent/case_assets/cases-v2-en-US.json",
             "lab/models.py", "lab/runtime.py", "lab/storage.py", "webagent/sandbox.py",
             "webagent/sandbox_assets/catalog.json", "webagent/sandbox_assets/app.js",
             "webagent/sandbox_assets/index.html", "webagent/sandbox_assets/styles.css",
             "webagent/procure.py", "webagent/memory.py", "webagent/recovery.py",
             "webagent/experiment.py"]
    files += [str(path.relative_to(root)) for path in sorted((root / "webagent/proc_assets").glob("*"))
              if path.is_file()]
    return {"commit": commit, "files": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                        for name in files if (root / name).is_file()},
            "meaning": "Runner checkout snapshot; actual server prompt/tools fingerprints are grouped separately."}


def _distribution(values):
    known = [value for value in values if type(value) in {int, float} and math.isfinite(value) and value >= 0]
    return {"known_count": len(known), "missing_count": len(values) - len(known),
            "sum": sum(known) if known else None,
            "mean": statistics.mean(known) if known else None,
            "median": statistics.median(known) if known else None}


def summarize(attempts):
    """Report known-score rates, coverage, and configuration groups."""
    runs = [a.get("run") for a in attempts if isinstance(a.get("run"), dict)]
    eligible = [r for r in runs if r.get("source") == "browser_model"
                and r.get("model", {}).get("provider") == "api" and r.get("task_id") != "custom"]
    groups = defaultdict(list)
    per_task = defaultdict(list)
    cfg_keys = {}
    for r in eligible:
        runtime = dict(r.get("runtime") or {})
        runtime.setdefault("context_mode", "full_history")
        case = r.get("benchmark_case") or {}
        dataset = {"dataset_fingerprint": r.get("dataset_fingerprint") or case.get("dataset_fingerprint"),
                   "split": case.get("split")}
        # Group suite cases by dataset version and split.
        if not case and r.get("catalog_fingerprint") is not None:
            dataset["catalog_fingerprint"] = r["catalog_fingerprint"]
        key = json.dumps({"model": r.get("model", {}).get("name"), "prompt_hash": r.get("prompt_hash"),
                          "tools_hash": r.get("tools_hash"), "protocol_version": r.get("protocol_version"),
                          "runtime": runtime, "context_policy": r.get("context_policy"),
                          "dataset": dataset}, sort_keys=True)
        groups[key].append(r)
        per_task[r.get("task_id")].append(r)
        cfg_keys[id(r)] = key

    def counts(rows):
        finished = [r for r in rows if r.get("status") not in ACTIVE]
        scored = [r for r in finished if type((r.get("result") or {}).get("success")) is bool]
        passed = sum(r["result"]["success"] is True for r in scored)
        return {"attempts": len(rows), "finished": len(finished), "successes": passed,
                "failures": len(scored) - passed, "unknown_results": len(finished) - len(scored),
                "known_results": len(scored), "success_rate_known": passed / len(scored) if scored else None,
                "result_coverage": len(scored) / len(finished) if finished else None,
                "statuses": dict(Counter(r.get("status") for r in rows)),
                "elapsed_seconds": _distribution([r.get("elapsed_seconds") for r in finished]),
                "model_calls": _distribution([r.get("model_calls") for r in finished]),
                "input_tokens": _distribution([(r.get("usage") or {}).get("input_tokens") for r in finished]),
                "output_tokens": _distribution([(r.get("usage") or {}).get("output_tokens") for r in finished])}

    result = counts(eligible)
    mixed = len(groups) > 1
    task_stats = {}
    for task, rows in sorted(per_task.items()):
        values = counts(rows)
        task_mixed = len({cfg_keys[id(row)] for row in rows}) > 1
        values["mixed_configuration"] = task_mixed
        if task_mixed:
            values["success_rate_known"] = None
        task_stats[task] = values
    # Keep rates separate across configurations.
    result.update(planned_attempts=len(attempts), eligible_model_attempts=len(eligible),
                  excluded_non_model_runs=len(runs) - len(eligible),
                  collection_errors=sum(bool(a.get("collection_error")) for a in attempts),
                  incomplete_submissions=sum(a.get("submission_state") in {"not_started", "submitting", "unknown"} for a in attempts),
                  mixed_configuration=mixed, pooled_success_rate=None if mixed else result["success_rate_known"],
                  per_task=task_stats,
                  configuration_groups=[{"configuration": json.loads(key), **counts(rows)} for key, rows in sorted(groups.items())],
                  unit="run_attempts_with_known_local_synthetic_results",
                  limitations=["Unknown results are not counted as failures; inspect coverage and statuses.",
                               "Repeated attempts are not independent tasks or evidence of statistical significance.",
                               "Local synthetic store results do not establish real merchant performance.",
                               "Mock transports and fixed-action policies are not live model evidence."])
    if mixed:
        result["success_rate_known"] = None
    return result


def _validate_run(run, run_id, spec):
    provider = spec.get("provider", "api")
    source = "browser_smoke" if provider == "browser_smoke" else "browser_model"
    if (run.get("run_id") != run_id or run.get("task_id") != spec["task_id"]
            or run.get("source") != source or run.get("model", {}).get("provider") != provider
            or any(run.get("runtime", {}).get(key) != spec[key] for key in ("max_steps", "max_seconds", "max_output_tokens"))):
        raise BenchmarkError("Workbench run identity, execution source, or budgets differ from the saved request.")
    if run.get("runtime", {}).get("context_mode", "full_history") != spec.get("context_mode", "full_history"):
        raise BenchmarkError("Workbench context mode differs from the saved request.")
    if run.get("status") not in ACTIVE | {"completed", "failed", "budget_exceeded", "cancelled"}:
        raise BenchmarkError("Workbench returned an unrecognized run status.")


def _collect(client, folder, run_id):
    document = _request(client, "GET", "/api/runs/" + run_id + "/export")
    if (document.get("run") or {}).get("run_id") != run_id:
        raise BenchmarkError("Exported run identity differs from the submitted run.")
    _validate_run(document["run"], run_id, json.loads((folder / "request.json").read_text()))
    _write(folder / "trajectory.json", document)
    _write(folder / "result.json", document["run"])
    shots = folder / "screenshots"
    shots.mkdir(exist_ok=True)
    names = set()
    for event in document.get("events", []):
        if event.get("type") != "observation":
            continue
        name = (event.get("payload", {}).get("observation") or {}).get("screenshot")
        if name is None:
            continue
        if not isinstance(name, str) or not SHOT.fullmatch(name):
            raise BenchmarkError("Export contains an invalid screenshot filename.")
        names.add(name)
    for name in sorted(names):
        try:
            response = client.get(f"/api/runs/{run_id}/screenshots/{name}")
        except httpx.HTTPError as exc:
            raise BenchmarkError("Screenshot collection failed; raw run export was preserved.") from exc
        if response.status_code != 200 or not response.content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise BenchmarkError("Screenshot is unavailable or not a PNG; raw run export was preserved.")
        (shots / name).write_bytes(response.content)
    return document["run"]


def _save_state(output, state):
    state["statistics"] = summarize(state["attempts"])
    _write(output / "batch.json", state)


def run_batch(base_url, task_ids, repeats, output, *, provider="api", max_steps=35, max_seconds=180,
              max_output_tokens=512, context_mode="full_history", client=None, sleep=time.sleep, poll_interval=1):
    """Run a finite batch in a new output directory."""
    base_url = _base_url(base_url)
    if provider not in {"api", "browser_smoke"}:
        raise BenchmarkError("Provider must be api or browser_smoke.")
    if type(repeats) is not int or not 1 <= repeats <= 10:
        raise BenchmarkError("Repeats must be an integer between 1 and 10.")
    if (not task_ids or len(task_ids) != len(set(task_ids)) or len(task_ids) > 100
            or any(not isinstance(t, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,100}", t) or t == "custom" for t in task_ids)):
        raise BenchmarkError("Provide 1–100 unique non-custom task IDs.")
    for value, lower, upper, label in ((max_steps, 1, 100, "steps"), (max_seconds, 1, 1800, "seconds"),
                                      (max_output_tokens, 32, 4096, "output tokens")):
        if type(value) is not int or not lower <= value <= upper:
            raise BenchmarkError("Invalid batch budget: " + label)
    if poll_interval < 0:
        raise BenchmarkError("Poll interval must be nonnegative.")
    if context_mode not in {"full_history", "latest_observation"}:
        raise BenchmarkError("Unknown context mode.")
    output = Path(output).resolve()
    try:
        output.mkdir(parents=True, exist_ok=False)
    except FileExistsError as exc:
        raise BenchmarkError("Output directory already exists; no tasks were submitted or retried.") from exc
    budgets = {"provider": provider, "max_steps": max_steps, "max_seconds": max_seconds,
               "max_output_tokens": max_output_tokens, "context_mode": context_mode}
    attempts = [{"task_id": task, "repeat": repeat, "folder": f"attempts/{task}/attempt-{repeat:02d}",
                 "submission_state": "not_started"} for repeat in range(1, repeats + 1) for task in task_ids]
    state = {"schema_version": SCHEMA, "created_at": _now(), "status": "planned", "base_url": base_url,
             "tasks": list(task_ids), "repeats": repeats, "budgets": budgets,
             "automatic_task_retries": 0, "runner_source": source_snapshot(), "attempts": attempts}
    _write(output / "plan.json", state, exclusive=True)
    _save_state(output, state)
    own_client = client is None
    # Only loopback control bypasses proxy settings.
    if own_client:
        client = httpx.Client(base_url=base_url, trust_env=False, timeout=30, follow_redirects=False)
    current = None
    try:
        public = _request(client, "GET", "/api/tasks")
        available = {item.get("id") for item in public.get("tasks", []) if isinstance(item, dict)}
        if set(task_ids) - available:
            expanded = _request(client, "GET", "/api/cases")
            available.update(item.get("id") for item in expanded.get("cases", []) if isinstance(item, dict))
            _write(output / "case-metadata.json", expanded, exclusive=True)
        if set(task_ids) - available:
            raise BenchmarkError("Requested tasks are absent from the workbench; no tasks were submitted.")
        _write(output / "task-metadata.json", public, exclusive=True)
        state["status"] = "running"
        _save_state(output, state)
        for current in attempts:
            folder = output / current["folder"]
            folder.mkdir(parents=True)
            # Omit the default mode for older workbenches.
            spec = {"task_id": current["task_id"], "provider": provider,
                    **{key: value for key, value in budgets.items()
                       if key not in {"provider", "context_mode"} or value != "full_history"}}
            _write(folder / "request.json", spec, exclusive=True)
            current["submission_state"] = "submitting"
            current["submission_started_at"] = _now()
            _save_state(output, state)  # Save before POST.
            try:
                run = _request(client, "POST", "/api/runs", json=spec)
            except BenchmarkError:
                current["submission_state"] = "unknown"
                raise
            run_id = run.get("run_id")
            if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
                current["submission_state"] = "unknown"
                raise BenchmarkError("Task creation did not return a valid run ID; do not resubmit automatically.")
            current.update(submission_state="acknowledged", run_id=run_id, run=run)
            _write(folder / "start-response.json", run, exclusive=True)
            _save_state(output, state)
            _validate_run(run, run_id, spec)
            deadline = time.monotonic() + max_seconds + 60
            while run.get("status") in ACTIVE:
                if time.monotonic() >= deadline:
                    raise BenchmarkError("Workbench polling deadline exceeded; use the saved run ID to inspect the original task.")
                sleep(poll_interval)
                run = _request(client, "GET", "/api/runs/" + run_id)
                _validate_run(run, run_id, spec)
                current["run"] = run
                _write(folder / "latest-status.json", run)
            current["run"] = run
            current["submission_state"] = "finished"
            _write(folder / "result.json", run)
            try:
                current["run"] = _collect(client, folder, run_id)
                current["evidence_collected"] = True
            except BenchmarkError as exc:
                current["collection_error"] = str(exc)
                current["evidence_collected"] = False
            _save_state(output, state)
        state["status"] = "completed"
        state["completed_at"] = _now()
    except (BenchmarkError, KeyboardInterrupt) as exc:
        state["status"] = "interrupted" if isinstance(exc, KeyboardInterrupt) else "stopped"
        state["runner_error"] = "Interrupted; original run IDs retained; no task creation was retried." if isinstance(exc, KeyboardInterrupt) else str(exc)
        if current is not None and current.get("submission_state") == "submitting":
            current["submission_state"] = "unknown"
        if current is not None and current.get("run_id"):
            # Collect the original run without submitting another.
            try:
                current["run"] = _collect(client, output / current["folder"], current["run_id"])
                current["evidence_collected"] = True
            except BenchmarkError as collection:
                current["collection_error"] = str(collection)
                current["evidence_collected"] = False
        _save_state(output, state)
        package_batch(output)
        if isinstance(exc, KeyboardInterrupt):
            raise
        raise BenchmarkError(str(exc)) from exc
    finally:
        if own_client:
            client.close()
    _save_state(output, state)
    package_batch(output)
    return state


def package_batch(output):
    """Build a report and archive from saved files."""
    output = Path(output)
    state = json.loads((output / "batch.json").read_text())
    sections = []
    escape = html.escape
    for attempt in state["attempts"]:
        run = attempt.get("run") or {}
        folder = output / attempt["folder"]
        path = folder / "trajectory.json"
        trajectory = json.loads(path.read_text()) if path.is_file() else {"events": []}
        steps = []
        for event in trajectory.get("events", []):
            if event.get("type") != "observation":
                continue
            payload = event.get("payload", {})
            obs = payload.get("observation") or {}
            name = obs.get("screenshot")
            image = ""
            if isinstance(name, str) and SHOT.fullmatch(name) and (folder / "screenshots" / name).is_file():
                image = '<img loading="lazy" alt="Saved browser observation" src="' + escape(attempt["folder"] + "/screenshots/" + name) + '">'
            steps.append('<details><summary>Observation ' + escape(str(payload.get("step"))) + '</summary>' + image +
                         '<pre>' + escape(str(obs.get("text", ""))) + '</pre></details>')
        links = ''.join('<a href="' + escape(attempt["folder"] + "/" + name) + '">' + label + '</a> · '
                        for name, label in (("request.json", "Request"), ("result.json", "Result"), ("trajectory.json", "Trajectory"))
                        if (folder / name).is_file())
        sections.append('<article><h2>' + escape(attempt["task_id"]) + ' / ' + str(attempt["repeat"]) +
                        '</h2><p>' + escape(json.dumps({"status": run.get("status"), "result": run.get("result"),
                                                      "submission": attempt["submission_state"], "collection_error": attempt.get("collection_error")}, ensure_ascii=False)) +
                        '</p><p>' + links + '</p>' + ''.join(steps) + '</article>')
    document = '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ShopTrace batch evidence</title><style>body{font:16px/1.6 system-ui;max-width:1100px;margin:32px auto;padding:0 20px;background:#f2f6ff;color:#18365b}article{background:white;padding:24px;margin:20px 0;border-radius:12px}img{max-width:100%;height:auto}pre,p{white-space:pre-wrap;overflow-wrap:anywhere}summary{cursor:pointer;padding:10px}a{color:#2350ad}</style>
<h1>ShopTrace batch evidence</h1><p>Offline report. Unknown scores are excluded from success rates; compare matching configurations.</p>
<p><a href="batch.json">All attempts and statistics</a> · <a href="plan.json">Original finite plan</a> · <a href="manifest.json">File hashes</a></p><pre>''' + escape(json.dumps(state["statistics"], ensure_ascii=False, indent=2)) + '</pre>' + ''.join(sections) + '</html>'
    (output / "index.html").write_text(document, encoding="utf-8")
    files = [p for p in sorted(output.rglob("*")) if p.is_file() and p.name not in {"manifest.json", "evidence.zip"}]
    manifest = [{"path": str(p.relative_to(output)), "bytes": p.stat().st_size,
                 "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in files]
    _write(output / "manifest.json", {"schema_version": SCHEMA, "files": manifest})
    with zipfile.ZipFile(output / "evidence.zip", "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in [*files, output / "manifest.json"]:
            archive.write(path, path.relative_to(output))


def add_arguments(parser):
    parser.add_argument("--base-url", default="http://127.0.0.1:8775")
    parser.add_argument("--task-id", action="append", help="Repeat for a subset; default is the six built-in tasks.")
    parser.add_argument("--provider", choices=["api", "browser_smoke"], default="api",
                        help="api runs the model; browser_smoke uses fixed actions with zero model calls.")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--max-steps", type=int, default=35)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--context-mode", choices=["full_history", "latest_observation"], default="full_history")
    return parser


def main(argv=None):
    parser = add_arguments(argparse.ArgumentParser(description=__doc__))
    args = parser.parse_args(argv)
    from .tasks import list_tasks
    task_ids = args.task_id or [task["id"] for task in list_tasks()]
    try:
        state = run_batch(args.base_url, task_ids, args.repeats, args.output,
                          provider=args.provider, max_steps=args.max_steps, max_seconds=args.max_seconds,
                          max_output_tokens=args.max_output_tokens, context_mode=args.context_mode)
    except KeyboardInterrupt:
        return 130
    except (BenchmarkError, OSError) as exc:
        print(json.dumps({"status": "error", "message": str(exc) if isinstance(exc, BenchmarkError) else type(exc).__name__}), file=sys.stderr)
        return 2
    print(json.dumps({"output": str(args.output), "statistics": state["statistics"]}, ensure_ascii=False, indent=2))
    stats = state["statistics"]
    if args.provider == "browser_smoke":
        runs = [a.get("run") or {} for a in state["attempts"]]
        ok = (len(runs) == state["statistics"]["planned_attempts"]
              and all(r.get("status") not in ACTIVE and type((r.get("result") or {}).get("success")) is bool
                      and r["result"]["success"] for r in runs)
              and not stats["collection_errors"])
        return 0 if ok else 1
    return 0 if (stats["finished"] == stats["planned_attempts"] == stats["eligible_model_attempts"]
                 and stats["unknown_results"] == 0 and stats["failures"] == 0
                 and not stats["collection_errors"] and not stats["mixed_configuration"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
