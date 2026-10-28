"""Web Agent commands."""
from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import importlib.metadata
import json
import os
from pathlib import Path
import platform
import shutil
import sys
import time
from urllib.parse import quote, urlsplit

from lab.storage import sanitize

ACTIVE = {"pending", "running"}
DEFAULT_BASE_URL = "http://127.0.0.1:8775"


class CLIError(ValueError):
    def __init__(self, message, *, run_id=None):
        super().__init__(message)
        self.run_id = run_id


def _render(value):
    return json.dumps(sanitize(value), ensure_ascii=False, indent=2, allow_nan=False)


def _base_url(value):
    value = value.strip().rstrip("/")
    try:
        parsed = urlsplit(value)
        _ = parsed.port
        if (parsed.scheme not in {"http", "https"} or not parsed.hostname
                or parsed.username or parsed.password or parsed.query or parsed.fragment):
            raise ValueError()
        if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError()
    except ValueError as exc:
        raise CLIError("Use a local HTTP or remote HTTPS URL without credentials, query parameters, or fragments.") from exc
    return value


def _client(value):
    import httpx
    return httpx.Client(base_url=_base_url(value) + "/", timeout=30, follow_redirects=False)


def _request(client, method, path, **kwargs):
    import httpx
    try:
        response = client.request(method, path, **kwargs)
    except httpx.HTTPError as exc:
        raise CLIError("Cannot reach the workbench. Check that serve is running on the specified port.") from exc
    if response.status_code >= 300:
        # Omit provider details from CLI errors.
        raise CLIError(f"Workbench request failed with HTTP {response.status_code}. Check its configuration and task status.")
    try:
        value = response.json()
        if not isinstance(value, dict):
            raise ValueError()
        return value
    except (ValueError, TypeError) as exc:
        raise CLIError("The workbench returned invalid JSON.") from exc


def _execution_arguments(parser):
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Local workbench URL (default port: 8775)")
    parser.add_argument("--max-steps", type=int, default=35)
    parser.add_argument("--max-seconds", type=int, default=180)
    parser.add_argument("--max-output-tokens", type=int, default=512)
    parser.add_argument("--context-mode", choices=["full_history", "latest_observation", "page_memory"], default="full_history")


def _budgets(args):
    result = {name: getattr(args, name) for name in ("max_steps", "max_seconds", "max_output_tokens")}
    if any(value <= 0 for value in result.values()):
        raise CLIError("Step, time, and output budgets must be positive.")
    result["context_mode"] = args.context_mode
    return result


def _wait(client, run):
    run_id = run.get("run_id")
    if not isinstance(run_id, str) or not run_id:
        raise CLIError("The workbench did not return a valid run_id.")
    path = "api/runs/" + quote(run_id, safe="")
    try:
        while run.get("status") in ACTIVE:
            time.sleep(1)
            try:
                run = _request(client, "GET", path)
            except CLIError as exc:
                raise CLIError("Could not read task status. Use run_id to check or cancel the task in the workbench.", run_id=run_id) from exc
    except KeyboardInterrupt:
        try:
            latest = _request(client, "POST", path + "/cancel", timeout=5)
            print(_render({"run_id": run_id, "cancel_request_sent": True, "run": latest}))
        except CLIError:
            print(_render({"run_id": run_id, "status": "unknown", "cancel_request_sent": False,
                           "message": "Cancellation was not confirmed. Check the task in the workbench."}))
        return 130
    print(_render(run))
    result = run.get("result") or {}
    return 0 if run.get("status") == "completed" and result.get("success") is not False else 1


def _browser_info():
    configured = os.environ.get("WEBAGENT_CHROMIUM_PATH", "").strip()
    if configured:
        path = Path(configured).expanduser()
        return {"source": "configured", "available": path.is_file() and os.access(path, os.X_OK)}
    system = shutil.which("chromium") or shutil.which("chromium-browser")
    if system:
        return {"source": "system", "available": True}
    # Inspect browser files without launching or downloading a browser.
    if sys.platform == "darwin":
        default = Path.home() / "Library/Caches/ms-playwright"
    elif os.name == "nt":
        default = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "ms-playwright"
    else:
        default = Path(os.environ.get("XDG_CACHE_HOME", str(Path.home() / ".cache"))) / "ms-playwright"
    cache_cfg = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    cache = Path(cache_cfg).expanduser() if cache_cfg and cache_cfg != "0" else default
    if cache_cfg == "0":
        try:
            import playwright
            cache = Path(playwright.__file__).parent / "driver/package/.local-browsers"
        except ImportError:
            pass
    available = False
    if cache.is_dir():
        patterns = ("chromium-*/chrome-linux/chrome", "chromium-*/chrome-linux64/chrome",
                    "chromium-*/chrome-mac/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-mac-arm64/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-mac-x64/Chromium.app/Contents/MacOS/Chromium",
                    "chromium-*/chrome-win/chrome.exe", "chromium-*/chrome-win64/chrome.exe",
                    "chromium_headless_shell-*/chrome-linux/headless_shell",
                    "chromium_headless_shell-*/chrome-linux64/headless_shell",
                    "chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell",
                    "chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell",
                    "chromium_headless_shell-*/chrome-headless-shell-mac-x64/chrome-headless-shell",
                    "chromium_headless_shell-*/chrome-headless-shell-win64/chrome-headless-shell.exe")
        available = any(p.is_file() and os.access(p, os.X_OK) for pattern in patterns for p in cache.glob(pattern))
    return {"source": "playwright_cache" if available else "unavailable", "available": available}


def doctor():
    packages = {}
    for name in ("fastapi", "uvicorn", "httpx", "pydantic", "playwright"):
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    browser = _browser_info()
    result = {"schema_version": "shoptrace-webagent-doctor-v1",
              "checked_at": datetime.now(timezone.utc).isoformat(),
              "python": platform.python_version(), "python_supported": sys.version_info >= (3, 11),
              "platform": platform.system(), "packages": packages, "browser": browser,
              "configuration": {key + "_present": bool(os.environ.get(key)) for key in
                                ("LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "OPENCODE_SESSION_ID",
                                 "WEBAGENT_ALLOWED_ORIGINS")},
              "browser_started": False, "model_called": False, "network_probed": False,
              "gpu_required_for_browser_agent": False,
              "scope": "Dependency/file presence; browser and model checks run separately."}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(prog="python -m webagent", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Start the local browser workbench")
    serve.add_argument("--host", choices=["127.0.0.1", "localhost", "::1"], default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8775)
    serve.add_argument("--data-dir", type=Path)
    commands.add_parser("doctor", help="Check dependencies and browser files offline")
    check = commands.add_parser("check-model", help="Check model availability and tools (at most two model requests)")
    check.add_argument("--deepseek", action="store_true", help="Default to the DeepSeek endpoint and deepseek-flash when unset")
    check.add_argument("--model-base-url", help="Model endpoint; defaults to LLM_BASE_URL")
    check.add_argument("--model", help="Model ID; defaults to LLM_MODEL")
    tasks = commands.add_parser("tasks", help="List built-in tasks")
    tasks.add_argument("--base-url", default=DEFAULT_BASE_URL)
    cases = commands.add_parser("cases", help="List additional cases by split")
    cases.add_argument("--split", choices=["development", "heldout"])
    cases.add_argument("--base-url", default=DEFAULT_BASE_URL)
    procure = commands.add_parser("procurement", help="List cross-page procurement tasks")
    procure.add_argument("--split", choices=["development", "heldout"])
    procure.add_argument("--base-url", default=DEFAULT_BASE_URL)
    from .benchmark import add_arguments
    benchmark = commands.add_parser("benchmark", help="Run a batch evaluation and save every attempt")
    add_arguments(benchmark)
    run = commands.add_parser("run", help="Run a task (fixed-script test by default)")
    run.add_argument("--task-id", required=True)
    run.add_argument("--provider", choices=["api", "browser_smoke"], default="browser_smoke")
    run.add_argument("--seed", type=int, default=7)
    run.add_argument("--fault", choices=["none", "ack_lost", "no_effect", "stockout"], default="none")
    run.add_argument("--recovery", choices=["baseline", "verify", "adaptive"], default="verify")
    _execution_arguments(run)
    custom = commands.add_parser("custom", help="Run an API task on an allowed site; results need manual review")
    custom.add_argument("--url", required=True)
    custom.add_argument("--goal", required=True)
    _execution_arguments(custom)
    export = commands.add_parser("export", help="Export a JSON trajectory to a new file")
    export.add_argument("--run-id", required=True)
    export.add_argument("--output", type=Path, required=True)
    export.add_argument("--base-url", default=DEFAULT_BASE_URL)
    args = parser.parse_args(argv)
    try:
        if args.command == "serve":
            if not 1 <= args.port <= 65535:
                raise CLIError("Port must be between 1 and 65535.")
            if args.data_dir:
                os.environ["WEBAGENT_DATA_DIR"] = str(args.data_dir.resolve())
            import uvicorn
            uvicorn.run("webagent.app:app", host=args.host, port=args.port)
            return 0
        if args.command == "doctor":
            report = doctor()
            print(_render(report))
            return 0 if report["python_supported"] and all(report["packages"].values()) and report["browser"]["available"] else 1
        if args.command == "benchmark":
            from .benchmark import main as bench_main
            arguments = list(argv) if argv is not None else sys.argv[1:]
            return bench_main(arguments[arguments.index("benchmark") + 1:])
        if args.command == "check-model":
            from .preflight import check_model
            report = asyncio.run(check_model(deepseek=args.deepseek, base_url=args.model_base_url, model=args.model))
            print(_render(report))
            return 0 if report["status"] == "passed" else 1
        if args.command == "export" and args.output.exists():
            raise CLIError("Export file already exists. Choose a new path.")
        with _client(args.base_url) as client:
            if args.command == "tasks":
                print(_render(_request(client, "GET", "api/tasks")))
                return 0
            if args.command == "cases":
                print(_render(_request(client, "GET", "api/cases", params={"split": args.split} if args.split else {})))
                return 0
            if args.command == "procurement":
                print(_render(_request(client, "GET", "api/procurement", params={"split": args.split} if args.split else {})))
                return 0
            if args.command == "export":
                document = _request(client, "GET", "api/runs/" + quote(args.run_id, safe="") + "/export")
                args.output.parent.mkdir(parents=True, exist_ok=True)
                with args.output.open("x", encoding="utf-8") as handle:
                    handle.write(_render(document) + "\n")
                print(_render({"output": str(args.output), "run_id": args.run_id}))
                return 0
            spec = _budgets(args)
            if args.command == "run":
                spec.update(task_id=args.task_id, provider=args.provider, seed=args.seed, fault=args.fault, recovery=args.recovery)
                run = _request(client, "POST", "api/runs", json=spec)
            else:
                spec.update(url=args.url, goal=args.goal, provider="api")
                run = _request(client, "POST", "api/custom-runs", json=spec)
            return _wait(client, run)
    except KeyboardInterrupt:
        return 130
    except (CLIError, OSError, ImportError, TypeError, ValueError) as exc:
        report = {"status": "error", "message": str(exc) if isinstance(exc, CLIError) else type(exc).__name__}
        if isinstance(exc, CLIError) and exc.run_id:
            report["run_id"] = exc.run_id
        print(_render(report), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
