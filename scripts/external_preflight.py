#!/usr/bin/env python3
"""Check whether the read-only external sites load in the project browser.

No model is called. This is a site-reachability and page-shape preflight:
it visits each origin from the WebVoyager slice with the same single-tab,
origin-scoped browser used by the agent, then records the title, visible
text length, interactive element count, forms, blocked requests, and a
screenshot.

Run:
    python scripts/external_preflight.py
    python scripts/external_preflight.py --site github.com
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
from pathlib import Path
import re
import shutil
import sys
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
DEFAULT_MANIFEST = ROOT / "benchmarks" / "external" / "webvoyager-readonly-30.json"
DEFAULT_OUTPUT = ROOT / "work" / "external-preflight"
CHROME = os.environ.get("WEBAGENT_CHROMIUM_PATH") or "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
BLOCK_WORDS = (
    "captcha", "verify you are human", "unusual traffic", "access denied",
    "enable javascript", "cookie", "consent", "accept all", "before you continue",
    "sign in to continue", "log in to continue",
)
CONTENT_BLOCK_WORDS = (
    "captcha", "verify you are human", "unusual traffic", "access denied",
    "access issue", "enable javascript",
)


def _slug(origin: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", urlsplit(origin).netloc.casefold()).strip("-")


def _origin(url: str) -> str:
    parsed = urlsplit(url)
    return f"{parsed.scheme}://{parsed.netloc}"


async def check_site(origin: str, output: Path) -> dict:
    from webagent.browser import BrowserSession

    site_dir = output / _slug(origin)
    if site_dir.exists():
        shutil.rmtree(site_dir)
    session = BrowserSession(origin + "/", site_dir, allowed_origins=[origin],
                             executable_path=CHROME if Path(CHROME).is_file() else None,
                             headless=True, read_only=True)
    row = {"origin": origin, "slug": _slug(origin), "ok": False}
    try:
        obs = await session.start()
        text = str(obs.get("text") or "")
        lowered = text.casefold()
        forms = await session.page.evaluate("""() => [...document.forms].slice(0, 10).map(form => ({
            method: (form.method || 'get').toUpperCase(),
            action: form.action || '',
            inputs: [...form.elements].filter(node => node.name).slice(0, 6).map(node => node.name)
        }))""")
        content_blocked = any(word in lowered for word in CONTENT_BLOCK_WORDS)
        degraded = int(obs.get("blocked_requests") or 0) >= 50
        verdict = "content-blocked" if content_blocked else "degraded" if degraded else "ok"
        row.update({
            "ok": True,
            "title": obs.get("title"),
            "url": obs.get("url"),
            "text_characters": obs.get("text_characters"),
            "text_excerpt": " ".join(text.split())[:240],
            "interactive_elements": obs.get("total_interactive_elements"),
            "blocked_requests": obs.get("blocked_requests"),
            "loading": obs.get("loading"),
            "truncated": obs.get("truncated"),
            "forms": forms,
            "signals": [word for word in BLOCK_WORDS if word in lowered],
            "verdict": verdict,
            "screenshot": obs.get("screenshot"),
            "screenshot_path": str(site_dir / obs["screenshot"]) if obs.get("screenshot") else None,
        })
    except Exception as exc:  # noqa: BLE001 - report the environment failure
        row["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await session.close()
        except Exception:
            pass
    return row


async def run(sites: list[str], output: Path) -> list[dict]:
    output.mkdir(parents=True, exist_ok=True)
    rows = []
    for origin in sites:
        print(f"CHECK {origin}", flush=True)
        row = await check_site(origin, output)
        rows.append(row)
        if row.get("ok"):
            print(f"  {row.get('verdict')} | {row.get('title')} | {row.get('text_characters')} chars | "
                  f"{row.get('interactive_elements')} elements | blocked={row.get('blocked_requests')} | "
                  f"signals={row.get('signals')}", flush=True)
        else:
            print(f"  FAILED: {row.get('error')}", flush=True)
    (output / "preflight.json").write_text(json.dumps({"sites": rows}, ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    return rows


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--site", help="Only check one origin or hostname")
    args = parser.parse_args(argv)
    try:
        data = json.loads(args.manifest.read_text(encoding="utf-8"))
        origins = []
        for task in data["tasks"]:
            for value in [task["start_url"], *task.get("extra_origins", [])]:
                origin = _origin(value)
                if origin not in origins:
                    origins.append(origin)
        if args.site:
            needle = args.site.casefold()
            origins = [origin for origin in origins if needle in origin.casefold()]
        if not origins:
            raise ValueError("No sites selected")
        rows = asyncio.run(run(origins, args.output))
    except (OSError, ValueError, KeyError) as exc:
        print(f"Preflight stopped: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    failed = [row for row in rows if not row.get("ok")]
    verdicts = {}
    for row in rows:
        key = row.get("verdict", "failed")
        verdicts[key] = verdicts.get(key, 0) + 1
    print(json.dumps({"sites": len(rows), "loaded": len(rows) - len(failed), "failed": len(failed),
                      "verdicts": verdicts, "output": str(args.output)}, indent=2))
    return 0 if not failed else 2


if __name__ == "__main__":
    raise SystemExit(main())
