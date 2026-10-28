"""Local Web Agent API and workbench."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import importlib.metadata
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field
from starlette.middleware.trustedhost import TrustedHostMiddleware

from lab.storage import Store, sanitize, utcnow
from .runtime import BrowserRuntime, evaluation

ROOT = Path(__file__).resolve().parent
EXPORT_SCHEMA = "shoptrace.webagent.trajectory.v1"


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)
    provider: Literal["api", "browser_smoke"] = "browser_smoke"
    max_steps: int = Field(default=35, ge=1, le=100)
    max_seconds: int = Field(default=180, ge=1, le=1800)
    max_output_tokens: int = Field(default=512, ge=32, le=4096)
    context_mode: Literal["full_history", "latest_observation", "page_memory"] = "full_history"
    seed: int = Field(default=7, ge=0, le=2147483647)
    fault: Literal["none", "ack_lost", "no_effect", "stockout"] = "none"
    recovery: Literal["baseline", "verify", "adaptive"] = "verify"


class CustomSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    url: str = Field(min_length=1, max_length=2000)
    goal: str = Field(min_length=1, max_length=2000)
    provider: Literal["api"] = "api"
    max_steps: int = Field(default=35, ge=1, le=100)
    max_seconds: int = Field(default=180, ge=1, le=1800)
    max_output_tokens: int = Field(default=512, ge=32, le=4096)
    context_mode: Literal["full_history", "latest_observation", "page_memory"] = "full_history"


def source_info():
    root = ROOT.parent
    paths = ["webagent/app.py", "webagent/runtime.py", "webagent/browser.py", "webagent/tasks.py",
             "webagent/procure.py", "webagent/memory.py", "webagent/recovery.py", "webagent/cases.py",
             "webagent/sandbox.py", "lab/models.py", "lab/runtime.py", "lab/storage.py"]
    for folder in ("proc_assets", "sandbox_assets", "case_assets"):
        paths.extend(str(p.relative_to(root)) for p in sorted((ROOT / folder).glob("*")) if p.is_file())
    files = {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in paths if (root / name).is_file()}
    stamp = hashlib.sha256(json.dumps(files, sort_keys=True).encode()).hexdigest()
    return {"source_fingerprint": stamp, "source_files": files}


def origin(url):
    p = urlsplit(url)
    if p.scheme not in {"http", "https"} or not p.hostname or p.username or p.password:
        raise ValueError("Only HTTP(S) page URLs without embedded credentials are supported.")
    if p.fragment:
        raise ValueError("Start URLs must not contain fragments.")
    port = p.port
    host = f"[{p.hostname}]" if ":" in p.hostname else p.hostname
    return p.scheme + "://" + host + (f":{port}" if port and port != (443 if p.scheme == "https" else 80) else "")


def browser_available():
    try:
        importlib.metadata.version("playwright")
    except importlib.metadata.PackageNotFoundError:
        return False
    candidate = os.environ.get("WEBAGENT_CHROMIUM_PATH") or shutil.which("chromium") or shutil.which("chromium-browser")
    if candidate:
        return Path(candidate).is_file()
    # Inspect installed browser files without launching Chromium.
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            return Path(p.chromium.executable_path).is_file()
    except Exception:
        return False


def create_app(data_dir=None):
    source = source_info()
    store = Store(data_dir or os.environ.get("WEBAGENT_DATA_DIR", ROOT.parent / "artifacts" / "webagent"))
    artifacts = store.root / "screenshots"
    jobs, cancel_map = {}, {}
    semaphore = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        yield
        for flag in cancel_map.values():
            flag.set()
        if jobs:
            await asyncio.gather(*list(jobs.values()), return_exceptions=True)

    app = FastAPI(title="ShopTrace Web Agent", version="1.0.0", lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.state.store, app.state.artifacts = store, artifacts

    def same_source():
        try:
            return source_info()["source_fingerprint"] == source["source_fingerprint"]
        except OSError:
            return False

    def check_source():
        if not same_source():
            raise HTTPException(409, "Server source or store assets changed. Restart the workbench before starting another run.")

    def check_space():
        try:
            free = shutil.disk_usage(store.root).free
        except OSError:
            raise HTTPException(503, "Run storage is unavailable.") from None
        if free < 100 * 1024 * 1024:
            raise HTTPException(503, "At least 100 MiB of free disk space is required to save a run.")

    @app.middleware("http")
    async def boundaries(request: Request, call_next):
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if request.headers.get("origin") and request.headers["origin"] != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Only same-origin control requests are accepted."}, status_code=403)
            body = await request.body()
            if len(body) > 64 * 1024:
                return JSONResponse({"detail": "Request body exceeds 64 KiB."}, status_code=413)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def get_run(run_id):
        if not re.fullmatch(r"[0-9a-f]{32}", run_id):
            raise HTTPException(404, "Run not found")
        try:
            return store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "Run not found") from None

    def check_provider(provider):
        if provider == "api" and not (os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL")):
            raise HTTPException(409, "Configure LLM_BASE_URL and LLM_MODEL on the server; supply the key through LLM_API_KEY.")

    async def execute(run_id, document):
        try:
            async with semaphore:
                if cancel_map[run_id].is_set():
                    store.update_run(run_id, status="cancelled", completed_at=utcnow(), termination_reason="cancelled_while_queued")
                    return
                if not same_source():
                    store.update_run(run_id, status="failed", completed_at=utcnow(), termination_reason="source_changed_while_queued",
                                     source_unchanged=False, result={"success": None, "reason": "Server source changed before execution."})
                    store.append_event(run_id, "status", {"status": "failed", "reason": "source_changed_while_queued"})
                    return
                runner = BrowserRuntime(store, artifacts)
                runner.source_check = same_source
                await runner.run(run_id, document, cancel_map[run_id])
        except Exception as exc:
            store.update_run(run_id, status="failed", completed_at=utcnow(), termination_reason="worker_error", error={"kind": "infrastructure", "message": type(exc).__name__})
        finally:
            jobs.pop(run_id, None)
            cancel_map.pop(run_id, None)

    def launch(document):
        check_source()
        check_space()
        if len(jobs) >= 6:
            raise HTTPException(429, "Too many active browser runs")
        document["source_fingerprint"] = source["source_fingerprint"]
        run = store.create_run(document, source="browser_smoke" if document["model"]["provider"] == "browser_smoke" else "browser_model")
        store.update_run(run["run_id"], result={"success": None, "reason": "not_started"})
        cancel_map[run["run_id"]] = asyncio.Event()
        jobs[run["run_id"]] = asyncio.create_task(execute(run["run_id"], document))
        return store.get_run(run["run_id"])

    @app.get("/api/status")
    def status():
        return {"application": "shoptrace-webagent", "display_name": "ShopTrace Web Agent", "version": "1.0.0", **source, "source_current": same_source(),
            "model_name": os.environ.get("LLM_MODEL") or None,
            "capabilities": {"browser_available": browser_available(), "llm_configured": bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL"))},
            "limitations": ["Local synthetic catalog", "Script and model results are reported separately", "Run the model preflight before starting tasks", "External-site results need manual review"]}

    @app.get("/api/tasks")
    def tasks():
        from .tasks import list_tasks
        return {"tasks": list_tasks()}

    @app.get("/api/cases")
    def cases(split: Literal["development", "heldout"] | None = None):
        from .cases import list_cases
        return {"cases": list_cases(split)}

    @app.get("/api/procurement")
    def procurement(split: Literal["development", "heldout"] | None = None):
        from .procure import list_tasks
        return {"tasks": list_tasks(split)}

    @app.post("/api/runs", status_code=201)
    async def start(spec: RunSpec, request: Request):
        check_source()
        check_space()
        from .tasks import get_task, list_tasks
        is_proc = spec.task_id.startswith("procure-")
        try:
            if is_proc:
                from .procure import get_task as proc_task
                task = proc_task(spec.task_id)
            else:
                task = get_task(spec.task_id)
        except (ValueError, KeyError):
            raise HTTPException(422, "Unknown browser task") from None
        check_provider(spec.provider)
        case_meta = {}
        run_cfg = {"max_steps": spec.max_steps, "max_seconds": spec.max_seconds,
                   "max_output_tokens": spec.max_output_tokens, "context_mode": spec.context_mode}
        if is_proc:
            from .procure import new_case
            if spec.provider != "api":
                raise HTTPException(422, "Procurement tasks require api provider.")
            if len(jobs) >= 6:
                raise HTTPException(429, "Too many active browser runs")
            try:
                case = new_case(spec.task_id, spec.seed, spec.fault)
            except ValueError as exc:
                raise HTTPException(422, str(exc)) from None
            task = {**task, "start_path": case["start_path"]}
            case_meta = {"benchmark_case": case["metadata"]}
            run_cfg["recovery"] = spec.recovery
        elif spec.seed != 7 or spec.fault != "none" or spec.recovery != "verify":
            raise HTTPException(422, "Seed, fault, and recovery settings apply only to procurement tasks.")
        elif spec.task_id not in {t["id"] for t in list_tasks()}:
            from .cases import private_case, catalog_fingerprint, dataset_fingerprint
            if spec.provider != "api":
                raise HTTPException(422, "Expanded cases require api provider; no fixed-action policy is supplied.")
            case_meta = {"benchmark_case": {"id": spec.task_id, "split": private_case(spec.task_id)["split"],
                         "catalog_fingerprint": catalog_fingerprint(spec.task_id), "dataset_fingerprint": dataset_fingerprint()}}
        base = str(request.base_url).rstrip("/")
        allowed = origin(base)
        return launch({"schema_version": EXPORT_SCHEMA, "mode": "browser", "task_id": task["id"], "query": task["query"],
            "model": {"provider": spec.provider, "name": os.environ.get("LLM_MODEL") if spec.provider == "api" else "fixed-browser-smoke"},
            "runtime": run_cfg,
            **case_meta,
            "browser": {"start_url": base + task.get("start_path", "/sandbox/"), "allowed_origins": [allowed],
                        "allowed_path_prefixes": {allowed: ["/sandbox/procurement/"] if is_proc else ["/sandbox/"]}}})

    @app.post("/api/custom-runs", status_code=201)
    async def custom(spec: CustomSpec):
        check_provider("api")
        try:
            start_orig = origin(spec.url)
            allowed = [origin(value.strip()) for value in os.environ.get("WEBAGENT_ALLOWED_ORIGINS", "").split(",") if value.strip()]
        except ValueError:
            raise HTTPException(422, "Invalid start URL or allowed origin configuration") from None
        if start_orig not in allowed:
            raise HTTPException(422, "Add the starting origin to WEBAGENT_ALLOWED_ORIGINS on the server before running an external-site task.")
        return launch({"schema_version": EXPORT_SCHEMA, "mode": "browser", "task_id": "custom", "query": spec.goal,
            "model": {"provider": "api", "name": os.environ["LLM_MODEL"]},
            "runtime": {"max_steps": spec.max_steps, "max_seconds": spec.max_seconds, "max_output_tokens": spec.max_output_tokens, "context_mode": spec.context_mode},
            "browser": {"start_url": spec.url, "allowed_origins": allowed, "allowed_path_prefixes": {value: ["/"] for value in allowed}}})

    @app.get("/api/runs")
    def runs():
        return {"runs": store.list_runs()}

    @app.get("/api/runs/{run_id}")
    def run(run_id: str):
        return get_run(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str):
        get_run(run_id)
        return {"events": store.events(run_id)}

    @app.get("/api/runs/{run_id}/screenshots/{name}")
    def screenshot(run_id: str, name: str):
        get_run(run_id)
        if not re.fullmatch(r"(?:obs-\d{4,6}|step-\d{3,6})\.png", name):
            raise HTTPException(404, "Screenshot not found")
        path = artifacts / run_id / name
        if not path.is_file():
            raise HTTPException(404, "Screenshot not found")
        return FileResponse(path, media_type="image/png")

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run = get_run(run_id)
        if run_id in cancel_map:
            cancel_map[run_id].set()
            store.append_event(run_id, "status", {"status": "cancellation_requested"})
        return run

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        get_run(run_id)
        data = store.export(run_id)
        data["schema_version"] = EXPORT_SCHEMA
        return JSONResponse(sanitize(data), headers={"Content-Disposition": f'attachment; filename="webagent-{run_id}.json"'})

    @app.get("/api/evaluation")
    def evaluate():
        return evaluation(store.list_runs())

    from .sandbox import router
    app.include_router(router)
    from .procure import router as proc_route
    app.include_router(proc_route)
    app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")

    @app.get("/")
    def index():
        return FileResponse(ROOT / "frontend" / "index.html")

    @app.get("/styles.css")
    def styles():
        return FileResponse(ROOT / "frontend" / "styles.css", media_type="text/css")

    @app.get("/app.js")
    def javascript():
        return FileResponse(ROOT / "frontend" / "app.js", media_type="application/javascript")

    @app.get("/favicon.svg")
    def favicon():
        return FileResponse(ROOT / "frontend" / "favicon.svg", media_type="image/svg+xml")

    return app


app = create_app()
