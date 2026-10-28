"""Fixed browser checks using current DOM refs; no model calls."""
from __future__ import annotations


PATHS = {
    "browser-headphones-commute": [("fill", "Search products", "Headphones"), ("click", "Search"), ("click", "Next page"),
        ("click", "Sample Commute Headphones"), ("click", "Add to list"), ("click", "View list"), ("finish", "completed")],
    "browser-thermos-variant": [("fill", "Search products", "Thermos"), ("click", "Search"), ("click", "Sample Blue Bay Thermos"),
        ("select", "Color", "Blue"), ("select", "Capacity", "500ml"), ("click", "Add to list"),
        ("click", "View list"), ("finish", "completed")],
    "browser-powerbank-spec": [("fill", "Search products", "Power bank"), ("click", "Search"), ("click", "Sample Swift Power Bank"),
        ("click", "Add to list"), ("click", "View list"), ("finish", "completed")],
    "browser-lamp-adjustable": [("fill", "Search products", "Lamp"), ("click", "Search"), ("click", "Sample Warm Page Lamp"),
        ("click", "Add to list"), ("click", "View list"), ("finish", "completed")],
    "browser-thermos-budget": [("select", "Category", "thermos"), ("fill", "Maximum price (CNY)", "30"),
        ("click", "Search"), ("finish", "no_suitable_product")],
    "browser-headphones-unknown": [("fill", "Search products", "Gray"), ("click", "Search"), ("click", "Sample Cloud Headphones"), ("finish", "blocked")],
}


class SmokePolicy:
    def __init__(self, task_id):
        if task_id not in PATHS:
            raise ValueError("No fixed browser connectivity path exists for this task.")
        self.path, self.index = PATHS[task_id], 0

    def next_action(self, observation):
        if self.index >= len(self.path):
            raise ValueError("Browser connectivity path exhausted without finishing.")
        command = self.path[self.index]
        kind, label = command[:2]
        if kind == "finish":
            self.index += 1
            return {"type": "finish", "reason": label,
                    "evidence": ["Fixed browser check: page and shopping list reviewed."]}
        candidates = [element for element in observation.get("elements", [])
                      if element.get("name", "").strip() == label and not element.get("disabled")]
        role = "textbox" if kind == "fill" else "combobox" if kind == "select" else None
        if role:
            candidates = [element for element in candidates if element.get("role") == role]
        if not candidates:
            return {"type": "wait", "milliseconds": 250}
        self.index += 1
        action = {"type": kind, "ref": candidates[0]["ref"]}
        if kind == "fill":
            action["text"] = command[2]
        elif kind == "select":
            action["value"] = command[2]
        return action
