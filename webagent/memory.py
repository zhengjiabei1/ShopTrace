"""Bounded excerpts from pages the browser has observed."""
from __future__ import annotations

from collections import OrderedDict
from copy import deepcopy
import hashlib
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


def page_key(url):
    parts = urlsplit(url)
    query = [(k, v) for k, v in parse_qsl(parts.query) if k not in {"sid", "session"}]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def no_refs(value):
    if isinstance(value, dict):
        return {k: no_refs(v) for k, v in value.items() if k != "ref"}
    if isinstance(value, list):
        return [no_refs(v) for v in value]
    if isinstance(value, str):
        return re.sub(r"\bobs-\d+-(?:e)?\d+\b", "[old reference]", value)
    return value


class PageMemory:
    def __init__(self, max_pages=32, max_chars=24000, page_chars=1800):
        if min(max_pages, max_chars, page_chars) < 1:
            raise ValueError("Memory limits must be positive")
        self.max_pages = max_pages
        self.max_chars = max_chars
        self.page_chars = min(page_chars, max_chars)
        self.pages = OrderedDict()
        self.evicted = self.clipped = 0

    def observe(self, obs, step):
        url = page_key(obs.get("url", ""))
        if obs.get("loading"):
            return {"step": step, "observation_id": obs.get("observation_id"), "url": url,
                    "changed": False, "loading": True, **self.stats(),
                    "source_truncated": bool(obs.get("truncated"))}
        text = no_refs(str(obs.get("text", "")))
        digest = hashlib.sha256(text.encode()).hexdigest()
        old = self.pages.get(url)
        changed = old is None or old["text_hash"] != digest
        clipped = len(text) > self.page_chars
        if changed:
            self.clipped += int(clipped)
            self.pages[url] = {"url": url, "title": str(obs.get("title", ""))[:160],
                "text": text[:self.page_chars], "text_hash": digest,
                "observation_id": obs.get("observation_id"), "step": step,
                "clipped": clipped, "source_truncated": bool(obs.get("truncated"))}
        self.pages.move_to_end(url)
        while len(self.pages) > self.max_pages or self.characters > self.max_chars:
            self.pages.popitem(last=False)
            self.evicted += 1
        return {"step": step, "observation_id": obs.get("observation_id"), "url": url,
                "changed": changed, "loading": False, **self.stats(), "source_truncated": bool(obs.get("truncated"))}

    @property
    def characters(self):
        return sum(len(row["text"]) for row in self.pages.values())

    def stats(self):
        return {"pages": len(self.pages), "characters": self.characters,
                "evicted_pages": self.evicted, "clipped_pages": self.clipped,
                "max_pages": self.max_pages, "max_characters": self.max_chars,
                "max_page_characters": self.page_chars}

    def context(self, url):
        current = page_key(url)
        return {"policy": "Observed page excerpts only; old facts can change. Reopen pages to obtain current controls and stock.",
                "limits": self.stats(), "pages": [deepcopy(row) for key, row in self.pages.items() if key != current]}
