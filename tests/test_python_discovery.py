"""Exercise Mac-style Python paths without a Mac or installed test interpreters."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
DISCOVERY = ROOT / "scripts" / "find-python.sh"
BASH = "/bin/bash"


class PythonDiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="shopping-python-discovery-")
        self.root = Path(self.temp.name)
        self.project = self.root / "\u9879\u76ee with spaces"
        self.framework = self.root / "Library with spaces" / "Python.framework" / "Versions"
        self.brew = self.root / "Homebrew with spaces"
        self.bin = self.root / "PATH with spaces"
        for path in (self.project, self.framework, self.brew, self.bin):
            path.mkdir(parents=True)
        self.log = self.root / "candidate-calls.log"

    def tearDown(self):
        self.temp.cleanup()

    def fake_python(self, path: Path, *, compatible=True) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        # The production finder checks a candidate via its -c exit status.
        # Shell fakes let us model old/broken interpreters without installations.
        path.write_text(
            '#!/bin/sh\n'
            'if [ -n "${PYTHON_DISCOVERY_LOG:-}" ]; then\n'
            '  printf "%s|%s\\n" "$0" "${1:-}" >> "$PYTHON_DISCOVERY_LOG"\n'
            'fi\n'
            'if [ "${1:-}" != "-c" ]; then exit 99; fi\n'
            f'exit {0 if compatible else 1}\n',
            encoding="utf-8",
        )
        path.chmod(0o755)
        return path

    def discover(self, explicit=None, *, brew_roots=None):
        env = dict(os.environ)
        env.pop("LAB_PYTHON", None)
        env.update(PATH=str(self.bin), PYTHON_DISCOVERY_LOG=str(self.log))
        if explicit is not None:
            env["LAB_PYTHON"] = str(explicit)
        roots = [self.brew] if brew_roots is None else brew_roots
        return subprocess.run(
            [BASH, "-c", 'source "$1"; shift; find_lab_python "$@"',
             "python-discovery-test", str(DISCOVERY), str(self.project),
             str(self.framework), *(str(path) for path in roots)],
            env=env, cwd="/tmp", capture_output=True, text=True, timeout=5,
        )

    def assert_selected(self, result, executable):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.strip(), str(executable))
        self.assertEqual(len(result.stdout.strip().splitlines()), 1)

    def test_explicit_absolute_path_with_spaces_takes_priority(self):
        selected = self.fake_python(self.root / "Chosen Python" / "python3.15")
        self.fake_python(self.bin / "python3.13")
        self.fake_python(self.project / ".venv" / "bin" / "python")
        self.assert_selected(self.discover(selected), selected)

    def test_explicit_command_name_resolves_on_path(self):
        selected = self.fake_python(self.bin / "python3.15")
        self.assert_selected(self.discover("python3.15"), selected)

    def test_explicit_incompatible_python_fails_without_fallback(self):
        explicit = self.fake_python(self.root / "old-python", compatible=False)
        self.fake_python(self.bin / "python3.13")
        result = self.discover(explicit)
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())
        self.assertNotIn("python3.13", self.log.read_text())

    def test_missing_explicit_python_fails_without_fallback(self):
        self.fake_python(self.bin / "python3.13")
        result = self.discover(self.root / "not installed")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertTrue(result.stderr.strip())

    def test_python314_on_path_is_found_after_old_python3(self):
        self.fake_python(self.bin / "python3", compatible=False)
        selected = self.fake_python(self.bin / "python3.14")
        self.assert_selected(self.discover(), selected)

    def test_python315_on_path_is_found(self):
        selected = self.fake_python(self.bin / "python3.15")
        self.assert_selected(self.discover(), selected)

    def test_pythonorg_framework_outside_path_with_spaces(self):
        selected = self.fake_python(self.framework / "3.15" / "bin" / "python3")
        self.assert_selected(self.discover(), selected)

    def test_framework_skips_incompatible_and_finds_versioned_binary(self):
        self.fake_python(self.framework / "3.9" / "bin" / "python3", compatible=False)
        selected = self.fake_python(self.framework / "3.14" / "bin" / "python3.14")
        self.assert_selected(self.discover(), selected)

    def test_homebrew_keg_only_versioned_binary_outside_path(self):
        selected = self.fake_python(self.brew / "opt" / "python@3.14" / "bin" / "python3.14")
        self.assert_selected(self.discover(), selected)

    def test_homebrew_keg_only_libexec_unversioned_binary(self):
        selected = self.fake_python(self.brew / "opt" / "python@3.15" / "libexec" / "bin" / "python3")
        self.assert_selected(self.discover(), selected)

    def test_homebrew_unversioned_binary(self):
        selected = self.fake_python(self.brew / "bin" / "python3")
        self.assert_selected(self.discover(), selected)

    def test_multiple_brew_roots_do_not_split_paths_with_spaces(self):
        alternate = self.root / "Alternate brew root"
        alternate.mkdir()
        selected = self.fake_python(alternate / "opt" / "python@3.12" / "bin" / "python3.12")
        self.assert_selected(self.discover(brew_roots=[self.brew, alternate]), selected)

    def test_project_venv_is_detected_outside_current_directory(self):
        selected = self.fake_python(self.project / ".venv" / "bin" / "python")
        self.assert_selected(self.discover(), selected)

    def test_no_compatible_interpreter_is_explicit_failure(self):
        self.fake_python(self.bin / "python3", compatible=False)
        result = self.discover()
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")

    def test_sourcing_is_silent_and_does_not_probe_candidates(self):
        env = dict(os.environ)
        env["PYTHON_DISCOVERY_LOG"] = str(self.log)
        result = subprocess.run([BASH, "-c", 'source "$1"', "discovery-source-only", str(DISCOVERY)],
                                env=env, cwd="/tmp", capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "")
        self.assertFalse(self.log.exists())


if __name__ == "__main__":
    unittest.main()
