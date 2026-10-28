"""Authored case split and independent-grader checks; no model calls."""
from copy import deepcopy
from decimal import Decimal
import hashlib
import json
import math
import unittest
from unittest.mock import patch

from webagent import cases, tasks


class ExpandedCaseTests(unittest.TestCase):
    def cases(self):
        return [cases.private_case(item["id"]) for item in cases.list_cases()]

    def test_split_cases_products_and_names_are_disjoint_from_each_other_and_legacy(self):
        development = cases.list_cases("development")
        heldout = cases.list_cases("heldout")
        self.assertEqual((len(development), len(heldout)), (6, 6))
        self.assertFalse({c["id"] for c in development} & {c["id"] for c in heldout})
        product_sets = []
        name_sets = []
        for split in (development, heldout):
            products = [p for case in split for p in cases.get_catalog(case["id"])["products"]]
            self.assertEqual(len(products), 48)
            ids = {p["id"] for p in products}
            self.assertEqual(len(ids), 48)
            product_sets.append(ids)
            name_sets.append({p["name"] for p in products})
        self.assertFalse(product_sets[0] & product_sets[1])
        self.assertFalse(name_sets[0] & name_sets[1])
        self.assertFalse((product_sets[0] | product_sets[1]) & {p["id"] for p in tasks._catalog()})
        self.assertEqual(len(tasks.list_tasks()), 6)
        self.assertEqual(len(tasks._catalog()), 12)

    def test_public_tasks_and_catalogs_do_not_leak_private_grading_or_split_metadata(self):
        for public in cases.list_cases():
            self.assertEqual(set(public), {"id", "title", "query", "difficulty", "start_path"})
            self.assertEqual(public["start_path"], "/sandbox/?case=" + public["id"])
            catalog = cases.get_catalog(public["id"])
            self.assertEqual(set(catalog), {"schema_version", "notice", "products"})
            serialized = json.dumps(catalog, ensure_ascii=False)
            for private in ("development", "heldout", "goal", "unknown_field", "expected_product", "success", public["id"]):
                self.assertNotIn(private, serialized)
            for product in catalog["products"]:
                self.assertEqual(set(product), cases.PUBLIC_PRODUCT_FIELDS)

    def test_catalogs_follow_the_actual_page_variant_schema_and_keep_facts_visible(self):
        for case in self.cases():
            products = case["catalog"]["products"]
            self.assertEqual(len(products), 8)
            for product in products:
                self.assertTrue(product["name"].startswith("Sample "))
                self.assertIn(product["category"], {"headphones", "thermos", "powerbank", "lamp"})
                self.assertTrue(isinstance(product["price"], (int, float)) and math.isfinite(product["price"]) and product["price"] > 0)
                self.assertIsInstance(product["facts"], dict)
                self.assertIsInstance(product["tags"], list)
                for axis in product["options"]:
                    self.assertIn(axis["id"], {"color", "capacity"})
                    values = axis["values"]
                    self.assertGreaterEqual(len(values), 2)
                    self.assertEqual(len({value["value"] for value in values}), len(values))
                    for value in values:
                        self.assertIsInstance(value["value"], str)
                        self.assertIsInstance(value["label"], str)
                        self.assertGreaterEqual(value.get("price_delta", 0), 0)
                self.assertTrue(all(variant["price"] >= product["price"] for variant in tasks._variants(product)))

    def test_each_positive_case_has_exactly_one_matching_variant_and_decoys_fail_grading(self):
        prices = {"case-7f20": Decimal("249"), "case-b8d1": Decimal("115"),
                  "case-2c94": Decimal("219"), "case-e615": Decimal("169"),
                  "case-a64d": Decimal("319"), "case-31f9": Decimal("148"),
                  "case-d082": Decimal("249"), "case-58be": Decimal("199")}
        for case in self.cases():
            if case.get("kind"):
                continue
            products = case["catalog"]["products"]
            variants = [variant for product in products for variant in tasks._variants(product)]
            matching = [variant for variant in variants if tasks._matches(variant, case["goal"])]
            self.assertEqual(len(matching), 1, case["id"])
            self.assertEqual(matching[0]["price"], prices[case["id"]])
            with patch.object(tasks, "_catalog", return_value=products):
                for variant in variants:
                    row = {"product_id": variant["product_id"], "color": variant.get("color", ""),
                           "capacity": f'{variant["capacity_ml"]}ml' if variant.get("capacity_ml") else "",
                           "price": str(variant["price"]), "text": "Visible final cart record"}
                    state = {"url": "http://localhost/sandbox/cart", "cart_count": 1, "cart": [row]}
                    result = tasks._grade_snapshot(case, state, {"reason": "completed"})
                    self.assertEqual(result["success"], variant == matching[0], (case["id"], variant, result))

    def test_thermos_goal_requires_both_variant_changes_and_selected_price(self):
        for case_id in ("case-b8d1", "case-31f9"):
            case = cases.private_case(case_id)
            products = case["catalog"]["products"]
            candidates = [p for p in products if any(tasks._matches(v, case["goal"]) for v in tasks._variants(p))]
            self.assertEqual(len(candidates), 1)
            product = candidates[0]
            self.assertFalse(tasks._matches(tasks._variant(product), case["goal"]))
            selected = {"color": case["goal"]["color"], "capacity": str(case["goal"]["capacity_ml"]) + "ml"}
            selected_variant = tasks._variant(product, selected)
            self.assertTrue(tasks._matches(selected_variant, case["goal"]))
            self.assertGreater(selected_variant["price"], product["price"])
            # Another apparently affordable starting price becomes over budget
            # when the same requested color/capacity are selected.
            decoys = [p for p in products if p["id"] != product["id"] and p["options"]]
            self.assertEqual(len(decoys), 1)
            self.assertLess(decoys[0]["price"], case["goal"]["max_price"])
            self.assertGreater(tasks._variant(decoys[0], selected)["price"], case["goal"]["max_price"])

    def test_no_match_cases_produce_zero_visible_results_under_requested_filter(self):
        for case in self.cases():
            if case.get("kind") != "none":
                continue
            products = case["catalog"]["products"]
            goal = case["goal"]
            # This matches the page's category and base-price filter, rather
            # than assuming no final variant is enough for an empty page.
            filtered = [p for p in products if p["category"] == goal["category"] and p["price"] <= goal["max_price"]]
            self.assertEqual(filtered, [])
            self.assertTrue(any(p["price"] <= goal["max_price"] for p in products))
            with patch.object(tasks, "_catalog", return_value=products):
                self.assertEqual(tasks._matching_variants(goal), [])
                state = {"url": "http://localhost/sandbox/", "cart_count": 0, "cart": [],
                         "results": {"total": 0, "query": "", "category": "thermos", "max_price": str(goal["max_price"]), "text": "0 products found"}}
                self.assertTrue(tasks._grade_snapshot(case, state, {"reason": "no_suitable_product"})["success"])
                state["results"]["max_price"] = ""
                self.assertFalse(tasks._grade_snapshot(case, state, {"reason": "no_suitable_product"})["success"])

    def test_unknown_cases_have_unique_relevant_missing_info_and_require_blocked_empty_cart(self):
        for case in self.cases():
            if case.get("kind") != "unknown":
                continue
            products = case["catalog"]["products"]
            known = {k: v for k, v in case["goal"].items() if k != case["unknown_field"]}
            relevant = [v for p in products for v in tasks._variants(p) if tasks._matches(v, known)]
            self.assertEqual(len(relevant), 1)
            self.assertIsNone(relevant[0]["wireless"])
            self.assertIsNone(relevant[0]["connection"])
            self.assertFalse(any(tasks._matches(v, case["goal"]) for p in products for v in tasks._variants(p)))
            state = {"url": "http://localhost/sandbox/product", "cart_count": 0, "cart": [],
                     "detail": {"product_id": relevant[0]["product_id"], "text": "Wireless Not provided Connection Not provided"}}
            with patch.object(tasks, "_catalog", return_value=products):
                self.assertTrue(tasks._grade_snapshot(case, state, {"reason": "blocked"})["success"])
                self.assertFalse(tasks._grade_snapshot(case, state, {"reason": "completed"})["success"])
                state["cart_count"] = 1
                self.assertFalse(tasks._grade_snapshot(case, state, {"reason": "blocked"})["success"])

    def test_fingerprints_and_copy_isolation_keep_case_definitions_reproducible(self):
        self.assertEqual(hashlib.sha256(cases.DATASET_PATH.read_bytes()).hexdigest(), cases.dataset_fingerprint())
        manifest = cases.split_manifest()
        self.assertEqual(manifest["dataset_sha256"], cases.DATASET_SHA256)
        self.assertEqual(set(manifest["splits"]), {"development", "heldout"})
        item = cases.list_cases()[0]
        catalog = cases.get_catalog(item["id"])
        original = deepcopy(catalog)
        fingerprint = cases.catalog_fingerprint(item["id"])
        catalog["products"][0]["price"] = -1
        private = cases.private_case(item["id"])
        private["goal"]["max_price"] = -1
        self.assertEqual(cases.get_catalog(item["id"]), original)
        self.assertEqual(cases.catalog_fingerprint(item["id"]), fingerprint)
        self.assertGreater(cases.private_case(item["id"])["goal"]["max_price"], 0)
        item["query"] = "modified"
        self.assertNotEqual(cases.get_case_public(item["id"])["query"], "modified")

    def test_unknown_ids_and_split_names_do_not_resolve_arbitrary_data(self):
        for value in ("../../tasks.py", "case-0000", "development", None, []):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cases.get_case_public(value)
        for value in ("train", "test", "../", ""):
            with self.subTest(value=value), self.assertRaises(ValueError):
                cases.list_cases(value)


if __name__ == "__main__":
    unittest.main()
