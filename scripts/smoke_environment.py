#!/usr/bin/env python3
"""Check shopping reset/search/open endpoints without model calls."""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import platform
import sys
import time


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lab.environments import EnvironmentError, ShopSimulatorEnvironment  # noqa: E402
from lab.runtime import parse_action  # noqa: E402


def summary(tool: str, frame: dict) -> dict:
    """Export field names and counts only."""
    state = frame["state"]
    return {
        "tool": tool,
        "observation_version": state.get("observation_version"),
        "page_type": state.get("page_type"),
        "public_field_names": sorted(state),
        "candidate_count": len(state.get("products", [])),
        "option_axis_count": len(state.get("available_options", {})),
        "action_count": len(state.get("actions", [])),
        "done": frame.get("done") is True,
    }


def checked_action(tool: str, arguments: dict, state: dict) -> str:
    # Reuse the workbench action validator.
    message = {"tool_calls": [{"id": "manual-protocol-smoke", "type": "function",
                              "function": {"name": tool, "arguments": json.dumps(arguments)}}]}
    return parse_action(message, state)[1]


async def run_check(args: argparse.Namespace) -> dict:
    started = time.monotonic()
    report = {
        "schema_version": "shopping-real-environment-smoke-v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "execution_kind": "manual_protocol_smoke",
        "task_id": args.task_id,
        "task_split_verified_by_script": False,
        "host": {"system": platform.system(), "architecture": platform.machine(),
                 "python": platform.python_version()},
        "model_called": False,
        "purchase_attempted": False,
        "agent_task_completed": False,
        "official_task_success": None,
        "steps": [],
        "lease_released": False,
        "client_closed": False,
        "error": None,
    }
    env = None
    try:
        env = ShopSimulatorEnvironment()
        frame = await env.reset(str(args.task_id))
        report["steps"].append(summary("reset", frame))
        if args.query:
            frame = await env.step(checked_action("search_products", {"query": args.query}, frame["state"]))
            report["steps"].append(summary("search_products", frame))
        if args.open_first:
            products = frame["state"].get("products", [])
            if not products:
                raise EnvironmentError("No public search candidate available to open")
            frame = await env.step(checked_action("open_product", {"asin": products[0]["asin"]}, frame["state"]))
            report["steps"].append(summary("open_product", frame))
    except Exception as exc:
        # Adapter errors omit private response bodies.
        report["error"] = {"kind": type(exc).__name__, "message": str(exc) if isinstance(exc, (EnvironmentError, ValueError)) else "Protocol smoke failed; private response omitted"}
    finally:
        if env is not None:
            try:
                await env.release()
                report["lease_released"] = True
            except Exception as exc:
                report["release_error"] = {"kind": type(exc).__name__, "message": "Environment release failed; check the service lease"}
            finally:
                try:
                    await env.close()
                    report["client_closed"] = True
                except Exception as exc:
                    report["close_error"] = {"kind": type(exc).__name__, "message": "Client close failed"}
    report["protocol_check_passed"] = bool(report["steps"]) and report["error"] is None and report["lease_released"] and report["client_closed"]
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["limits"] = ["Manual protocol check", "Model and purchase calls disabled", "Exports field names and counts only", "Select a development task; split checks are external", "Endpoint source commit unavailable"]
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task-id", type=int, required=True, help="Predefined development task ID")
    parser.add_argument("--query", help="Optional manual search query; omit for reset/release only")
    parser.add_argument("--open-first", action="store_true", help="Explicitly open the first public result; requires --query")
    parser.add_argument("--output", type=Path, help="Save summary JSON to a new file; refuses overwrite")
    args = parser.parse_args()
    if args.task_id < 0:
        parser.error("--task-id must be non-negative")
    if args.query is not None and not args.query.strip():
        parser.error("--query must be non-empty when supplied")
    if args.open_first and not args.query:
        parser.error("--open-first requires --query")
    if args.output and args.output.exists():
        parser.error("Refusing to overwrite an existing output file")
    result = asyncio.run(run_check(args))
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0 if result["protocol_check_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
