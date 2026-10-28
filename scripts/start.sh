#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -x .venv/bin/python ]]; then
  echo 'First run: python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt'
  exit 1
fi
exec .venv/bin/python -m uvicorn lab.app:app --host 127.0.0.1 --port "${LAB_PORT:-8765}"
