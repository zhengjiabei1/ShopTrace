"""Observed-page memory and visible recovery decisions."""
import json
import unittest

from webagent.memory import PageMemory
from webagent.recovery import policy_text, visible_hint
from webagent.runtime import model_context


def observe(url, text, seq=1, **extra):
    return {"url": "http://127.0.0.1/sandbox/" + url, "text": text,
            "observation_id": f"obs-{seq:04d}", "title": "Visible product",
            "elements": [{"ref": f"obs-{seq:04d}-e1", "name": "Add"}], **extra}


class PageMemoryTests(unittest.TestCase):
    def test_only_observed_text_survives_and_current_refs_stay_current(self):
        memory = PageMemory()
        old = observe("product?id=one&sid=private-session", "Keyboard: wireless, CNY 120", 1,
                      goal={"private": "EXPECTED ANSWER"}, expected="hidden target")
        memory.observe(old, 0)
        current = observe("product?id=two", "Mouse: silent, CNY 60", 2)
        memory.observe(current, 1)
        journal = [{"step": 1, "action": {"type": "click", "ref": "obs-0001-e1"}, "target": "View mouse"}]
        context = model_context([], "Buy a keyboard and mouse", current, journal, "page_memory", memory=memory)
        text = json.dumps(context)
        self.assertIn("Keyboard: wireless, CNY 120", text)
        self.assertIn("obs-0002-e1", text)
        self.assertNotIn("obs-0001-e1", text)
        for value in ("EXPECTED ANSWER", "hidden target", "private-session"):
            self.assertNotIn(value, text)
        self.assertEqual(context[0]["role"], "system")
        page = memory.context(current["url"])["pages"][0]
        self.assertEqual(page["observation_id"], "obs-0001")
        self.assertEqual(len(page["text_hash"]), 64)
        self.assertEqual(old["elements"][0]["ref"], "obs-0001-e1")

    def test_same_url_updates_stock_but_loading_keeps_last_read_page(self):
        memory = PageMemory()
        first = observe("product?id=a", "In stock: 3", 1)
        memory.observe(first, 0)
        memory.observe(observe("product?id=a", "Out of stock", 2), 1)
        event = memory.observe(observe("product?id=a", "Loading…", 3, loading=True), 2)
        self.assertTrue(event["loading"])
        self.assertFalse(event["changed"])
        pages = memory.context("http://127.0.0.1/sandbox/cart")["pages"]
        self.assertEqual(len(pages), 1)
        self.assertEqual(pages[0]["text"], "Out of stock")
        self.assertEqual(pages[0]["observation_id"], "obs-0002")
        pages[0]["text"] = "external mutation"
        self.assertEqual(memory.context("elsewhere")["pages"][0]["text"], "Out of stock")

    def test_clipping_eviction_and_source_truncation_are_explicit(self):
        memory = PageMemory(max_pages=2, max_chars=15, page_chars=10)
        event = memory.observe(observe("one", "a" * 20, truncated=True), 0)
        self.assertEqual(event["clipped_pages"], 1)
        self.assertTrue(event["source_truncated"])
        memory.observe(observe("two", "b" * 10, 2), 1)
        self.assertEqual(memory.stats()["evicted_pages"], 1)
        self.assertLessEqual(memory.characters, 15)
        page = memory.context("elsewhere")["pages"][0]
        self.assertTrue(page["url"].endswith("two"))
        self.assertNotIn("a" * 20, json.dumps(memory.context("elsewhere")))

    def test_empty_or_invalid_memory_is_not_silently_used(self):
        with self.assertRaises(ValueError):
            PageMemory(max_pages=0)
        with self.assertRaises(ValueError):
            model_context([], "Goal", {}, [], "page_memory")


class VisibleRecoveryTests(unittest.TestCase):
    def test_hidden_fault_name_does_not_select_recovery(self):
        a = visible_hint({"text": "Product details", "fault": "ack_lost"}, "adaptive")
        b = visible_hint({"text": "Product details", "fault": "stockout"}, "adaptive")
        self.assertEqual(a, b)
        self.assertEqual(a["state"], "neutral")
        self.assertFalse(a["guidance"])

    def test_uncertainty_is_the_same_for_applied_and_unapplied_add(self):
        text = "Add result not confirmed. Your shopping list may have changed."
        a = visible_hint({"text": text, "fault": "ack_lost"}, "verify")
        b = visible_hint({"text": text, "fault": "no_effect"}, "verify")
        self.assertEqual(a, b)
        self.assertEqual(a["state"], "uncertain")
        self.assertIn("check its current quantity", a["guidance"])
        self.assertFalse(visible_hint({"text": text}, "baseline")["guidance"])
        self.assertNotIn("Retry only", policy_text("baseline"))

    def test_adaptive_distinguishes_loading_stockout_and_success(self):
        rows = [({"loading": True, "text": "Add result not confirmed"}, "loading", "Wait"),
                ({"text": "This item is now out of stock. Choose another product."}, "stockout", "other observed candidates"),
                ({"text": "Added to your shopping list"}, "confirmed", "remaining requirements")]
        for obs, state, phrase in rows:
            hint = visible_hint(obs, "adaptive")
            self.assertEqual(hint["state"], state)
            self.assertIn(phrase, hint["guidance"])
        with self.assertRaises(ValueError):
            policy_text("invented")
