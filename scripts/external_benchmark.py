#!/usr/bin/env python3
"""Plan, run, and summarize the read-only WebVoyager slice.

This is a small external check, not a WebVoyager leaderboard result. The
upstream WebVoyager protocol allows writes and uses an LLM judge; this slice
uses read-only tasks and manual grading so a run can be audited without a
second model.

Examples:
    python scripts/external_benchmark.py --list
    python scripts/external_benchmark.py --plan
    python scripts/external_benchmark.py --run --limit 3
    python scripts/external_benchmark.py --summary work/external-benchmark/grades.csv
"""
from __future__ import annotations

import argparse
import csv
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import re
import shlex
import subprocess
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = ROOT / "benchmarks" / "external" / "webvoyager-readonly-30.json"
DEFAULT_OUTPUT = ROOT / "work" / "external-benchmark"
REQUIRED = {"id", "site", "start_url", "question", "grading", "rubric", "stability", "read_only", "source"}
WRITE_ACTION = re.compile(
    r"\b(add|buy|purchase|book|reserve|subscribe|post|comment|checkout|download|upload|log ?in|sign ?up|create account|send)\b",
    re.IGNORECASE,
)


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != "shoptrace.external-benchmark.v1":
        raise ValueError("Unexpected external benchmark schema")
    tasks = data.get("tasks")
    expected = data.get("task_count", 30)
    if not isinstance(tasks, list) or type(expected) is not int or len(tasks) != expected:
        raise ValueError(f"The read-only slice must contain {expected} tasks")
    return data


def validate(data: dict) -> list[str]:
    problems: list[str] = []
    ids = set()
    for index, task in enumerate(data["tasks"]):
        missing = REQUIRED - set(task)
        if missing:
            problems.append(f"task {index}: missing {sorted(missing)}")
            continue
        if task["id"] in ids:
            problems.append(f"duplicate id: {task['id']}")
        ids.add(task["id"])
        if not task["read_only"]:
            problems.append(f"{task['id']}: read_only must be true")
        if not str(task["start_url"]).startswith("https://"):
            problems.append(f"{task['id']}: start_url must be HTTPS")
        extra = task.get("extra_origins", [])
        if not isinstance(extra, list) or any(not isinstance(value, str) or not value.startswith("https://")
                                              for value in extra):
            problems.append(f"{task['id']}: extra_origins must be an HTTPS list")
        if WRITE_ACTION.search(task["question"]):
            problems.append(f"{task['id']}: question looks like a write action")
        if task["grading"] != "manual":
            problems.append(f"{task['id']}: this slice only supports manual grading")
        if task["stability"] not in {"stable", "snapshot"}:
            problems.append(f"{task['id']}: unknown stability value")
    return problems


def origins(data: dict) -> list[str]:
    result = []
    for task in data["tasks"]:
        for value in [task["start_url"], *task.get("extra_origins", [])]:
            parsed = urlsplit(value)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            if origin not in result:
                result.append(origin)
    return result


def _command(task: dict, *, context_mode: str, max_steps: int, max_seconds: int,
             max_output_tokens: int, base_url: str) -> list[str]:
    return [sys.executable, "-m", "webagent", "custom", "--base-url", base_url,
            "--url", task["start_url"], "--goal", task["question"],
            "--context-mode", context_mode, "--max-steps", str(max_steps),
            "--max-seconds", str(max_seconds), "--max-output-tokens", str(max_output_tokens)]


def _select(data: dict, *, limit: int | None, site: str | None,
            task_ids: list[str] | None) -> list[dict]:
    tasks = data["tasks"]
    if site:
        tasks = [task for task in tasks if task["site"].casefold() == site.casefold()]
    if task_ids:
        wanted = set(task_ids)
        unknown = wanted - {task["id"] for task in tasks}
        if unknown:
            raise ValueError("Unknown task ID: " + ", ".join(sorted(unknown)))
        tasks = [task for task in tasks if task["id"] in wanted]
    if limit is not None:
        tasks = tasks[:limit]
    return tasks


def run(data: dict, output: Path, *, context_mode: str, max_steps: int, max_seconds: int,
        max_output_tokens: int, base_url: str, limit: int | None, site: str | None,
        task_ids: list[str] | None) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    tasks = _select(data, limit=limit, site=site, task_ids=task_ids)
    if not tasks:
        raise ValueError("No tasks selected")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    log_path = output / f"runs-{stamp}.jsonl"
    passed = 0
    with log_path.open("w", encoding="utf-8") as log:
        for task in tasks:
            command = _command(task, context_mode=context_mode, max_steps=max_steps,
                               max_seconds=max_seconds, max_output_tokens=max_output_tokens,
                               base_url=base_url)
            print(f"RUN {task['id']}", flush=True)
            try:
                completed = subprocess.run(command, capture_output=True, text=True,
                                           timeout=max_seconds + 60, check=False)
                stdout = completed.stdout.strip()
                payload = json.loads(stdout) if stdout.startswith("{") else None
                row = {"id": task["id"], "task": task,
                       "returncode": completed.returncode, "result": payload,
                       "stderr": completed.stderr[-2000:]}
            except (subprocess.TimeoutExpired, json.JSONDecodeError, OSError) as exc:
                row = {"id": task["id"], "task": task, "returncode": None, "result": None,
                       "error": f"{type(exc).__name__}: {exc}"}
            log.write(json.dumps(row, ensure_ascii=False) + "\n")
            log.flush()
            if row.get("result") and isinstance(row["result"], dict):
                print(f"  id={row['result'].get('run_id')} status={row['result'].get('status')}", flush=True)
            else:
                print(f"  failed: {row.get('error') or row.get('stderr', '')[:200]}", flush=True)
    return {"tasks": len(tasks), "log": str(log_path)}


def _wilson(passed: int, total: int, z: float = 1.96) -> tuple[float, float] | None:
    if total == 0:
        return None
    p = passed / total
    denominator = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denominator
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def summary(data: dict, grades_path: Path) -> dict:
    by_id = {task["id"]: task for task in data["tasks"]}
    with grades_path.open(encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    grades = {}
    for row in rows:
        task_id = (row.get("id") or "").strip()
        if task_id not in by_id:
            raise ValueError(f"Unknown task id in grades: {task_id}")
        value = (row.get("success") or "").strip().lower()
        if value not in {"true", "false", "unknown"}:
            raise ValueError(f"Grade for {task_id} must be true, false, or unknown")
        grades[task_id] = value
    known = [value for value in grades.values() if value in {"true", "false"}]
    passed = sum(value == "true" for value in known)
    unknown = sum(value == "unknown" for value in grades.values())
    attempted = len(grades)
    interval = _wilson(passed, len(known))
    sites = {}
    for task_id, value in grades.items():
        site = by_id[task_id]["site"]
        item = sites.setdefault(site, {"attempted": 0, "passed": 0, "known": 0})
        item["attempted"] += 1
        if value in {"true", "false"}:
            item["known"] += 1
            item["passed"] += value == "true"
    return {
        "manifest_tasks": len(data["tasks"]),
        "attempted": attempted,
        "passed": passed,
        "failed": len(known) - passed,
        "unknown": unknown,
        "known": len(known),
        "coverage": round(attempted / len(data["tasks"]), 4),
        "known_success_rate": round(passed / len(known), 4) if known else None,
        "wilson_95": [round(value, 4) for value in interval] if interval else None,
        "missing_grades": sorted(set(by_id) - set(grades)),
        "by_site": dict(sorted(sites.items())),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--list", action="store_true", help="List selected tasks")
    parser.add_argument("--plan", action="store_true", help="Print server and CLI commands")
    parser.add_argument("--run", action="store_true", help="Run tasks against a local workbench")
    parser.add_argument("--summary", type=Path, metavar="GRADES_CSV", help="Summarize a manual grades CSV")
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--site")
    parser.add_argument("--task-id", action="append", dest="task_ids",
                        help="Task ID to run; repeat the flag to select several tasks")
    parser.add_argument("--context-mode", choices=["full_history", "latest_observation", "page_memory"],
                        default="latest_observation")
    parser.add_argument("--max-steps", type=int, default=50)
    parser.add_argument("--max-seconds", type=int, default=240)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--base-url", default="http://127.0.0.1:8775")
    args = parser.parse_args(argv)
    try:
        data = load_manifest(args.manifest)
        problems = validate(data)
        if problems:
            raise ValueError("Manifest validation failed: " + "; ".join(problems))
        if args.list:
            for task in data["tasks"]:
                print(f"{task['id']}\t{task['site']}\t{task['stability']}\t{task['question']}")
            return 0
        if args.plan:
            print("# Start the workbench with every origin used by this slice:")
            print("export WEBAGENT_ALLOWED_ORIGINS=" + shlex.quote(",".join(origins(data))))
            print("python -m webagent serve --port 8775")
            print("# Then run one task at a time:")
            for task in _select(data, limit=args.limit, site=args.site, task_ids=args.task_ids):
                command = _command(task, context_mode=args.context_mode, max_steps=args.max_steps,
                                   max_seconds=args.max_seconds, max_output_tokens=args.max_output_tokens,
                                   base_url=args.base_url)
                print(f"# {task['id']}")
                print(" ".join(shlex.quote(part) for part in command))
            return 0
        if args.run:
            result = run(data, args.output, context_mode=args.context_mode, max_steps=args.max_steps,
                         max_seconds=args.max_seconds, max_output_tokens=args.max_output_tokens,
                         base_url=args.base_url, limit=args.limit, site=args.site, task_ids=args.task_ids)
            print(json.dumps(result, indent=2))
            return 0
        if args.summary:
            print(json.dumps(summary(data, args.summary), indent=2))
            return 0
        parser.print_help()
        return 2
    except (OSError, ValueError, KeyError) as exc:
        print(f"External benchmark stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
