"""Recovery hints selected from visible page feedback."""
from __future__ import annotations

MODES = {"baseline", "verify", "adaptive"}
PROC_TEXT = """This task can require several products across pages. Compare observed
product details, retain each requirement, and check exact quantities and the total
price on the shopping list before finishing. Item availability can change."""
VERIFY = """When an add result is not confirmed, inspect the shopping list before
adding again. Retry only if the desired quantity is absent. If an item is out of
stock, find another item meeting the same requirements. Remove any excess quantity."""
ADAPTIVE = """Use visible feedback to decide the next check. An unconfirmed add
requires a shopping-list check; a stock change requires another valid choice.
After confirmed success, continue the task, and check the final list before finishing."""


def policy_text(mode):
    if mode not in MODES:
        raise ValueError("Unknown recovery mode")
    return PROC_TEXT + ("\n" + VERIFY if mode == "verify" else "\n" + ADAPTIVE if mode == "adaptive" else "")


def visible_hint(obs, mode):
    if mode not in MODES:
        raise ValueError("Unknown recovery mode")
    text = str(obs.get("text", "")).casefold()
    state, hint = "neutral", ""
    if obs.get("loading"):
        state = "loading"
        if mode == "adaptive":
            hint = "The page is still loading. Wait for a new observation before repeating any action."
    elif "add result not confirmed" in text:
        state = "uncertain"
        if mode != "baseline":
            hint = "The last add was not confirmed. Open the shopping list and check its current quantity before deciding whether to add again."
    elif "this item is now out of stock" in text:
        state = "stockout"
        if mode == "adaptive":
            hint = "The last item became unavailable. Compare other observed candidates against the original constraints; do not relax the requirements."
    elif "added to your shopping list" in text or "added to the shopping list" in text:
        state = "confirmed"
        if mode == "adaptive":
            hint = "The page confirms the add. Continue with remaining requirements and check the final shopping list."
    return {"mode": mode, "state": state, "guidance": hint,
            "observation_id": obs.get("observation_id")}
