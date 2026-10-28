#!/usr/bin/env python3
"""Compress saved portal screenshots to WebP and update the report index.

The portal is a static replay viewer. Full-resolution PNG evidence is useful
while a batch is being inspected, but it makes the repository expensive to
clone. This script keeps every observation, resizes it to a readable width,
and rewrites the saved report so the same replay works with smaller files.

Requires Pillow (``python -m pip install pillow``). The runtime itself does not
need Pillow; this is an offline maintenance script.
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
from pathlib import Path
import re
import shutil
import sys

try:
    from PIL import Image
except ImportError:  # pragma: no cover - exercised by the CLI
    Image = None

SCREENSHOT_NAME = re.compile(r'((?:obs|step)-\d{4,6})\.png')


def _image_bytes(path: Path, max_width: int, quality: int) -> bytes:
    with Image.open(path) as source:
        image = source.convert("RGB")
        if image.width > max_width:
            height = round(image.height * max_width / image.width)
            image = image.resize((max_width, height), Image.LANCZOS)
        buffer = io.BytesIO()
        image.save(buffer, "WEBP", quality=quality, method=6)
        return buffer.getvalue()


def _rewrite_report(root: Path) -> dict:
    report_path = root / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    changed = 0
    for suite in report.get("suites", []):
        for attempt in suite.get("attempts", []):
            for step in attempt.get("steps", []):
                shot = step.get("screenshot")
                if isinstance(shot, str) and shot.endswith(".png"):
                    step["screenshot"] = shot[:-4] + ".webp"
                    changed += 1
    if changed:
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        encoded = (json.dumps(report, ensure_ascii=False)
                   .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
        (root / "report-data.js").write_text("window.SHOPTRACE_REPORT = " + encoded + ";\n", encoding="utf-8")
    return report


def _rewrite_evidence_json(root: Path) -> int:
    changed = 0
    for path in (root / "evidence").rglob("*.json"):
        text = path.read_text(encoding="utf-8")
        updated = SCREENSHOT_NAME.sub(r"\1.webp", text)
        if updated != text:
            path.write_text(updated, encoding="utf-8")
            changed += 1
    return changed


def _gzip_trajectories(root: Path) -> int:
    """Compress raw traces; the report already contains the replay data."""
    changed = 0
    for path in sorted((root / "evidence").rglob("trajectory.json")):
        target = path.with_suffix(".json.gz")
        target.write_bytes(gzip.compress(path.read_bytes(), 9))
        path.unlink()
        changed += 1
    if changed:
        report_path = root / "report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        for suite in report.get("suites", []):
            for attempt in suite.get("attempts", []):
                url = attempt.get("trajectory_url")
                if isinstance(url, str) and url.endswith("trajectory.json"):
                    attempt["trajectory_url"] = url + ".gz"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        encoded = (json.dumps(report, ensure_ascii=False)
                   .replace("<", "\\u003c").replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))
        (root / "report-data.js").write_text("window.SHOPTRACE_REPORT = " + encoded + ";\n", encoding="utf-8")
    return changed


def _manifest(root: Path) -> None:
    files = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and path.name != "manifest.json":
            raw = path.read_bytes()
            files.append({"path": path.relative_to(root).as_posix(), "bytes": len(raw),
                          "sha256": hashlib.sha256(raw).hexdigest()})
    (root / "manifest.json").write_text(
        json.dumps({"schema_version": "shoptrace.portal.manifest.v1", "files": files}, indent=2) + "\n",
        encoding="utf-8")


def optimize(root: Path, *, max_width: int = 960, quality: int = 72, archive: Path | None = None,
             gzip_trajectories: bool = False) -> dict:
    if Image is None:
        raise RuntimeError("Pillow is required: python -m pip install pillow")
    root = root.resolve()
    evidence = root / "evidence"
    if not evidence.is_dir():
        raise ValueError(f"No evidence directory under {root}")
    if archive is not None:
        archive = archive.resolve()
        if archive.exists():
            raise ValueError(f"Archive destination already exists: {archive}")
        shutil.copytree(evidence, archive)

    before = after = 0
    converted = 0
    for path in sorted(evidence.rglob("*.png")):
        target = path.with_suffix(".webp")
        raw = _image_bytes(path, max_width, quality)
        target.write_bytes(raw)
        before += path.stat().st_size
        after += len(raw)
        path.unlink()
        converted += 1

    report = _rewrite_report(root)
    changed_json = _rewrite_evidence_json(root)
    compressed = _gzip_trajectories(root) if gzip_trajectories else 0
    _manifest(root)
    return {"converted": converted, "before_bytes": before, "after_bytes": after,
            "report_steps_updated": sum(1 for suite in report.get("suites", [])
                                        for attempt in suite.get("attempts", [])
                                        for step in attempt.get("steps", [])
                                        if isinstance(step.get("screenshot"), str) and step["screenshot"].endswith(".webp")),
            "evidence_json_updated": changed_json, "trajectories_gzipped": compressed, "root": str(root)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "portal" / "dist")
    parser.add_argument("--max-width", type=int, default=960)
    parser.add_argument("--quality", type=int, default=72)
    parser.add_argument("--archive", type=Path, help="Copy the original PNG evidence here before converting.")
    parser.add_argument("--gzip-trajectories", action="store_true",
                        help="Gzip raw trajectory JSON; the report keeps the replay data.")
    args = parser.parse_args(argv)
    try:
        result = optimize(args.root, max_width=args.max_width, quality=args.quality, archive=args.archive,
                          gzip_trajectories=args.gzip_trajectories)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"Portal optimization stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    saved = result["before_bytes"] - result["after_bytes"]
    print(json.dumps({**result, "saved_bytes": saved,
                      "saved_megabytes": round(saved / 1024 / 1024, 2)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
