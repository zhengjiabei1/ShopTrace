#!/usr/bin/env python3
"""Build the browser-only synthetic preview without model credentials or data."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("styles.css", "app.js", "favicon.svg", "preview-runtime.js")


def build(destination: Path):
    destination.mkdir(parents=True, exist_ok=True)
    static = destination / "static"
    static.mkdir(exist_ok=True)
    for name in ASSETS:
        shutil.copyfile(ROOT / "frontend" / name, static / name)
    html = (ROOT / "frontend" / "index.html").read_text(encoding="utf-8")
    html = html.replace('<html lang="en">', '<html lang="en" data-preview="true">', 1)
    assert 'data-preview="true"' in html
    (destination / "index.html").write_text(html, encoding="utf-8")
    return {"destination": str(destination.resolve()), "mode": "browser_synthetic_preview",
            "model_calls": 0, "backend_required": False, "files": ["index.html", *[f"static/{name}" for name in ASSETS]]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.destination), ensure_ascii=False, indent=2))
