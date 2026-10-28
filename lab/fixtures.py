"""Small synthetic fixtures for software tests."""
from __future__ import annotations

from copy import deepcopy

TASKS = {
    "fixture-001": {
        "id": "fixture-001", "title": "Headphones: budget and color", "source": "fixture",
        "query": "[Fixture] Find black wireless headphones costing no more than CNY 300.",
        "search": "black wireless headphones", "product": "DEMO-HEADPHONE",
        "options": ["black"],
        "products": [
            {"asin": "DEMO-HEADPHONE", "title": "Demo Wireless Headphones", "brand": "Demo", "category": "headphones", "price": 249, "key_attributes": ["wireless", "black"]},
            {"asin": "DEMO-EXPENSIVE", "title": "Demo Premium Headphones", "brand": "Demo", "category": "headphones", "price": 499, "key_attributes": ["wireless", "black"]},
        ],
    },
    "fixture-002": {
        "id": "fixture-002", "title": "Thermos: choose capacity", "source": "fixture",
        "query": "[Fixture] Find a blue 500ml thermos costing no more than CNY 100.",
        "search": "blue 500ml thermos", "product": "DEMO-CUP",
        "options": ["blue", "500ml"],
        "products": [
            {"asin": "DEMO-CUP", "title": "Demo Thermos", "brand": "Demo", "category": "thermos", "price": 69, "key_attributes": ["blue", "500ml"]},
            {"asin": "DEMO-CUP-SMALL", "title": "Demo Small Thermos", "brand": "Demo", "category": "thermos", "price": 49, "key_attributes": ["blue", "300ml"]},
        ],
    },
    "fixture-003": {
        "id": "fixture-003", "title": "No match: stop without buying", "source": "fixture",
        "query": "[Fixture] Find a mechanical keyboard costing no more than CNY 50; stop if none match.",
        "search": "mechanical keyboard", "product": "DEMO-KEYBOARD", "options": [], "abstain": True,
        "products": [
            {"asin": "DEMO-KEYBOARD", "title": "Demo Mechanical Keyboard", "brand": "Demo", "category": "keyboards", "price": 199, "key_attributes": ["mechanical"]},
        ],
    },
}


def list_tasks() -> list[dict]:
    return [{key: task[key] for key in ("id", "title", "query", "source")} for task in TASKS.values()]


def get_task(task_id: str) -> dict:
    if task_id not in TASKS:
        raise ValueError("unknown fixture task")
    return deepcopy(TASKS[task_id])


def scripted_plan(task_id: str) -> list[tuple[str, dict]]:
    task = get_task(task_id)
    result = [("search_products", {"query": task["search"]}), ("open_product", {"asin": task["product"]}), ("view_features", {})]
    result.extend(("select_option", {"value": option}) for option in task["options"])
    result.append(("finish_without_purchase", {"reason": "no_suitable_product"}) if task.get("abstain") else ("buy_now", {}))
    return result
