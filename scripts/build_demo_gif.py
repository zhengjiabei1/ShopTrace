#!/usr/bin/env python3
"""Build a short GIF from one saved portal attempt.

This is a documentation helper, not part of the runtime. It reads the same
WebP screenshots that the static report uses, so the GIF always matches the
committed evidence.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

try:
    from PIL import Image
except ImportError:  # pragma: no cover - exercised by the CLI
    Image = None


def build(root: Path, output: Path, *, suite: str, attempt_id: str, frames: int = 10,
          width: int = 720, duration_ms: int = 1200) -> dict:
    if Image is None:
        raise RuntimeError("Pillow is required: python -m pip install pillow")
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    suite_row = next((item for item in report.get("suites", []) if item.get("id") == suite), None)
    if suite_row is None:
        raise ValueError(f"Unknown suite: {suite}")
    attempt = next((item for item in suite_row.get("attempts", []) if item.get("id") == attempt_id), None)
    if attempt is None:
        raise ValueError(f"Unknown attempt: {attempt_id}")
    shots = [step["screenshot"] for step in attempt.get("steps", []) if step.get("screenshot")]
    if not shots:
        raise ValueError("Attempt has no screenshots")
    if frames < 2:
        raise ValueError("At least two frames are required")
    if len(shots) > frames:
        indexes = [round(i * (len(shots) - 1) / (frames - 1)) for i in range(frames)]
        shots = [shots[i] for i in indexes]

    images = []
    for name in shots:
        with Image.open(root / name) as source:
            image = source.convert("RGB")
            height = round(image.height * width / image.width)
            images.append(image.resize((width, height), Image.LANCZOS))
    output.parent.mkdir(parents=True, exist_ok=True)
    images[0].save(output, save_all=True, append_images=images[1:], duration=duration_ms,
                   loop=0, optimize=True, disposal=2)
    return {"output": str(output), "frames": len(images), "bytes": output.stat().st_size,
            "suite": suite, "attempt": attempt_id, "task": attempt.get("task_id")}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1] / "portal" / "dist")
    parser.add_argument("--output", type=Path,
                        default=Path(__file__).resolve().parents[1] / "docs" / "assets" / "shoptrace-replay.gif")
    parser.add_argument("--suite", default="procurement-memory")
    parser.add_argument("--attempt", default="427529da2acc42d6869b1164c01ffe04")
    parser.add_argument("--frames", type=int, default=10)
    parser.add_argument("--width", type=int, default=720)
    args = parser.parse_args(argv)
    try:
        result = build(args.root, args.output, suite=args.suite, attempt_id=args.attempt,
                       frames=args.frames, width=args.width)
    except (OSError, ValueError, RuntimeError) as exc:
        print(f"GIF build stopped: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
