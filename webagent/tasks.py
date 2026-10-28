"""Browser tasks and DOM grading. Criteria remain server-side."""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import itertools
import json
from pathlib import Path
from urllib.parse import urlsplit

BENCHMARK = "shoptrace-browser-sandbox-v1"
CATALOG_PATH = Path(__file__).resolve().parent / "sandbox_assets" / "catalog.json"

_TASKS = [{'id': 'browser-headphones-commute',
  'title': 'Find commuting headphones',
  'difficulty': 'medium',
  'query': 'Find black wireless headphones costing no more than CNY 300. Check their details, add them to '
           'the shopping list, then open the list and keep only that item.',
  'goal': {'category': 'headphones', 'max_price': 300, 'color': 'Black', 'wireless': True}},
 {'id': 'browser-thermos-variant',
  'title': 'Check the thermos variant and final price',
  'difficulty': 'medium',
  'query': 'Find a blue 500 mL thermos with a final selected-variant price of no more than CNY 100. Select '
           'the correct variant, add it to the shopping list, then open the list and keep only that item.',
  'goal': {'category': 'thermos', 'max_price': 100, 'color': 'Blue', 'capacity_ml': 500}},
 {'id': 'browser-powerbank-spec',
  'title': 'Compare power bank output specifications',
  'difficulty': 'medium',
  'query': 'Find a black 10,000 mAh power bank with USB-C, a maximum output of at least 20 W, and a price no '
           'higher than CNY 150. Add it to the shopping list, then open the list and keep only that item.',
  'goal': {'category': 'powerbank',
           'max_price': 150,
           'color': 'Black',
           'capacity_mah': 10000,
           'port': 'USB-C',
           'min_output_w': 20}},
 {'id': 'browser-lamp-adjustable',
  'title': 'Check the reading lamp features',
  'difficulty': 'easy',
  'query': 'Find a dimmable USB-powered lamp with warm light costing no more than CNY 150. Check its '
           'details, add it to the shopping list, then open the list and keep only that item.',
  'goal': {'category': 'lamp',
           'max_price': 150,
           'dimmable': True,
           'light': 'Warm light',
           'power_supply': 'USB'}},
 {'id': 'browser-thermos-budget',
  'title': 'Stop when no product meets the budget',
  'difficulty': 'hard',
  'kind': 'none',
  'query': 'Filter the Thermos category with a maximum price of CNY 30 to look for a red 500 mL thermos. If '
           'no product matches, stay on the filtered results page, keep the shopping list empty, and report '
           'that no suitable product is available. Do not relax the requirements.',
  'goal': {'category': 'thermos', 'max_price': 30, 'color': 'Red', 'capacity_ml': 500}},
 {'id': 'browser-headphones-unknown',
  'title': 'Keep missing product information uncertain',
  'difficulty': 'hard',
  'kind': 'unknown',
  'query': 'Check whether gray headphones costing no more than CNY 280 support wireless connectivity. If the '
           'product details do not provide connection information, stay on the details page and report the '
           'uncertainty. Do not guess or add the item to the shopping list.',
  'goal': {'category': 'headphones', 'max_price': 280, 'color': 'Gray', 'wireless': True},
  'unknown_field': 'wireless'}]


def list_tasks() -> list[dict]:
    return [get_task(task["id"]) for task in _TASKS]


def get_task(task_id: str) -> dict:
    if task_id.startswith("procure-"):
        from .procure import get_task as procure
        return procure(task_id)
    if not any(task["id"] == task_id for task in _TASKS):
        from .cases import get_case_public
        return get_case_public(task_id)
    task = _private_task(task_id)
    return {key: task[key] for key in ("id", "title", "query", "difficulty")} | {"start_path": "/sandbox/"}


def _private_task(task_id):
    for task in _TASKS:
        if task["id"] == task_id:
            return deepcopy(task)
    raise ValueError("Unknown browser task")


def _catalog(catalog=None):
    return catalog if catalog is not None else json.loads(CATALOG_PATH.read_text(encoding="utf-8"))["products"]


def _variant(product, selections=None):
    selections = selections or {}
    facts, price = deepcopy(product["facts"]), Decimal(str(product["price"]))
    for axis in product["options"]:
        selected = selections.get(axis["id"], axis["values"][0]["value"])
        option = next((value for value in axis["values"] if value["value"] == selected), None)
        if option is None:
            raise ValueError("Variant is not in the displayed catalog")
        price += Decimal(str(option.get("price_delta", 0)))
        if axis["id"] == "color":
            facts["color"] = option["value"]
        elif axis["id"] == "capacity":
            facts["capacity_ml"] = int(option["value"].removesuffix("ml"))
    return {"product_id": product["id"], "category": product["category"], "price": price, **facts}


def _variants(product):
    axes = product["options"]
    choices = itertools.product(*(axis["values"] for axis in axes))
    return [_variant(product, {axis["id"]: choice["value"] for axis, choice in zip(axes, combo)})
            for combo in choices]


def _matches(facts, goal):
    for key, required in goal.items():
        if key == "max_price":
            if facts["price"] > Decimal(str(required)):
                return False
        elif key == "min_output_w":
            value = facts.get("max_output_w")
            if not isinstance(value, (int, float)) or value < required:
                return False
        elif facts.get(key) is None or facts.get(key) != required:
            return False
    return True


def _matching_variants(goal, catalog=None):
    return [variant for product in _catalog(catalog) for variant in _variants(product) if _matches(variant, goal)]


def _observed_item(row, catalog=None):
    product = next((p for p in _catalog(catalog) if p["id"] == row.get("product_id")), None)
    if product is None:
        raise ValueError("Cart product is not in this sandbox catalog")
    selections = {}
    for axis in product["options"]:
        field = {"color": "color", "capacity": "capacity"}.get(axis["id"])
        selected = row.get(field) if field else None
        if selected is None or not any(value["value"] == selected for value in axis["values"]):
            raise ValueError("Selected variant has no matching visible cart state")
        selections[axis["id"]] = selected
    variant = _variant(product, selections)
    try:
        shown_cost = Decimal(str(row["price"]))
    except (InvalidOperation, KeyError) as exc:
        raise ValueError("Cart price is unavailable") from exc
    if not shown_cost.is_finite() or shown_cost != variant["price"]:
        raise ValueError("Displayed cart price differs from selected variant")
    if row.get("color") != str(variant.get("color", "")):
        raise ValueError("Displayed cart color differs from selected variant")
    cap_text = f'{variant["capacity_ml"]}ml' if variant.get("capacity_ml") else ""
    if row.get("capacity") != cap_text:
        raise ValueError("Displayed cart capacity differs from selected variant")
    return variant


_DOM_SNAPSHOT = """() => {
  const visible = node => !!node && node.getClientRects().length > 0;
  const cart = [...document.querySelectorAll('.cart-item')].filter(visible).map(node => ({
    product_id: node.dataset.productId, color: node.dataset.variantColor,
    capacity: node.dataset.variantCapacity, price: node.dataset.unitPrice,
    text: node.innerText.slice(0, 2500)
  }));
  const detail = document.getElementById('product-details');
  const results = document.getElementById('results');
  return {url: location.href, cart_count: Number(document.getElementById('cart-count')?.innerText || 0),
    cart, detail: visible(detail) ? {product_id: detail.dataset.productId, text: detail.innerText.slice(0, 5000)} : null,
    results: visible(results) ? {total: Number(results.dataset.totalResults), query: results.dataset.searchQuery,
      category: results.dataset.category, max_price: results.dataset.maxPrice, text: results.innerText.slice(0, 2000)} : null};
}"""


def _grade_snapshot(task, state, finish, *, catalog=None):
    evidence = []
    response = {"benchmark": BENCHMARK, "success": False, "reason": "The task is not complete.", "evidence": evidence,
                "scope": "Checked against the local catalog and current page."}
    reason = finish.get("reason") if isinstance(finish, dict) else None
    if reason not in {"completed", "no_suitable_product", "blocked"}:
        response["reason"] = "The finish reason is not recognized."
        return response
    path = urlsplit(state.get("url", "")).path
    if task.get("kind") == "unknown":
        known = {k: v for k, v in task["goal"].items() if k != task["unknown_field"]}
        candidates = {v["product_id"] for p in _catalog(catalog) for v in _variants(p)
                      if _matches(v, known) and v.get(task["unknown_field"]) is None}
        detail = state.get("detail")
        if (reason == "blocked" and state.get("cart_count") == 0 and path == "/sandbox/product"
                and detail and detail.get("product_id") in candidates and "Not provided" in detail.get("text", "")):
            response.update(success=True, reason="Connection information is not provided. The agent reported a block and kept the list empty.",
                            outcome_kind="uncertainty_reported")
            evidence.append({"source": "visible_product_detail", **detail})
        else:
            response["reason"] = "Report blocked on the headphone details page when connection information is missing, and keep the list empty."
        return response
    if task.get("kind") == "none":
        results = state.get("results") or {}
        try:
            budget_ok = Decimal(str(results.get("max_price", ""))) <= Decimal(str(task["goal"]["max_price"]))
        except InvalidOperation:
            budget_ok = False
        cat_ok = results.get("category") == "thermos" or "thermos" in results.get("query", "").casefold()
        if (reason == "no_suitable_product" and state.get("cart_count") == 0 and path == "/sandbox/"
                and results.get("total") == 0 and budget_ok and cat_ok and not _matching_variants(task["goal"], catalog)):
            response.update(success=True, reason="No product matches the category and budget filters. The list is empty.",
                            outcome_kind="no_suitable_product")
            evidence.append({"source": "visible_search_results", **results})
        else:
            response["reason"] = "Apply the Thermos category and budget, check the empty results, then report no suitable product."
        return response
    if reason != "completed" or path != "/sandbox/cart":
        response["reason"] = "Check the final item and variant on the shopping list page before finishing."
        return response
    rows = state.get("cart") or []
    if len(rows) != 1 or state.get("cart_count") != 1:
        response["reason"] = "The shopping list must contain only one item that meets the task requirements."
        return response
    try:
        facts = _observed_item(rows[0], catalog)
    except (ValueError, TypeError) as exc:
        response["reason"] = str(exc)
        return response
    evidence.append({"source": "visible_cart_dom", **rows[0]})
    if _matches(facts, task["goal"]):
        response.update(success=True, reason="The listed item, variant, and price meet the requirements.", outcome_kind="cart_verified")
    else:
        response["reason"] = "The listed item variant or price does not meet the requirements."
    return response


async def evaluate_task(page, task_id: str, finish: dict) -> dict:
    if task_id.startswith("procure-"):
        from .procure import evaluate
        return await evaluate(page, task_id, finish)
    catalog = None
    if any(task["id"] == task_id for task in _TASKS):
        task = _private_task(task_id)
    else:
        from .cases import private_case, get_catalog
        task = private_case(task_id)
        catalog = get_catalog(task_id)["products"]
    try:
        state = await page.evaluate(_DOM_SNAPSHOT)
    except Exception as exc:
        return {"benchmark": BENCHMARK, "success": None, "reason": "The page could not be read. The result is unknown.",
                "evidence": [], "error_type": type(exc).__name__}
    return _grade_snapshot(task, state, finish, catalog=catalog)
