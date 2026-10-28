#!/usr/bin/env python3
"""Build a static replay of browser-smoke recordings."""
from __future__ import annotations

import argparse
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lab.storage import Store
from webagent.runtime import evaluation
from webagent.tasks import list_tasks


def build(destination: Path, data_dir: Path):
    destination = destination.resolve()
    if destination == ROOT or ROOT / "webagent" == destination or (ROOT / "webagent") in destination.parents:
        raise ValueError("Use a destination outside the application source directory.")
    if not (data_dir / "lab.sqlite3").is_file():
        raise ValueError("Record browser-smoke runs before building the replay.")
    store = Store(data_dir)
    records, by_task = [], {}
    for run in store.list_runs():
        if run.get("source") == "browser_smoke" and run.get("status") == "completed" and run.get("model_calls") == 0:
            by_task.setdefault(run["task_id"], run)
    for task in list_tasks():
        if task["id"] in by_task:
            records.append(by_task[task["id"]])
    if not records:
        raise ValueError("No completed browser-smoke recordings found.")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("index.html", "styles.css", "app.js", "favicon.svg"):
        content = (ROOT / "webagent/frontend" / name).read_bytes()
        if name == "index.html":
            content = content.replace(b'<html lang="en">', b'<html lang="en" data-preview="true">', 1)
            if b'data-preview="true"' not in content:
                raise ValueError("Could not mark the static replay mode.")
        (destination / name).write_bytes(content)
    sandbox = destination / "sandbox"
    shutil.copytree(ROOT / "webagent/sandbox_assets", sandbox / "assets", dirs_exist_ok=True)
    html_src = (ROOT / "webagent/sandbox_assets/index.html").read_bytes()
    for child in (sandbox, sandbox / "product", sandbox / "cart"):
        child.mkdir(parents=True, exist_ok=True)
        (child / "index.html").write_bytes(html_src)
    events, images = {}, []
    for run in records:
        run_id = run["run_id"]
        sequence = deepcopy(store.events(run_id))
        for event in sequence:
            payload = event.get("payload", {})
            obs = payload.get("observation")
            if not isinstance(obs, dict):
                continue
            original = obs.get("url", "")
            parts = urlsplit(original)
            if parts.path.startswith("/sandbox/") or parts.path in {"/sandbox/product", "/sandbox/cart"}:
                obs["recorded_url"] = original
                obs["url"] = parts.path + ("?" + parts.query if parts.query else "")
            name = obs.get("screenshot")
            if not isinstance(name, str) or not re.fullmatch(r"obs-\d{4,6}\.png", name):
                raise ValueError("A browser observation has no supported recorded screenshot.")
            source = data_dir / "screenshots" / run_id / name
            if not source.is_file():
                raise ValueError("A recorded screenshot is missing.")
            relative = Path("assets") / run_id / name
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            payload["screenshot_url"] = relative.as_posix()
            obs["screenshot"] = relative.as_posix()
            images.append({"path": relative.as_posix(), "sha256": hashlib.sha256(source.read_bytes()).hexdigest()})
        events[run_id] = sequence
    run_rows = [{key: value for key, value in run.items() if key != "browser"} for run in records]
    data = {"schema_version": "shoptrace.webagent.replay.v1", "runs": run_rows, "events": events,
        "tasks": list_tasks(), "evaluation": evaluation(records),
        "preview_meta": {"mode": "recorded_browser_smoke_replay", "live_browser": False,
            "model_called": False, "generated_at": datetime.now(timezone.utc).isoformat(),
            "url_mapping": "Sandbox links map to the hosted copy; recorded_url retains the original recording origin.",
            "scope": "Chromium screenshots of the local sandbox; fixed actions, zero model calls."}}
    (destination / "preview-data.json").write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"destination": str(destination), "recorded_runs": len(records), "screenshots": len(images),
            "model_calls": 0, "live_execution": False, "image_hashes": images}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--data-dir", type=Path, default=ROOT / "artifacts/webagent")
    args = parser.parse_args()
    print(json.dumps(build(args.destination, args.data_dir.resolve()), ensure_ascii=False, indent=2))
