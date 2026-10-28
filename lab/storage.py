"""Durable append-only events and run metadata, kept independently of the UI."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sqlite3
import threading
from typing import Any
import uuid

SCHEMA_VERSION = "shopping-lab.trajectory.v1"
ACTIVE_STATUSES = {"pending", "running"}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def sanitize(value: Any) -> Any:
    """Remove credential keys and exact configured credentials, even in errors."""
    sensitive = {"api_key", "apikey", "authorization", "password", "access_token", "refresh_token", "secret"}
    if isinstance(value, dict):
        return {str(k): "[REDACTED]" if str(k).lower().replace("-", "_") in sensitive else sanitize(v)
                for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, str):
        for name in ("LLM_API_KEY", "OPENAI_API_KEY"):
            secret = os.environ.get(name)
            if secret:
                value = value.replace(secret, "[REDACTED]")
        return value
    return value


class Store:
    def __init__(self, data_dir: str | Path):
        self.root = Path(data_dir).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "lab.sqlite3"
        self.lock = threading.RLock()
        with self.connect() as conn:
            conn.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, document TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events (
                    run_id TEXT NOT NULL, sequence INTEGER NOT NULL, document TEXT NOT NULL,
                    PRIMARY KEY(run_id, sequence), FOREIGN KEY(run_id) REFERENCES runs(run_id));
                CREATE TABLE IF NOT EXISTS annotations (
                    annotation_id TEXT PRIMARY KEY, run_id TEXT NOT NULL, document TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=10)
        try:
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA synchronous=FULL")
            with conn:
                yield conn
        finally:
            conn.close()

    def create_run(self, spec: dict, *, source: str | None = None) -> dict:
        run_id = uuid.uuid4().hex
        meta = {"schema_version": SCHEMA_VERSION, **spec, "run_id": run_id,
                "source": source or ("synthetic_fixture" if spec["mode"] == "fixture" else "live"),
                "created_at": utcnow(), "completed_at": None, "status": "pending",
                "termination_reason": None, "steps": 0, "model_calls": 0, "elapsed_seconds": 0,
                "usage": {"input_tokens": None, "output_tokens": None},
                "result": {"reward": None, "reward_valid": False, "strict_success": None, "purchase_success": None},
                "error": None}
        with self.lock, self.connect() as conn:
            conn.execute("INSERT INTO runs VALUES (?,?)", (run_id, json.dumps(sanitize(meta), ensure_ascii=False, allow_nan=False)))
        return self.get_run(run_id)

    def get_run(self, run_id: str) -> dict:
        with self.connect() as conn:
            row = conn.execute("SELECT document FROM runs WHERE run_id=?", (run_id,)).fetchone()
        if row is None:
            raise KeyError(run_id)
        return json.loads(row[0])

    def list_runs(self) -> list[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT document FROM runs ORDER BY rowid DESC").fetchall()
        return [json.loads(row[0]) for row in rows]

    def update_run(self, run_id: str, **changes) -> dict:
        with self.lock, self.connect() as conn:
            row = conn.execute("SELECT document FROM runs WHERE run_id=?", (run_id,)).fetchone()
            if row is None:
                raise KeyError(run_id)
            document = json.loads(row[0])
            document.update(sanitize(changes))
            document["run_id"] = run_id
            conn.execute("UPDATE runs SET document=? WHERE run_id=?",
                         (json.dumps(document, ensure_ascii=False, allow_nan=False), run_id))
        return document

    def append_event(self, run_id: str, event_type: str, payload: dict) -> dict:
        with self.lock, self.connect() as conn:
            seq = conn.execute("SELECT COALESCE(MAX(sequence),0)+1 FROM events WHERE run_id=?", (run_id,)).fetchone()[0]
            event = {"schema_version": SCHEMA_VERSION, "run_id": run_id, "event_id": f"e{seq:06d}",
                     "sequence": seq, "type": event_type, "payload": sanitize(payload), "created_at": utcnow()}
            conn.execute("INSERT INTO events VALUES (?,?,?)",
                         (run_id, seq, json.dumps(event, ensure_ascii=False, allow_nan=False)))
        return event

    def events(self, run_id: str, after: int = 0) -> list[dict]:
        self.get_run(run_id)
        with self.connect() as conn:
            rows = conn.execute("SELECT document FROM events WHERE run_id=? AND sequence>? ORDER BY sequence", (run_id, after)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def annotations(self, run_id: str) -> list[dict]:
        self.get_run(run_id)
        with self.connect() as conn:
            rows = conn.execute("SELECT document FROM annotations WHERE run_id=? ORDER BY rowid", (run_id,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def annotate(self, run_id: str, event_id: str, category: str, note: str) -> dict:
        if not any(e["event_id"] == event_id for e in self.events(run_id)):
            raise ValueError("event_id not found")
        annotation = {"annotation_id": uuid.uuid4().hex, "run_id": run_id, "event_id": event_id,
                      "category": category, "note": note, "created_at": utcnow()}
        with self.connect() as conn:
            conn.execute("INSERT INTO annotations VALUES (?,?,?)", (annotation["annotation_id"], run_id,
                         json.dumps(sanitize(annotation), ensure_ascii=False)))
        return annotation

    def export(self, run_id: str) -> dict:
        return {"schema_version": SCHEMA_VERSION, "run": self.get_run(run_id),
                "events": self.events(run_id), "annotations": self.annotations(run_id)}

    def recover_interrupted(self) -> int:
        stopped = [r for r in self.list_runs() if r["status"] in ACTIVE_STATUSES]
        for run in stopped:
            self.update_run(run["run_id"], status="failed", termination_reason="process_interrupted",
                            completed_at=utcnow(), error={"kind": "infrastructure", "message": "Process interrupted; task was not retried."})
            self.append_event(run["run_id"], "status", {"status": "failed", "reason": "process_interrupted"})
        return len(stopped)
