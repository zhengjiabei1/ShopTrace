"""English copy checks for active source and generated previews."""
from pathlib import Path
import re
import tempfile
import unittest

from scripts.build_preview import build


ROOT = Path(__file__).resolve().parents[1]
HAN = re.compile(r"[\u3400-\u9fff]")


class EnglishTests(unittest.TestCase):
    def test_active_source_and_docs_use_english(self):
        roots = ("lab", "webagent", "scripts", "frontend", "experience", "portal")
        suffixes = {".py", ".js", ".html", ".css", ".svg", ".json", ".md", ".sh"}
        skip_dirs = {"work", "node_modules", "__pycache__", ".git", "evidence"}
        raw_files = {"report.json", "report-data.js"}
        files = [ROOT / "README.md", ROOT / "Launch ShopTrace.command"]
        files += [p for p in (ROOT / "docs").glob("*.md") if not p.name.endswith(".zh-CN.md")]
        for name in roots:
            files += [p for p in (ROOT / name).rglob("*")
                      if p.is_file() and p.suffix in suffixes
                      and not set(p.relative_to(ROOT).parts) & skip_dirs
                      and p.name not in raw_files]
        bad = [str(p.relative_to(ROOT)) for p in files if HAN.search(p.read_text(encoding="utf-8"))]
        self.assertEqual(bad, [], "Untranslated active files")

    def test_preview_builder_keeps_english_language_and_preview_mode(self):
        with tempfile.TemporaryDirectory() as folder:
            dest = Path(folder)
            build(dest)
            html = (dest / "index.html").read_text(encoding="utf-8")
            self.assertIn('<html lang="en" data-preview="true">', html)
            self.assertIsNone(HAN.search(html))
        for name in ("webagent/frontend", "webagent/sandbox_assets", "portal/dist", "experience"):
            html = (ROOT / name / "index.html").read_text(encoding="utf-8")
            self.assertRegex(html, r'<html\s+lang="en(?:-US)?"')


if __name__ == "__main__":
    unittest.main()
