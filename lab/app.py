"""Local FastAPI service. Bind to loopback; credentials stay in the backend."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import json
import os
from pathlib import Path
import platform
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, ConfigDict, Field

from lab import __version__
from lab.storage import ACTIVE_STATUSES, SCHEMA_VERSION, Store, sanitize, utcnow

ROOT = Path(__file__).resolve().parent.parent
MAX_BODY = 16 * 1024 * 1024


class ModelSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    provider: Literal["scripted", "api"] = "scripted"
    name: str = Field(default="fixture-script", min_length=1, max_length=200)
    stage: Literal["base", "sft", "rl", "other"] = "other"


class RuntimeSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_steps: int = Field(default=35, ge=1, le=100)
    max_seconds: int = Field(default=120, ge=1, le=3600)
    max_calls: int = Field(default=35, ge=1, le=200)
    max_output_tokens: int = Field(default=512, ge=32, le=8192)


class RunSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    task_id: str = Field(min_length=1, max_length=100)
    mode: Literal["fixture", "shopsimulator"] = "fixture"
    model: ModelSpec = Field(default_factory=ModelSpec)
    runtime: RuntimeSpec = Field(default_factory=RuntimeSpec)
    strategy: Literal["default"] = "default"


class AnnotationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    event_id: str = Field(max_length=100)
    category: Literal["retrieval", "evidence", "memory", "specification", "tool", "environment", "scoring", "other"]
    note: str = Field(min_length=1, max_length=2000)


def create_app(data_dir: str | Path | None = None) -> FastAPI:
    store = Store(data_dir or os.environ.get("LAB_DATA_DIR", ROOT / "artifacts"))
    jobs: dict[str, asyncio.Task] = {}
    cancels: dict[str, asyncio.Event] = {}
    semaphore = asyncio.Semaphore(2)

    @asynccontextmanager
    async def lifespan(app):
        store.recover_interrupted()
        yield
        for flag in cancels.values():
            flag.set()
        pending = list(jobs.values())
        for job in pending:
            job.cancel()
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    app = FastAPI(title="ShopTrace", version=__version__, lifespan=lifespan)
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]", "testserver"])
    app.state.store = store

    @app.middleware("http")
    async def local_boundaries(request: Request, call_next):
        # Reject cross-origin writes.
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
                return JSONResponse({"detail": "Same-origin requests required"}, status_code=403)
            try:
                if int(request.headers.get("content-length", "0")) > MAX_BODY:
                    return JSONResponse({"detail": "File must not exceed 16 MiB"}, status_code=413)
            except ValueError:
                return JSONResponse({"detail": "Invalid Content-Length"}, status_code=400)
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > MAX_BODY:
                    return JSONResponse({"detail": "File must not exceed 16 MiB"}, status_code=413)
            request._body = bytes(body)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"
        return response

    def run_or_404(run_id):
        try:
            return store.get_run(run_id)
        except KeyError:
            raise HTTPException(404, "Run not found") from None

    async def read_object(request: Request) -> dict:
        try:
            value = await request.json()
        except (ValueError, TypeError):
            raise HTTPException(422, "Request must be a JSON object") from None
        if not isinstance(value, dict):
            raise HTTPException(422, "Request must be a JSON object")
        return value

    @app.get("/api/status")
    def status():
        lock_path = ROOT / "upstream.lock.json"
        lock = json.loads(lock_path.read_text()) if lock_path.exists() else {}
        return {"application": "shopping-agent-lab", "display_name": "ShopTrace", "version": __version__, "platform": platform.system(), "model_name": os.environ.get("LLM_MODEL") or None,
                "upstream": {"commit": "9140f0d699b2feef7d11b8cbbcadbf612877096b", "license_status": "No explicit license found; upstream code and data are not bundled", "lock": lock},
                "capabilities": {"fixture": True, "shopsimulator_configured": bool(os.environ.get("SHOPSIM_BASE_URL")),
                                 "llm_configured": bool(os.environ.get("LLM_BASE_URL") and os.environ.get("LLM_MODEL"))},
                "limitations": ["Fixture tests are excluded from model success rates", "API credentials are read from server environment variables", "Not tested on Mac hardware", "Custom tasks have no automatic score"]}

    @app.get("/api/tasks")
    def tasks():
        from lab.fixtures import list_tasks
        return {"tasks": list_tasks()}

    @app.get("/api/runs")
    def runs():
        return {"runs": store.list_runs()}

    async def execute(run_id, spec):
        from lab.runtime import Runtime
        try:
            async with semaphore:
                if cancels[run_id].is_set():
                    store.update_run(run_id, status="cancelled", termination_reason="user_cancelled", completed_at=utcnow())
                    return
                await Runtime(store).run(run_id, spec, cancels[run_id])
        except asyncio.CancelledError:
            store.update_run(run_id, status="cancelled", termination_reason="server_shutdown", completed_at=utcnow())
            raise
        except Exception as exc:
            # Hide provider response bodies.
            store.update_run(run_id, status="failed", termination_reason="runtime_error", completed_at=utcnow(),
                             error={"kind": "infrastructure", "message": f"Runtime error: {type(exc).__name__}"})
            store.append_event(run_id, "error", {"kind": "infrastructure", "message": type(exc).__name__})
        finally:
            cancels.pop(run_id, None)
            jobs.pop(run_id, None)

    @app.post("/api/runs", status_code=201)
    async def start(spec: RunSpec):
        if sum(r["status"] in ACTIVE_STATUSES for r in store.list_runs()) >= 10:
            raise HTTPException(429, "At most 10 queued or running tasks are allowed")
        if spec.mode == "shopsimulator":
            if spec.model.provider != "api":
                raise HTTPException(422, "ShopSimulator requires an API model")
            if not spec.task_id.isdecimal():
                raise HTTPException(422, "ShopSimulator requires an integer task_id")
            if not os.environ.get("SHOPSIM_BASE_URL"):
                raise HTTPException(409, "Configure SHOPSIM_BASE_URL first")
        else:
            from lab.fixtures import list_tasks
            if spec.task_id not in {str(t["id"]) for t in list_tasks()}:
                raise HTTPException(422, "Fixture task not found")
        if spec.model.provider == "api":
            model_name = os.environ.get("LLM_MODEL")
            if not os.environ.get("LLM_BASE_URL") or not model_name:
                raise HTTPException(409, "Configure LLM_BASE_URL and LLM_MODEL first")
            # Use the server-configured model.
            spec.model.name = model_name
        document = spec.model_dump()
        document["upstream_commit"] = "9140f0d699b2feef7d11b8cbbcadbf612877096b"
        document["environment_version"] = "lab-fixture-v1" if spec.mode == "fixture" else "unverified-shopsimulator"
        run = store.create_run(document)
        cancels[run["run_id"]] = asyncio.Event()
        jobs[run["run_id"]] = asyncio.create_task(execute(run["run_id"], document))
        return run

    @app.get("/api/runs/{run_id}")
    def get_run(run_id: str):
        return run_or_404(run_id)

    @app.get("/api/runs/{run_id}/events")
    def events(run_id: str, after: int = Query(0, ge=0)):
        run_or_404(run_id)
        result = store.events(run_id, after)
        return {"events": result, "next_sequence": result[-1]["sequence"] if result else after}

    @app.post("/api/runs/{run_id}/cancel")
    def cancel(run_id: str):
        run = run_or_404(run_id)
        flag = cancels.get(run_id)
        if flag is not None:
            flag.set()
            store.append_event(run_id, "status", {"status": "cancellation_requested"})
        return run

    @app.get("/api/runs/{run_id}/export")
    def export(run_id: str):
        run_or_404(run_id)
        return JSONResponse(sanitize(store.export(run_id)), headers={"Content-Disposition": f'attachment; filename="trajectory-{run_id}.json"'})

    @app.get("/api/runs/{run_id}/annotations")
    def annotations(run_id: str):
        run_or_404(run_id)
        return {"annotations": store.annotations(run_id)}

    @app.post("/api/runs/{run_id}/annotations", status_code=201)
    def annotate(run_id: str, spec: AnnotationSpec):
        run_or_404(run_id)
        try:
            store.annotate(run_id, **spec.model_dump())
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return {"annotations": store.annotations(run_id)}

    @app.post("/api/import", status_code=201)
    async def import_trajectory(request: Request):
        from lab.importer import prepare_import, persist_import
        try:
            envelope = await request.json()
            if not isinstance(envelope, dict):
                raise ValueError("trajectory must be an object")
            data = envelope.get("trajectory", envelope)
            prepared = prepare_import(data)
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(422, "Invalid trajectory format; expected Lab v1 or upstream raw/normalized-v1 JSON") from None
        return persist_import(store, prepared)

    @app.get("/api/evaluation")
    def evaluation():
        from lab.evaluation import summarize
        return summarize(store.list_runs())

    @app.post("/api/evaluation")
    async def selected_evaluation(request: Request):
        from lab.evaluation import summarize
        body = await read_object(request)
        ids = body.get("run_ids", [])
        if not isinstance(ids, list) or len(ids) > 10000 or any(not isinstance(x, str) for x in ids):
            raise HTTPException(422, "run_ids must be a list of strings")
        return summarize([run_or_404(i) for i in dict.fromkeys(ids)])

    @app.get("/api/compare")
    def compare(left: str, right: str):
        from lab.evaluation import compare_runs
        return compare_runs(run_or_404(left), run_or_404(right))

    @app.post("/api/datasets/validate")
    async def validate_dataset(request: Request):
        from lab.datasets import validate_splits
        body = await read_object(request)
        try:
            return validate_splits(body["splits"])
        except (ValueError, TypeError, KeyError, AttributeError):
            raise HTTPException(422, "Expected a splits object containing train/validation/test task lists") from None

    @app.post("/api/datasets/export-sft")
    async def export_dataset(request: Request):
        from lab.datasets import build_sft_records
        body = await read_object(request)
        ids = body.get("run_ids", [])
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids) or len(ids) > 10000:
            raise HTTPException(422, "run_ids must be a list of strings")
        if body.get("splits") is not None and not isinstance(body["splits"], dict):
            raise HTTPException(422, "splits must be an object")
        records, report = build_sft_records([store.export(i) for i in dict.fromkeys(ids) if run_or_404(i)], body.get("splits"))
        return {"records": records, "report": report}

    frontend = ROOT / "frontend"
    if frontend.exists():
        app.mount("/static", StaticFiles(directory=frontend), name="static")

    @app.get("/")
    def index():
        return FileResponse(frontend / "index.html")

    return app


app = create_app()
