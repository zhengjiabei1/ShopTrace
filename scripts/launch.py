#!/usr/bin/env python3
"""Launch this source checkout locally, opening a browser only after readiness."""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import platform
import socket
import subprocess
import sys
import time


ROOT = Path(__file__).resolve().parents[1]


def inspect_service(port: int) -> str:
    """Return free, workbench or occupied."""
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=.4):
            pass
    except ConnectionRefusedError:
        return "free"
    except OSError:
        return "occupied"
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=.6)
    try:
        connection.request("GET", "/api/status")
        response = connection.getresponse()
        if response.status != 200:
            return "occupied"
        body = response.read(128 * 1024 + 1)
        if len(body) > 128 * 1024:
            return "occupied"
        data = json.loads(body)
        if (isinstance(data, dict) and data.get("application") == "shopping-agent-lab" and data.get("version")
                and isinstance(data.get("capabilities"), dict)
                and data["capabilities"].get("fixture") is True):
            return "workbench"
    except (OSError, ValueError, http.client.HTTPException):
        pass
    finally:
        connection.close()
    return "occupied"


def environment_python() -> Path:
    """Create or validate the project venv."""
    directory = ROOT / ".venv"
    executable = directory / "bin" / "python"
    if directory.exists():
        if not executable.is_file():
            raise RuntimeError("Existing .venv has no usable bin/python and was left unchanged. Check or rename it before retrying.")
    else:
        print("First launch: creating the project .venv.", flush=True)
        subprocess.run([sys.executable, "-m", "venv", str(directory)], check=True, cwd=ROOT)
    check = subprocess.run([str(executable), "-c", "import sys; raise SystemExit(sys.version_info < (3,11))"], cwd=ROOT)
    if check.returncode:
        raise RuntimeError("Existing .venv has an invalid interpreter or Python below 3.11 and was left unchanged.")
    reqs = ROOT / "requirements.txt"
    lock = ROOT / "requirements.lock.txt"
    req_hash = hashlib.sha256(reqs.read_bytes() + lock.read_bytes()).hexdigest()
    marker = directory / ".shopping-lab-requirements.sha256"
    current = marker.read_text().strip() if marker.is_file() else None
    if current != req_hash:
        print("Installing workbench dependencies. Network access is required.", flush=True)
        subprocess.run([str(executable), "-m", "pip", "install", "-r", str(reqs)], check=True, cwd=ROOT)
        marker.write_text(req_hash + "\n")
    return executable


def show_browser(url: str, enabled: bool) -> None:
    print(f"Workbench ready: {url}", flush=True)
    print("Open this address on the computer running the launcher.", flush=True)
    if enabled and platform.system() == "Darwin":
        opened = subprocess.run(["open", url], check=False)
        if opened.returncode:
            print("Could not open a browser. Open the address above manually.", flush=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="Check paths, Python, and port without installation or startup")
    parser.add_argument("--no-browser", action="store_true", help="Print the local URL without opening a browser")
    args = parser.parse_args(argv)
    if sys.version_info < (3, 11):
        print("Python 3.11 or later is required.", file=sys.stderr)
        return 1
    try:
        port = int(os.environ.get("LAB_PORT", "8765"))
        if not 1 <= port <= 65535:
            raise ValueError()
    except ValueError:
        print("LAB_PORT must be an integer between 1 and 65535.", file=sys.stderr)
        return 1
    current = inspect_service(port)
    if args.check:
        print(json.dumps({"project_root": str(ROOT), "python": platform.python_version(),
                          "platform": platform.system(), "venv_exists": (ROOT / ".venv").exists(),
                          "service": current, "url": f"http://127.0.0.1:{port}",
                          "model_called": False, "dependencies_installed": False,
                          "macos_runtime_validated": False}, ensure_ascii=False, indent=2))
        return 0 if current != "occupied" else 1
    if current == "workbench":
        print("The workbench is already running on this computer.", flush=True)
        show_browser(f"http://127.0.0.1:{port}", not args.no_browser)
        return 0
    if current == "occupied":
        print(f"Port {port} is used by another service. Free it or set LAB_PORT before retrying.", file=sys.stderr)
        return 1
    process = None
    try:
        executable = environment_python()
        print("Starting the local workbench. Keep this terminal open; press Control-C to stop.", flush=True)
        process = subprocess.Popen([str(executable), "-m", "uvicorn", "lab.app:app", "--host", "127.0.0.1", "--port", str(port)], cwd=ROOT)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise RuntimeError("The backend exited during startup. Check the error above.")
            if inspect_service(port) == "workbench":
                show_browser(f"http://127.0.0.1:{port}", not args.no_browser)
                return process.wait()
            time.sleep(.2)
        raise RuntimeError("Timed out waiting for the workbench to become ready.")
    except KeyboardInterrupt:
        print("\nStopping the workbench started by this launcher.", flush=True)
        return 0
    except (OSError, RuntimeError, subprocess.CalledProcessError) as exc:
        print(f"Startup failed: {exc}", file=sys.stderr)
        return 1
    finally:
        if process is not None and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)


if __name__ == "__main__":
    raise SystemExit(main())
