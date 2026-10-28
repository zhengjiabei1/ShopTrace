#!/usr/bin/env python3
"""Fetch the pinned upstream source into a new directory."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]


def command(args: list[str], cwd: Path | None = None) -> str:
    completed = subprocess.run(args, cwd=cwd, text=True, capture_output=True, timeout=300)
    if completed.returncode:
        # Use the public URL from the lock file.
        raise RuntimeError(completed.stderr.strip() or f"Command failed: {args[0]}")
    return completed.stdout.strip()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=ROOT / "external" / "shopping-grpo")
    parser.add_argument("--with-data", action="store_true", help="Also checkout upstream datasets and binary assets; default is source-only")
    args = parser.parse_args()
    dest = args.destination.resolve()
    if dest.exists():
        parser.error(f"Refusing to overwrite existing path: {dest}")
    lock = json.loads((ROOT / "upstream.lock.json").read_text(encoding="utf-8"))
    source = lock["reproduction"]
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.mkdir()
    try:
        command(["git", "init", str(dest)])
        command(["git", "remote", "add", "origin", source["repository"] + ".git"], dest)
        command(["git", "config", "remote.origin.promisor", "true"], dest)
        command(["git", "config", "remote.origin.partialclonefilter", "blob:none"], dest)
        if not args.with_data:
            command(["git", "config", "core.sparseCheckout", "true"], dest)
            sparse = dest / ".git" / "info" / "sparse-checkout"
            sparse.parent.mkdir(parents=True, exist_ok=True)
            sparse.write_text(
                "/*\n!/data/\n!/docs/images/\n"
                "!/environments/ShopSimulator/shop_env/data/*.gz\n"
                "!/environments/ShopSimulator/shop_env/data/*.zip\n"
                "!/environments/ShopSimulator/shop_env/data/*.json\n",
                encoding="utf-8",
            )
        command(["git", "fetch", "--depth", "1", "--filter=blob:none", "origin", source["commit"]], dest)
        command(["git", "checkout", "--detach", source["commit"]], dest)
        actual = command(["git", "rev-parse", "HEAD"], dest)
        if actual != source["commit"]:
            raise RuntimeError("Pinned commit verification failed")
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
        print(f"Fetch failed: {exc}\nPartial checkout retained at {dest}; no installation was attempted.", file=sys.stderr)
        return 1
    print(json.dumps({"path": str(dest), "commit": actual, "data_included": args.with_data,
                      "installed": False, "upstream_license": "unresolved; check before redistribution"}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
