#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
PYTHON=python3
if [[ -x .venv/bin/python ]]; then PYTHON=.venv/bin/python; fi
"$PYTHON" -m unittest discover -s tests -v
if command -v node >/dev/null 2>&1; then
  for script in frontend/app.js webagent/frontend/app.js webagent/sandbox_assets/app.js webagent/proc_assets/app.js portal/dist/app.js; do
    if [[ -f "$script" ]]; then node --check "$script"; fi
  done
fi
"$PYTHON" -m unittest discover -s portal -p 'test_*.py' -v
