#!/usr/bin/env python3
"""Package the separate, browser-only ShopTrace participation experience."""
import argparse
import json
from pathlib import Path
import shutil

ROOT = Path(__file__).resolve().parents[1]
ASSETS = ("index.html", "styles.css", "app.js", "favicon.svg", "scenarios.js", "feedback.js")


def build(destination: Path):
    source = ROOT / "experience"
    missing = [name for name in ASSETS if not (source / name).is_file()]
    if missing:
        raise SystemExit("Missing experience assets: " + ", ".join(missing))
    destination = destination.resolve()
    if destination == source.resolve() or source.resolve() in destination.parents:
        raise SystemExit("Choose a build destination outside the experience source directory.")
    destination.mkdir(parents=True, exist_ok=True)
    for name in ASSETS:
        shutil.copyfile(source / name, destination / name)
    return {"destination": str(destination), "mode": "synthetic_participation_demo",
            "model_calls": 0, "backend_required": False, "feedback_storage": "browser_local_only",
            "files": list(ASSETS)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.destination), ensure_ascii=False, indent=2))
