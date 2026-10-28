"""Launcher readiness must identify this app and preserve existing environments."""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch


_spec = importlib.util.spec_from_file_location("lab_launcher", Path(__file__).resolve().parents[1] / "scripts" / "launch.py")
launcher = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launcher)


class LauncherTests(unittest.TestCase):
    def test_other_service_does_not_open_browser_or_install(self):
        with patch.object(launcher, "inspect_service", return_value="occupied"), patch.object(launcher, "show_browser") as browser, patch.object(launcher, "environment_python") as install:
            self.assertEqual(launcher.main([]), 1)
        browser.assert_not_called()
        install.assert_not_called()

    def test_existing_workbench_reused_without_starting_another(self):
        with patch.object(launcher, "inspect_service", return_value="workbench"), patch.object(launcher, "show_browser") as browser, patch.object(launcher, "environment_python") as install:
            self.assertEqual(launcher.main(["--no-browser"]), 0)
        browser.assert_called_once_with("http://127.0.0.1:8765", False)
        install.assert_not_called()

    def test_broken_venv_not_overwritten(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(launcher, "ROOT", Path(directory)), patch.object(launcher.subprocess, "run") as run:
            (Path(directory) / ".venv").mkdir()
            with self.assertRaisesRegex(RuntimeError, "left unchanged"):
                launcher.environment_python()
        run.assert_not_called()

    def test_status_identity_required(self):
        response = MagicMock(status=200)
        connection = MagicMock()
        connection.getresponse.return_value = response
        with patch.object(launcher.socket, "create_connection"), patch.object(launcher.http.client, "HTTPConnection", return_value=connection):
            response.read.return_value = json.dumps({"version": "1", "capabilities": {"fixture": True}}).encode()
            self.assertEqual(launcher.inspect_service(8765), "occupied")
            response.read.return_value = json.dumps({"application": "shopping-agent-lab", "version": "0.1", "capabilities": {"fixture": True}}).encode()
            self.assertEqual(launcher.inspect_service(8765), "workbench")


if __name__ == "__main__":
    unittest.main()
