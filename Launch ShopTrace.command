#!/bin/bash
# Double-click on macOS, or invoke from any working directory.
set -u
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
source "$APP_DIR/scripts/find-python.sh"
PY_BIN="$(find_lab_python "$APP_DIR")"
FOUND=$?
if [ "$FOUND" -ne 0 ]; then
  if [ "$FOUND" -eq 1 ]; then
    echo "Python 3.11+ was not found in PATH, Python.org Frameworks, or common Homebrew paths."
    echo "Install Python 3.13 from https://www.python.org/downloads/macos/ and try again."
    echo "For other installation paths, set LAB_PYTHON to the full interpreter path."
  fi
  if [ -t 0 ]; then read -r -p "Press Return to close this window..." _answer; fi
  exit 1
fi
"$PY_BIN" "$APP_DIR/scripts/launch.py" "$@"
RESULT=$?
if [ "$RESULT" -ne 0 ] && [ -t 0 ]; then
  read -r -p "Startup failed. Keep the error above; press Return to close..." _answer
fi
exit "$RESULT"
