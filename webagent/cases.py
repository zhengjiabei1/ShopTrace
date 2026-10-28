"""Development and heldout cases with separate product and grading data.

Expose only get_case_public, list_cases and get_catalog to the actor.
"""
from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
from pathlib import Path
import re
from urllib.parse import quote


DATASET_SCHEMA = "shoptrace-expanded-cases-v2-en-US"
DATASET_PATH = Path(__file__).resolve().parent / "case_assets" / "cases-v2-en-US.json"
DATASET_SHA256 = "952af659fa1006946351ae876dd65cf1f4de6aaafce42b0a9bdb60d34f20850f"
SPLITS = ("development", "heldout")
PUBLIC_CASE_FIELDS = ("id", "title", "query", "difficulty")
PUBLIC_PRODUCT_FIELDS = {"id", "name", "category", "price", "summary", "tags", "facts", "options"}


def _canonical_digest(value):
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


@lru_cache(maxsize=1)
def _dataset():
    raw = DATASET_PATH.read_bytes()
    if hashlib.sha256(raw).hexdigest() != DATASET_SHA256:
        raise ValueError("Expanded case dataset fingerprint differs from its versioned manifest")
    data = json.loads(raw)
    records = data.get("cases", [])
    if data.get("schema_version") != DATASET_SCHEMA or len(records) != 12:
        raise ValueError("Invalid expanded case dataset")
    case_ids, prod_ids = set(), set()
    for case in records:
        case_id = case.get("id")
        if not isinstance(case_id, str) or not re.fullmatch(r"case-[0-9a-f]{4}", case_id) or case_id in case_ids:
            raise ValueError("Expanded case IDs must be unique and opaque")
        case_ids.add(case_id)
        if case.get("split") not in SPLITS:
            raise ValueError("Invalid expanded case split")
        products = case.get("catalog", {}).get("products", [])
        if len(products) != 8:
            raise ValueError("Each expanded case requires eight synthetic products")
        for product in products:
            if set(product) != PUBLIC_PRODUCT_FIELDS or product.get("id") in prod_ids:
                raise ValueError("Invalid or duplicate public product record")
            prod_ids.add(product["id"])
            if not product["name"].startswith("Sample "):
                raise ValueError("Synthetic products must be labelled as examples")
    if any(sum(case["split"] == split for case in records) != 6 for split in SPLITS):
        raise ValueError("Both expanded case splits require six cases")
    return data


def _find_case(case_id):
    if not isinstance(case_id, str):
        raise ValueError("Unknown expanded case")
    for case in _dataset()["cases"]:
        if case["id"] == case_id:
            return case
    raise ValueError("Unknown expanded case")


def get_case_public(case_id: str) -> dict:
    """Return task text and URL without grading data."""
    case = _find_case(case_id)
    return {key: deepcopy(case[key]) for key in PUBLIC_CASE_FIELDS} | {
        "start_path": "/sandbox/?case=" + quote(case_id, safe="")}


def list_cases(split: str | None = None) -> list[dict]:
    """List public cases for a split."""
    if split is not None and split not in SPLITS:
        raise ValueError("Expanded split must be development or heldout")
    return [get_case_public(case["id"]) for case in _dataset()["cases"]
            if split is None or case["split"] == split]


def private_case(case_id: str) -> dict:
    """Return server-only grading data."""
    return deepcopy(_find_case(case_id))


def get_catalog(case_id: str) -> dict:
    """Return a copy of the product catalog."""
    return deepcopy(_find_case(case_id)["catalog"])


def catalog_fingerprint(case_id: str) -> str:
    """Hash the canonical product catalog."""
    return _canonical_digest(_find_case(case_id)["catalog"])


def dataset_fingerprint() -> str:
    """Validate the dataset and return its SHA-256."""
    _dataset()
    return DATASET_SHA256


def split_manifest() -> dict:
    """Return server-only dataset metadata."""
    return {"schema_version": DATASET_SCHEMA, "dataset_sha256": dataset_fingerprint(),
            "scope": "authored_synthetic_project_cases",
            "splits": {split: [{"case_id": case["id"], "catalog_sha256": catalog_fingerprint(case["id"]),
                                "product_count": len(case["catalog"]["products"])}
                               for case in _dataset()["cases"] if case["split"] == split]
                       for split in SPLITS}}
