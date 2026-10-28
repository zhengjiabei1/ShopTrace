"""Procurement state, faults, and independent cart checks."""
from copy import deepcopy
import json
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from webagent import procure as proc


class ProcurementTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()
        app.include_router(proc.router)
        self.client = TestClient(app)
        self.tokens = []

    def tearDown(self):
        self.client.close()
        for token in self.tokens:
            proc._SESSIONS.pop(token, None)

    def start(self, fault="none", task="procure-dev-01", seed=7, client=None):
        case = proc.new_case(task, seed, fault)
        token = case["start_path"].split("sid=", 1)[1]
        self.tokens.append(token)
        resp = (client or self.client).get(case["start_path"])
        self.assertEqual(resp.status_code, 200)
        return proc._SESSIONS[token], case, resp

    def get(self, path, **kwargs):
        return self.client.get(proc.PREFIX + "/api" + path, **kwargs)

    def post(self, path, pid="pk01", **kwargs):
        return self.client.post(proc.PREFIX + "/api" + path, json={"id": pid, **kwargs})

    @staticmethod
    def snap(sess):
        cart = proc._cart(sess)
        return {"url": "http://testserver" + proc.PREFIX + "/cart", "visible": True,
                "count": cart["count"], "total": cart["total"],
                "rows": [{key: row[key] for key in ("id", "qty", "cents")} for row in cart["items"]]}

    def test_split_is_complete_and_public_metadata_contains_no_grading_fields(self):
        self.assertEqual(len(proc.list_tasks("development")), 6)
        self.assertEqual(len(proc.list_tasks("heldout")), 4)
        for task in proc.list_tasks():
            self.assertEqual(set(task), {"id", "title", "query", "difficulty", "start_path"})
            self.assertNotIn("fault", task["query"])
            self.assertNotIn("heldout", task["query"])
        with self.assertRaises(ValueError):
            proc.list_tasks("training")
        with self.assertRaises(ValueError):
            proc.get_task("missing")

    def test_every_task_has_a_budget_feasible_alternative_after_any_single_stockout(self):
        combos = set()
        for row in proc._TASKS:
            task = proc._task(row[0])
            key = json.dumps(task["needs"], sort_keys=True)
            self.assertNotIn(key, combos)
            combos.add(key)
            for seed in (0, 7, 19, 2**31 - 1):
                rows = proc._catalog(seed)
                for removed in [None, *(p["id"] for p in rows)]:
                    pools = [[p["cents"] for p in rows if p["id"] != removed and p["category"] == cat
                              and all(p["attrs"].get(k) == v for k, v in attrs.items())]
                             for cat, attrs in task["needs"].items()]
                    self.assertTrue(all(pools), (row[0], seed, removed))
                    self.assertLessEqual(sum(min(p) for p in pools), task["budget"])

    def test_seed_is_repeatable_and_changes_catalog_prices_and_order(self):
        self.assertEqual(proc._catalog(7), proc._catalog(7))
        self.assertNotEqual([r["id"] for r in proc._catalog(7)], [r["id"] for r in proc._catalog(8)])
        first = {r["id"]: r["cents"] for r in proc._catalog(7)}
        self.assertTrue(any(first[r["id"]] != r["cents"] for r in proc._catalog(8)))
        for seed in (True, -1, 2**31, "7"):
            with self.assertRaises(ValueError):
                proc.new_case("procure-dev-01", seed)

    def test_cookie_is_scoped_and_catalog_hides_details_and_fault_configuration(self):
        _, case, resp = self.start("ack_lost")
        self.assertIn("HttpOnly", resp.headers["set-cookie"])
        self.assertIn("SameSite=strict", resp.headers["set-cookie"])
        self.assertIn("Path=" + proc.PREFIX, resp.headers["set-cookie"])
        self.assertNotIn("ack_lost", case["start_path"])
        self.assertEqual(case["metadata"]["benchmark_fingerprint"], proc.benchmark_fingerprint())
        resp = self.get("/catalog")
        data = resp.json()
        self.assertEqual(data["total"], 24)
        self.assertEqual(len(data["products"]), 4)
        self.assertEqual(set(data["products"][0]), {"id", "name", "category", "cents"})
        self.assertNotIn("stock", resp.text)
        self.assertNotIn("attrs", resp.text)
        self.assertNotIn("fault", resp.text)
        self.assertEqual(resp.headers["cache-control"], "no-store")
        self.assertEqual(resp.headers["vary"], "Cookie")

    def test_search_pages_and_details_only_expose_requested_products(self):
        self.start()
        a = self.get("/catalog?category=keyboard&page=1").json()
        b = self.get("/catalog?category=keyboard&page=2").json()
        self.assertEqual(a["total"], 8)
        self.assertEqual({p["id"] for p in a["products"]} & {p["id"] for p in b["products"]}, set())
        one = self.get("/catalog?q=Atlas").json()
        self.assertEqual(one["total"], 1)
        detail = self.get("/item/pk01").json()
        self.assertEqual(detail["attrs"]["color"], "Black")
        self.assertEqual(detail["stock"], 8)
        self.assertEqual(self.get("/catalog?category=private").status_code, 422)
        self.assertEqual(self.get("/catalog?page=0").status_code, 422)
        self.assertEqual(self.get("/item/unknown").status_code, 404)

    def test_ack_lost_and_no_effect_have_identical_feedback_but_different_cart_state(self):
        self.start("ack_lost")
        lost = self.post("/add").json()
        self.assertEqual(self.get("/cart").json()["count"], 1)
        self.start("no_effect")
        empty = self.post("/add").json()
        self.assertEqual(self.get("/cart").json()["count"], 0)
        self.assertEqual(lost, empty)
        self.assertNotIn("count", lost)
        self.assertNotIn("ack_lost", str(lost))
        self.assertNotIn("check", lost["message"].lower())

    def test_fault_fires_once_and_blind_retry_can_be_repaired(self):
        sess, case, _ = self.start("ack_lost")
        self.assertFalse(self.post("/add").json()["ok"])
        self.assertTrue(self.post("/add").json()["ok"])
        self.assertEqual(self.get("/cart").json()["items"][0]["qty"], 2)
        self.assertTrue(self.post("/cart", delta=-1).json()["ok"])
        self.assertEqual(self.get("/cart").json()["count"], 1)
        self.assertEqual(sum(op.get("fault") == "ack_lost" for op in sess["ops"]), 1)
        result = proc._grade(sess, self.snap(sess), {"reason": "completed"})
        self.assertEqual(result["metrics"]["duplicates"], 0)
        self.assertEqual(result["metrics"]["duplicate_adds"], 1)
        self.assertEqual(result["metrics"]["duplicate_peak"], 1)
        self.assertEqual(result["metrics"]["uncertain_adds"], 1)
        self.assertGreaterEqual(result["metrics"]["cart_checks"], 2)
        audit = proc.audit(case["start_path"], "procure-dev-01")
        self.assertEqual(audit["metrics"], result["metrics"])
        self.assertEqual(audit["trigger"], {"product": "pk01", "add_index": 1, "op_index": 1})
        self.assertEqual([row["seq"] for row in audit["operations"]], [1, 2, 3])
        self.assertEqual(audit["cart"]["count"], 1)
        with self.assertRaises(ValueError):
            proc.audit(case["start_path"], "procure-dev-02")

    def test_audit_records_unfinished_runs_without_assigning_a_score(self):
        _, case, _ = self.start("no_effect")
        self.post("/add")
        audit = proc.audit("http://localhost" + case["start_path"], "procure-dev-01")
        self.assertTrue(audit["metrics"]["fault_triggered"])
        self.assertEqual(audit["metrics"]["cart_count"], 0)
        self.assertEqual(audit["metrics"]["add_attempts"], 1)
        self.assertNotIn("success", audit)
        self.assertEqual(audit["operations"][0]["effect"], False)

    def test_stockout_changes_inventory_and_allows_replacement(self):
        sess, _, _ = self.start("stockout")
        self.assertEqual(self.post("/add").json()["state"], "stockout")
        self.assertEqual(self.get("/cart").json()["count"], 0)
        self.assertEqual(self.get("/item/pk01").json()["stock"], 0)
        self.assertEqual(self.post("/add").json()["state"], "stockout")
        self.assertTrue(self.post("/add", "pk07").json()["ok"])
        self.assertEqual(sess["cart"], {"pk07": 1})

    def test_two_sessions_have_independent_carts_and_inventory(self):
        first, _, _ = self.start("stockout")
        self.post("/add")
        app = FastAPI()
        app.include_router(proc.router)
        with TestClient(app) as other:
            second, _, _ = self.start(client=other)
            self.assertEqual(other.get(proc.PREFIX + "/api/item/pk01").json()["stock"], 8)
            other.post(proc.PREFIX + "/api/add", json={"id": "pk01"})
            self.assertEqual(second["cart"], {"pk01": 1})
        self.assertEqual(first["cart"], {})

    def test_grading_requires_matching_visible_cart_specs_quantities_and_budget(self):
        sess, _, _ = self.start()
        for pid in ("pk01", "pm01"):
            self.get("/item/" + pid)
            self.post("/add", pid)
        state = self.snap(sess)
        self.assertTrue(proc._grade(sess, state, {"reason": "completed"})["success"])
        wrong = deepcopy(state)
        wrong["rows"][0]["cents"] = 1
        self.assertFalse(proc._grade(sess, wrong, {"reason": "completed"})["success"])
        self.assertFalse(proc._grade(sess, state, {"reason": "blocked"})["success"])
        wrong = deepcopy(state)
        wrong["visible"] = False
        self.assertFalse(proc._grade(sess, wrong, {"reason": "completed"})["success"])
        sess["task"]["budget"] = 100
        self.assertFalse(proc._grade(sess, state, {"reason": "completed"})["success"])
        sess["task"]["budget"] = 35000
        sess["seen"].clear()
        self.assertFalse(proc._grade(sess, state, {"reason": "completed"})["success"])

    def test_bad_attributes_duplicates_and_unrequested_products_fail_grading(self):
        sess, _, _ = self.start()
        for pid in ("pk04", "pm01"):
            self.get("/item/" + pid)
            self.post("/add", pid)
        self.assertFalse(proc._grade(sess, self.snap(sess), {"reason": "completed"})["success"])
        self.post("/remove", "pk04")
        self.get("/item/pk01")
        self.post("/add", "pk01")
        self.assertTrue(proc._grade(sess, self.snap(sess), {"reason": "completed"})["success"])
        self.post("/add", "pk01")
        result = proc._grade(sess, self.snap(sess), {"reason": "completed"})
        self.assertFalse(result["success"])
        self.assertEqual(result["metrics"]["duplicates"], 1)
        self.post("/cart", "pk01", delta=-1)
        self.post("/add", "ph01")
        self.assertFalse(proc._grade(sess, self.snap(sess), {"reason": "completed"})["success"])

    def test_invalid_mutations_and_foreign_origins_do_not_change_cart(self):
        sess, _, _ = self.start()
        self.assertEqual(self.post("/add", "../../private").status_code, 422)
        self.assertEqual(self.post("/add", "pk99").status_code, 404)
        self.assertEqual(self.post("/cart", delta=0).status_code, 422)
        resp = self.client.post(proc.PREFIX + "/api/add", json={"id": "pk01"}, headers={"Origin": "https://elsewhere.example"})
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(sess["cart"], {})
        self.assertEqual(self.client.get(proc.PREFIX + "/assets/../procure.py").status_code, 404)
        self.assertEqual(self.client.get(proc.PREFIX + "/assets/catalog.json").status_code, 404)

    def test_expired_or_invalid_sessions_are_not_silently_replaced(self):
        sess, case, _ = self.start()
        sess["touch"] = time.monotonic() - proc._TTL - 1
        self.assertEqual(self.get("/cart").status_code, 410)
        self.assertEqual(self.client.get(case["start_path"]).status_code, 410)
        self.assertEqual(self.client.get(proc.PREFIX + "/?sid=not-a-token").status_code, 410)
        self.assertFalse(proc.has_session("../anything"))

    def test_capacity_does_not_evict_live_sessions(self):
        _, case, _ = self.start()
        with patch.object(proc, "_LIMIT", len(proc._SESSIONS)):
            with self.assertRaises(ValueError):
                proc.new_case("procure-dev-02")
        self.assertEqual(self.client.get(case["start_path"]).status_code, 200)


if __name__ == "__main__":
    unittest.main()
