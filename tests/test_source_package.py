"""A deploy-ready source bundle must never include runtime credentials."""
import contextlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile


class SourcePackageTests(unittest.TestCase):
    def test_only_deployment_templates_are_bundled_and_symlinks_are_excluded(self):
        path = Path(__file__).resolve().parents[1] / "scripts" / "package.py"
        spec = importlib.util.spec_from_file_location("source_package_under_test", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "source"
            (root / "deploy").mkdir(parents=True)
            (root / "README.md").write_text("example source")
            (root / "deploy" / "Dockerfile").write_text("FROM example")
            (root / "deploy" / "runtime.env.example").write_text("LLM_API_KEY=configure-in-platform")
            (root / "deploy" / "runtime.env").write_text("fake-sensitive-runtime-data")
            (root / "deploy" / "proxy.pem").write_text("fake-private-certificate-data")
            (root / "deploy" / "unexpected.yaml").write_text("fake-secret-in-unapproved-file")
            (root / "deploy" / "compose.yaml").symlink_to(root / "deploy" / "runtime.env")
            (root / "portal" / "work").mkdir(parents=True)
            (root / "portal" / "work" / "scratch.txt").write_text("fake-sensitive-scratch-data")
            output = Path(directory) / "source.zip"
            with patch.object(module, "ROOT", root), patch("sys.argv", ["package.py", str(output)]), contextlib.redirect_stdout(io.StringIO()):
                module.main()
            with zipfile.ZipFile(output) as archive:
                names = archive.namelist()
                self.assertIn("shopping-agent-lab/deploy/Dockerfile", names)
                self.assertIn("shopping-agent-lab/deploy/runtime.env.example", names)
                self.assertFalse(any(n.endswith("runtime.env") or n.endswith(".pem") or n.endswith("unexpected.yaml") or n.endswith("compose.yaml") for n in names))
                self.assertNotIn(b"fake-sensitive-runtime-data", b"".join(archive.read(n) for n in names))
                self.assertFalse(any("/work/" in n for n in names))
            with patch.object(module, "ROOT", root), patch("sys.argv", ["package.py", str(output)]), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    module.main()


if __name__ == "__main__":
    unittest.main()
