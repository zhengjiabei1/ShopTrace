#!/usr/bin/env python3
"""Check host dependencies and local services."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import importlib.metadata
import json
import os
import platform
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from urllib.parse import urlsplit


def memory_bytes() -> int | None:
    try:
        if platform.system() == "Darwin":
            return int(subprocess.check_output(["sysctl", "-n", "hw.memsize"], text=True, timeout=5).strip())
        return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def chip_name() -> str:
    if platform.system() == "Darwin":
        try:
            return subprocess.check_output(["sysctl", "-n", "machdep.cpu.brand_string"], text=True, timeout=5).strip()
        except (OSError, subprocess.SubprocessError):
            pass
    return platform.processor() or platform.machine()


def local_port_status(url: str) -> dict:
    if not url:
        return {"configured": False, "checked": False, "reason": "not_configured"}
    try:
        parsed = urlsplit(url)
        host = parsed.hostname
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if parsed.username or parsed.password:
            return {"configured": True, "checked": False, "reason": "userinfo_in_url_not_supported"}
        # Check local TCP connectivity only.
        if host not in {"localhost", "127.0.0.1", "::1"}:
            return {"configured": True, "checked": False, "reason": "remote_service_not_probed"}
        with socket.create_connection((host, port), timeout=1):
            return {"configured": True, "checked": True, "tcp_reachable": True, "api_validated": False}
    except (ValueError, OSError):
        return {"configured": bool(url), "checked": True, "tcp_reachable": False, "api_validated": False}


def report() -> dict:
    packages = {}
    for name in ["fastapi", "uvicorn", "httpx", "pydantic"]:
        try:
            packages[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            packages[name] = None
    root = Path(__file__).resolve().parents[1]
    total_mem = memory_bytes()
    data_root = Path(os.getenv("LAB_DATA_DIR", str(root / "artifacts")))
    return {
        "schema_version": "shopping-agent-doctor-v1",
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "host": {"system": platform.system(), "release": platform.release(), "architecture": platform.machine(),
                 "chip": chip_name(), "memory_gib": round(total_mem / 1024**3, 2) if total_mem else None,
                 "python": platform.python_version(), "python_supported": sys.version_info >= (3, 11),
                 "free_disk_gib": round(shutil.disk_usage(root).free / 1024**3, 2)},
        "packages": packages,
        "configuration": {name + "_present": bool(os.getenv(name)) for name in
                          ["LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "SHOPSIM_BASE_URL"]},
        "services": {
            "workbench": local_port_status("http://127.0.0.1:8765"),
            "shopsimulator": local_port_status(os.getenv("SHOPSIM_BASE_URL", "http://127.0.0.1:5700")),
            "model": local_port_status(os.getenv("LLM_BASE_URL", "")),
        },
        "data_directory": {"exists": data_root.exists()},
        "validation_scope": "macOS host detected; runtime/API functionality requires separate checks" if platform.system() == "Darwin"
                            else "Linux/other host checks do not establish macOS compatibility",
        "model_called": False,
        "cuda_required_for_workbench": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Save a report to a new file; refuses overwrite")
    args = parser.parse_args()
    result = report()
    rendered = json.dumps(result, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as handle:
            handle.write(rendered)
    print(rendered, end="")
    return 0 if result["host"]["python_supported"] and all(result["packages"].values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
