#!/bin/bash
# Python discovery only: no installation, PATH changes or system modifications.

_lab_try_python() {
  local resolved
  resolved="$(command -v "$1" 2>/dev/null)" || return 1
  if "$resolved" -c 'import sys; raise SystemExit(sys.version_info < (3, 11))' >/dev/null 2>&1; then
    printf '%s\n' "$resolved"
    return 0
  fi
  return 1
}

find_lab_python() (
  # The optional roots make platform layouts testable without a Mac or writes
  # to /Library. This subshell does not alter the caller's shell options.
  local project_dir framework_root candidate root
  project_dir="$1"
  framework_root="${2:-/Library/Frameworks/Python.framework/Versions}"
  local brew_roots
  if [ "$#" -ge 3 ]; then
    shift 2
    brew_roots=("$@")
  else
    brew_roots=(/opt/homebrew /usr/local)
  fi

  if [ -n "${LAB_PYTHON:-}" ]; then
    if _lab_try_python "$LAB_PYTHON"; then return 0; fi
    printf '%s\n' 'LAB_PYTHON does not point to a usable Python 3.11+ interpreter. Check the path.' >&2
    return 2
  fi

  for candidate in "$project_dir/.venv/bin/python" python3.13 python3.12 python3.11 python3.14 python3.15 python3; do
    if _lab_try_python "$candidate"; then return 0; fi
  done

  # Finder-launched Terminal can have a minimal PATH. Probe python.org's
  # framework installation directly, including versioned-only executables.
  shopt -s nullglob
  for candidate in "$framework_root"/3.*/bin/python3 "$framework_root"/3.*/bin/python3.*; do
    if _lab_try_python "$candidate"; then return 0; fi
  done
  for root in "${brew_roots[@]}"; do
    for candidate in "$root"/bin/python3 "$root"/bin/python3.* "$root"/opt/python@3.*/bin/python3.* "$root"/opt/python@3.*/libexec/bin/python3; do
      if _lab_try_python "$candidate"; then return 0; fi
    done
  done
  return 1
)
