"""Command-line tools for runs, imports and exports."""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import signal
import sys

from .storage import Store, sanitize


def _json(value):
    return json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False)


def _write_new(path: str, text: str):
    dest = Path(path)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("x", encoding="utf-8") as handle:
        handle.write(text)
        if not text.endswith("\n"):
            handle.write("\n")


def _spec(args, task_id):
    from .fixtures import list_tasks
    if args.mode == "shopsimulator":
        if args.provider != "api":
            raise ValueError("ShopSimulator requires an API model; scripts are for fixtures")
        if not str(task_id).isdecimal():
            raise ValueError("ShopSimulator requires a nonnegative integer task_id")
        if not os.getenv("SHOPSIM_BASE_URL"):
            raise ValueError("Set SHOPSIM_BASE_URL first")
    elif str(task_id) not in {task["id"] for task in list_tasks()}:
        raise ValueError("Fixture task not found")
    if args.provider == "api" and not (os.getenv("LLM_BASE_URL") and os.getenv("LLM_MODEL")):
        raise ValueError("API models require LLM_BASE_URL and LLM_MODEL")
    if min(args.max_steps, args.max_seconds, args.max_calls, args.max_output_tokens) <= 0:
        raise ValueError("Run budgets must be positive")
    return {"task_id": str(task_id), "mode": args.mode,
            "model": {"provider": args.provider, "name": os.getenv("LLM_MODEL") if args.provider == "api" else "fixture-script", "stage": args.stage},
            "runtime": {"max_steps": args.max_steps, "max_seconds": args.max_seconds, "max_calls": args.max_calls, "max_output_tokens": args.max_output_tokens},
            "strategy": "default", "environment_version": "lab-fixture-v1" if args.mode == "fixture" else "unverified-shopsimulator",
            "upstream_commit": "9140f0d699b2feef7d11b8cbbcadbf612877096b"}


async def _execute(store, specs):
    from .runtime import Runtime
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
            installed.append(sig)
        except (NotImplementedError, RuntimeError):
            pass
    outcomes = []
    try:
        for spec in specs:
            if stop.is_set():
                break
            run = store.create_run(spec)
            await Runtime(store).run(run["run_id"], spec, stop)
            result = store.get_run(run["run_id"])
            outcomes.append(result)
            print(_json({"run_id": result["run_id"], "task_id": result["task_id"], "status": result["status"], "source": result["source"], "result": result["result"]}))
    finally:
        for sig in installed:
            loop.remove_signal_handler(sig)
    return outcomes


def _runtime_arguments(parser):
    parser.add_argument("--mode", choices=["fixture", "shopsimulator"], default="fixture")
    parser.add_argument("--provider", choices=["scripted", "api"], default="scripted")
    parser.add_argument("--stage", choices=["base", "sft", "rl", "other"], default="other")
    parser.add_argument("--max-steps", type=int, default=35)
    parser.add_argument("--max-seconds", type=int, default=120)
    parser.add_argument("--max-calls", type=int, default=35)
    parser.add_argument("--max-output-tokens", type=int, default=512)


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m lab.cli")
    parser.add_argument("--data-dir", default=os.environ.get("LAB_DATA_DIR", "artifacts"))
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("run", help="Run one task; defaults to a fixture")
    run.add_argument("--task-id", required=True)
    _runtime_arguments(run)
    batch = commands.add_parser("batch", help="Run a fixed JSONL task list in order, retaining failures")
    batch.add_argument("--tasks", required=True)
    _runtime_arguments(batch)
    report = commands.add_parser("report")
    report.add_argument("--output")
    load = commands.add_parser("import", help="Import Lab JSON or supported upstream JSON/JSONL")
    load.add_argument("path")
    export = commands.add_parser("export")
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", required=True)
    sft = commands.add_parser("export-sft")
    sft.add_argument("--run-id", action="append", required=True)
    sft.add_argument("--output", required=True)
    sft.add_argument("--splits", help="Train/validation/test split manifest; omitted samples remain unassigned")
    splits = commands.add_parser("validate-splits")
    splits.add_argument("path")
    args = parser.parse_args(argv)
    try:
        store = Store(args.data_dir)
        if args.command in {"run", "batch"}:
            task_ids = [args.task_id] if args.command == "run" else [item["task_id"] for item in _read_tasks(args.tasks)]
            specs = [_spec(args, task_id) for task_id in task_ids]
            outcomes = asyncio.run(_execute(store, specs))
            return 0 if outcomes and all(row["status"] == "completed" for row in outcomes) and len(outcomes) == len(specs) else 1
        if args.command == "report":
            from .evaluation import summarize
            report = _json(summarize(store.list_runs()))
            _write_new(args.output, report) if args.output else print(report)
        elif args.command == "import":
            from .importer import load_documents, prepare_import, persist_import
            prepared = [prepare_import(row) for row in load_documents(args.path)]
            runs = [persist_import(store, row) for row in prepared]
            print(_json({"imported": len(runs), "run_ids": [r["run_id"] for r in runs], "verified": False}))
        elif args.command == "export":
            _write_new(args.output, _json(store.export(args.run_id)))
            print(_json({"output": args.output}))
        elif args.command == "export-sft":
            from .datasets import build_sft_records
            split_map = None
            if args.splits:
                split_doc = json.loads(Path(args.splits).read_text(encoding="utf-8"))
                if not isinstance(split_doc, dict):
                    raise ValueError("Split manifest must be a JSON object")
                split_map = split_doc.get("splits", split_doc)
            records, report = build_sft_records([store.export(run_id) for run_id in dict.fromkeys(args.run_id)], split_manifest=split_map)
            report["export_purpose"] = "assigned_interchange_review" if split_map is not None else "unassigned_preview_only"
            report["training_ready"] = False
            output = "".join(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n" for record in records)
            _write_new(args.output, output)
            print(_json({"output": args.output, "report": report}))
        elif args.command == "validate-splits":
            from .datasets import validate_splits
            document = json.loads(Path(args.path).read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("Split manifest must be a JSON object")
            result = validate_splits(document.get("splits", document))
            print(_json(result))
            return 0 if result.get("valid", result.get("ok", False)) else 1
        return 0
    except (ValueError, KeyError, OSError, TypeError) as exc:
        print(_json({"error": type(exc).__name__, "message": str(exc)}), file=sys.stderr)
        return 2


def _read_tasks(path):
    rows = [json.loads(line) for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows or any(not isinstance(row, dict) or "task_id" not in row for row in rows):
        raise ValueError("Task manifest requires one JSON object with task_id per line")
    task_ids = [str(row["task_id"]) for row in rows]
    if len(set(task_ids)) != len(task_ids):
        raise ValueError("Duplicate task_id; use separate manifests for repeated experiments")
    return rows


if __name__ == "__main__":
    raise SystemExit(main())
