"""Paged procurement tasks, isolated carts, and controlled add failures."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
from pathlib import Path
import random
import re
import secrets
import threading
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

BENCHMARK = "shoptrace-procurement-v1"
PREFIX = "/sandbox/procurement"
COOKIE = "st_proc"
ASSETS = Path(__file__).resolve().parent / "proc_assets"
FAULTS = ("none", "ack_lost", "no_effect", "stockout")
router = APIRouter(prefix=PREFIX, tags=["procurement"])
_LOCK = threading.RLock()
_SESSIONS: dict[str, dict] = {}
_TTL = 3600
_LIMIT = 256

_LABELS = {"wireless": "Wireless", "quiet": "Quiet", "multi": "Multi-device",
           "compact": "Compact", "light": "Lightweight", "mic": "Microphone", "color": "Color"}
_NAMES = {"keyboard": "keyboard", "mouse": "mouse", "headset": "headset"}


def _product(pid, name, cat, price, color, wireless, **attrs):
    return {"id": pid, "name": name, "category": cat, "cents": price * 100,
            "attrs": {"color": color, "wireless": wireless, **attrs}, "stock": 8}


_PRODUCTS = [
    _product("pk01", "Atlas Keyboard", "keyboard", 179, "Black", True, quiet=True, multi=True, compact=False),
    _product("pk02", "Harbor Keyboard", "keyboard", 189, "White", True, quiet=True, multi=True, compact=True),
    _product("pk03", "Birch Keyboard", "keyboard", 99, "Black", False, quiet=True, multi=False, compact=False),
    _product("pk04", "Delta Keyboard", "keyboard", 129, "Gray", True, quiet=False, multi=False, compact=True),
    _product("pk05", "Ember Keyboard", "keyboard", 149, "Black", True, quiet=True, multi=False, compact=True),
    _product("pk06", "Fern Keyboard", "keyboard", 89, "White", False, quiet=True, multi=False, compact=True),
    _product("pk07", "Glen Keyboard", "keyboard", 199, "Black", True, quiet=True, multi=True, compact=True),
    _product("pk08", "Haze Keyboard", "keyboard", 79, "Gray", False, quiet=False, multi=False, compact=False),
    _product("pm01", "Cove Mouse", "mouse", 79, "Black", True, quiet=True, multi=True, light=True),
    _product("pm02", "Dune Mouse", "mouse", 69, "White", True, quiet=True, multi=False, light=True),
    _product("pm03", "Echo Mouse", "mouse", 49, "Black", False, quiet=True, multi=False, light=False),
    _product("pm04", "Flint Mouse", "mouse", 59, "Gray", True, quiet=False, multi=False, light=True),
    _product("pm05", "Grove Mouse", "mouse", 89, "Black", True, quiet=True, multi=True, light=True),
    _product("pm06", "Heath Mouse", "mouse", 39, "White", False, quiet=False, multi=False, light=True),
    _product("pm07", "Iris Mouse", "mouse", 75, "Black", True, quiet=True, multi=True, light=False),
    _product("pm08", "Jade Mouse", "mouse", 85, "White", True, quiet=True, multi=True, light=True),
    _product("ph01", "Kite Headset", "headset", 129, "Black", True, mic=True, light=True),
    _product("ph02", "Lark Headset", "headset", 149, "White", True, mic=True, light=False),
    _product("ph03", "Moss Headset", "headset", 69, "Black", False, mic=True, light=True),
    _product("ph04", "Nova Headset", "headset", 49, "Gray", False, mic=False, light=True),
    _product("ph05", "Opal Headset", "headset", 139, "Black", True, mic=True, light=True),
    _product("ph06", "Pine Headset", "headset", 59, "White", False, mic=True, light=True),
    _product("ph07", "Reed Headset", "headset", 109, "Gray", True, mic=False, light=False),
    _product("ph08", "Sage Headset", "headset", 79, "Black", False, mic=True, light=False),
]

_TASKS = [
    ("procure-dev-01", "A quiet flexible desk", 350, {"keyboard": {"wireless": True, "multi": True, "quiet": True},
                                                    "mouse": {"wireless": True, "quiet": True, "light": True}}),
    ("procure-dev-02", "A mixed-connection desk", 300, {"keyboard": {"wireless": False, "quiet": True},
                                                      "mouse": {"wireless": True, "multi": True}}),
    ("procure-dev-03", "A mobile meeting kit", 420, {"keyboard": {"wireless": True, "compact": True},
                                                   "headset": {"wireless": True, "mic": True}}),
    ("procure-dev-04", "A light calling setup", 260, {"mouse": {"wireless": True, "light": True, "quiet": True},
                                                     "headset": {"wireless": False, "mic": True}}),
    ("procure-dev-05", "Black desk and call equipment", 360, {"keyboard": {"color": "Black", "quiet": True},
                                                            "headset": {"color": "Black", "mic": True}}),
    ("procure-dev-06", "A shared three-piece desk", 500, {"keyboard": {"wireless": True, "quiet": True},
                                                        "mouse": {"color": "Black", "quiet": True},
                                                        "headset": {"wireless": False, "mic": True}}),
    ("procure-test-01", "A multi-device travel desk", 350, {"keyboard": {"color": "Black", "wireless": True, "multi": True},
                                                          "mouse": {"wireless": True, "multi": True, "light": True}}),
    ("procure-test-02", "A compact desk and call kit", 380, {"keyboard": {"quiet": True, "compact": True},
                                                          "headset": {"color": "Black", "wireless": True, "mic": True}}),
    ("procure-test-03", "Black mouse and wired audio", 240, {"mouse": {"color": "Black", "wireless": True, "quiet": True},
                                                          "headset": {"color": "Black", "wireless": False, "mic": True}}),
    ("procure-test-04", "A wireless three-piece desk", 520, {"keyboard": {"color": "Black", "wireless": True, "quiet": True},
                                                           "mouse": {"wireless": True, "light": True},
                                                           "headset": {"wireless": True, "mic": True}}),
]


def _hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def dataset_fingerprint():
    return _hash({"version": BENCHMARK, "products": _PRODUCTS, "tasks": _TASKS})


def benchmark_fingerprint():
    paths = [Path(__file__), *sorted(ASSETS.glob("*"))]
    return _hash({path.name: hashlib.sha256(path.read_bytes()).hexdigest() for path in paths if path.is_file()})


def _task(taskid):
    row = next((row for row in _TASKS if row[0] == taskid), None)
    if row is None:
        raise ValueError("Unknown procurement task")
    return {"id": row[0], "title": row[1], "budget": row[2] * 100, "needs": deepcopy(row[3]),
            "split": "development" if "-dev-" in row[0] else "heldout"}


def get_task(taskid):
    task = _task(taskid)
    parts = []
    for cat, attrs in task["needs"].items():
        terms = ["wired" if key == "wireless" and val is False else
                 str(val).lower() if key == "color" else _LABELS[key].lower()
                 for key, val in attrs.items()]
        parts.append("one " + ", ".join(terms) + " " + _NAMES[cat])
    goal = ("Buy " + "; ".join(parts) + f". Keep the combined price at or below CNY {task['budget'] // 100}. "
            "Inspect product details before adding items. Keep exactly one of each requested category and no other items. "
            "Open the shopping list and check quantities and the total before finishing.")
    return {"id": task["id"], "title": task["title"], "query": goal, "difficulty": "hard",
            "start_path": PREFIX + "/?" + urlencode({"task": taskid})}


def list_tasks(split=None):
    if split not in {None, "development", "heldout"}:
        raise ValueError("Unknown procurement split")
    return [get_task(row[0]) for row in _TASKS if split is None or _task(row[0])["split"] == split]


def _catalog(seed):
    rng = random.Random(seed)
    rows = deepcopy(_PRODUCTS)
    for row in rows:
        row["cents"] += rng.randint(-3, 3) * 100
    rng.shuffle(rows)
    return rows


def _prune():
    now = time.monotonic()
    for key in list(_SESSIONS):
        if now - _SESSIONS[key]["touch"] > _TTL:
            del _SESSIONS[key]


def new_case(taskid, seed=7, fault="none"):
    task = _task(taskid)
    if type(seed) is not int or not 0 <= seed <= 2**31 - 1:
        raise ValueError("Seed must be an integer between 0 and 2147483647")
    if fault not in FAULTS:
        raise ValueError("Unknown procurement fault")
    rows = _catalog(seed)
    meta = {"id": taskid, "split": task["split"], "seed": seed, "fault": fault,
            "catalog_fingerprint": _hash(rows), "dataset_fingerprint": dataset_fingerprint(),
            "benchmark_fingerprint": benchmark_fingerprint(), "benchmark": BENCHMARK}
    with _LOCK:
        _prune()
        if len(_SESSIONS) >= _LIMIT:
            raise ValueError("Procurement session capacity reached; wait for an inactive session to expire")
        token = secrets.token_hex(16)
        _SESSIONS[token] = {"task": task, "rows": rows, "cart": {}, "seen": set(), "fault": fault,
                            "fired": False, "ops": [], "checks": 0, "touch": time.monotonic(), "meta": meta}
    return {"start_path": PREFIX + "/?sid=" + token, "metadata": deepcopy(meta)}


def has_session(token):
    if not isinstance(token, str) or not re.fullmatch(r"[0-9a-f]{32}", token):
        return False
    with _LOCK:
        _prune()
        return token in _SESSIONS


def _session(request):
    token = request.cookies.get(COOKIE)
    with _LOCK:
        if not has_session(token):
            raise HTTPException(410, "Shopping session expired. Start a new task.")
        sess = _SESSIONS[token]
        sess["touch"] = time.monotonic()
        return sess


def _find(sess, pid):
    row = next((row for row in sess["rows"] if row["id"] == pid), None)
    if row is None:
        raise HTTPException(404, "Product not found")
    return row


def _view(row, detail=False):
    data = {key: row[key] for key in ("id", "name", "category", "cents")}
    if detail:
        data.update(attrs=deepcopy(row["attrs"]), stock=row["stock"])
    return data


def _cart(sess):
    rows = [_view(_find(sess, pid), True) | {"qty": qty} for pid, qty in sess["cart"].items()]
    return {"items": rows, "count": sum(row["qty"] for row in rows),
            "total": sum(row["cents"] * row["qty"] for row in rows)}


def _response(data, status=200):
    return JSONResponse(data, status_code=status, headers={"Cache-Control": "no-store", "Vary": "Cookie"})


def _origin(request):
    origin = request.headers.get("origin")
    if origin and origin != f"{request.url.scheme}://{request.headers.get('host')}":
        raise HTTPException(403, "Only same-origin shopping requests are accepted")


@router.get("/", include_in_schema=False)
@router.get("/product", include_in_schema=False)
@router.get("/cart", include_in_schema=False)
def page(request: Request):
    token = request.query_params.get("sid")
    taskid = request.query_params.get("task")
    if token is not None:
        if not has_session(token):
            raise HTTPException(410, "Shopping session expired. Start a new task.")
    elif taskid is not None or not request.cookies.get(COOKIE):
        try:
            case = new_case(taskid or _TASKS[0][0])
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        token = case["start_path"].split("sid=", 1)[1]
    else:
        _session(request)
    resp = FileResponse(ASSETS / "index.html", media_type="text/html", headers={"Cache-Control": "no-store", "Vary": "Cookie"})
    if token:
        resp.set_cookie(COOKIE, token, httponly=True, samesite="strict", path=PREFIX, max_age=_TTL)
    return resp


@router.get("/assets/{name}", include_in_schema=False)
def asset(name: str):
    media = {"styles.css": "text/css", "app.js": "text/javascript"}
    if name not in media:
        raise HTTPException(404, "Asset not found")
    return FileResponse(ASSETS / name, media_type=media[name])


@router.get("/api/catalog")
def catalog(request: Request, q: str = "", category: str = "", page: int = 1):
    if len(q) > 120 or category not in {"", *_NAMES} or not 1 <= page <= 100:
        raise HTTPException(422, "Invalid search filters")
    with _LOCK:
        sess = _session(request)
        rows = [row for row in sess["rows"] if (not category or row["category"] == category)
                and q.strip().casefold() in (row["name"] + " " + row["category"]).casefold()]
        pages = max(1, (len(rows) + 3) // 4)
        page = min(page, pages)
        return _response({"products": [_view(row) for row in rows[(page - 1) * 4:page * 4]],
                          "total": len(rows), "page": page, "pages": pages, "q": q.strip(), "category": category})


@router.get("/api/item/{pid}")
def item(pid: str, request: Request):
    with _LOCK:
        sess = _session(request)
        row = _find(sess, pid)
        sess["seen"].add(pid)
        return _response(_view(row, True))


@router.get("/api/cart")
def cart(request: Request):
    with _LOCK:
        sess = _session(request)
        sess["checks"] += 1
        return _response(_cart(sess))


class ItemSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(pattern=r"^p[kmh][0-9]{2}$")


class QtySpec(ItemSpec):
    delta: int = Field(ge=-1, le=1, strict=True)


def _add(sess, pid):
    row = _find(sess, pid)
    qty = sess["cart"].get(pid, 0)
    if row["stock"] <= qty:
        _log(sess, {"kind": "add", "id": pid, "effect": False, "fault": "none", "qty_before": qty})
        return {"ok": False, "state": "stockout", "message": "This item is now out of stock. Choose another product."}
    fault = "none" if sess["fired"] else sess["fault"]
    if fault != "none":
        sess["fired"] = True
    if fault == "stockout":
        row["stock"] = 0
    elif fault != "no_effect":
        sess["cart"][pid] = qty + 1
    _log(sess, {"kind": "add", "id": pid, "effect": fault not in {"no_effect", "stockout"}, "fault": fault, "qty_before": qty})
    if fault in {"ack_lost", "no_effect"}:
        return {"ok": False, "state": "uncertain", "message": "Add result not confirmed. Your shopping list may have changed."}
    if fault == "stockout":
        return {"ok": False, "state": "stockout", "message": "This item is now out of stock. Choose another product."}
    return {"ok": True, "state": "added", "message": "Added to your shopping list.", "count": sum(sess["cart"].values())}


def _limit(sess):
    if len(sess["ops"]) >= 300:
        raise HTTPException(429, "Shopping action limit reached")


def _log(sess, op):
    sess["ops"].append({"seq": len(sess["ops"]) + 1, **op,
                       "duplicates": sum(max(0, qty - 1) for qty in sess["cart"].values()),
                       "cart_count": sum(sess["cart"].values())})


@router.post("/api/add")
def add(spec: ItemSpec, request: Request):
    _origin(request)
    with _LOCK:
        sess = _session(request)
        _limit(sess)
        return _response(_add(sess, spec.id))


@router.post("/api/cart")
def quantity(spec: QtySpec, request: Request):
    _origin(request)
    if spec.delta not in {-1, 1}:
        raise HTTPException(422, "Quantity change must be -1 or 1")
    with _LOCK:
        sess = _session(request)
        _limit(sess)
        if spec.id not in sess["cart"]:
            raise HTTPException(404, "Item is not in the shopping list")
        if spec.delta == 1:
            return _response(_add(sess, spec.id))
        qty = sess["cart"][spec.id] - 1
        if qty:
            sess["cart"][spec.id] = qty
        else:
            del sess["cart"][spec.id]
        _log(sess, {"kind": "decrease", "id": spec.id})
        return _response({"ok": True, "state": "updated", "message": "Quantity updated."})


@router.post("/api/remove")
def remove(spec: ItemSpec, request: Request):
    _origin(request)
    with _LOCK:
        sess = _session(request)
        _limit(sess)
        if spec.id not in sess["cart"]:
            raise HTTPException(404, "Item is not in the shopping list")
        del sess["cart"][spec.id]
        _log(sess, {"kind": "remove", "id": spec.id})
        return _response({"ok": True, "state": "removed", "message": "Item removed."})


_SNAPSHOT = """() => {
  const vis = node => !!node && node.getClientRects().length > 0;
  const cart = document.querySelector('#proc-cart');
  return {url:location.href, visible:vis(cart),
    count:Number(document.querySelector('#cart-count')?.textContent || 0),
    total:Number(document.querySelector('#cart-total')?.dataset.cents),
    rows:[...document.querySelectorAll('.proc-item')].filter(vis).map(n => ({
      id:n.dataset.id, qty:Number(n.dataset.qty), cents:Number(n.dataset.cents), text:n.innerText
    }))};
}"""


def _metrics(sess):
    actual = _cart(sess)
    extra = sum(max(0, row["qty"] - 1) for row in actual["items"])
    adds = [op for op in sess["ops"] if op["kind"] == "add"]
    return {"duplicates": extra, "duplicate_peak": max([extra, *(op["duplicates"] for op in sess["ops"])]),
            "fault_triggered": sess["fired"], "add_attempts": len(adds),
            "add_retries": len(adds) - len({op["id"] for op in adds}),
            "duplicate_adds": sum(op.get("effect", False) and op.get("qty_before", 0) > 0 for op in adds),
            "uncertain_adds": sum(op.get("fault") in {"ack_lost", "no_effect"} for op in adds),
            "cart_checks": sess["checks"], "inspected_products": len(sess["seen"]),
            "cart_count": actual["count"], "total_cents": actual["total"]}


def audit(start_path, taskid):
    token = parse_qs(urlsplit(start_path).query).get("sid", [None])[0]
    with _LOCK:
        if not has_session(token) or _SESSIONS[token]["task"]["id"] != taskid:
            raise ValueError("Procurement session does not match this task")
        sess = _SESSIONS[token]
        adds = [op for op in sess["ops"] if op["kind"] == "add"]
        fired = next(((idx, op) for idx, op in enumerate(adds, 1) if op["fault"] != "none"), None)
        return {"benchmark": BENCHMARK, "task_id": taskid, "fault": sess["fault"],
                "metrics": _metrics(sess), "trigger": None if fired is None else {
                    "product": fired[1]["id"], "add_index": fired[0], "op_index": fired[1]["seq"]},
                "operations": deepcopy(sess["ops"]), "cart": _cart(sess), "inspected": sorted(sess["seen"])}


def _grade(sess, state, finish):
    actual = _cart(sess)
    result = {"benchmark": BENCHMARK, "success": False, "reason": "The procurement task is not complete.",
              "evidence": [], "metrics": _metrics(sess), "scope": "Checked against the session catalog, cart, and visible shopping list."}
    if finish.get("reason") != "completed" or not state.get("visible") or urlsplit(state.get("url", "")).path != PREFIX + "/cart":
        result["reason"] = "Open and check the final shopping list before finishing."
        return result
    expected = sorted((row["id"], row["qty"], row["cents"]) for row in actual["items"])
    shown = state.get("rows", [])
    observed = sorted((row.get("id", ""), row.get("qty", -1), row.get("cents", -1)) for row in shown)
    if observed != expected or state.get("count") != actual["count"] or state.get("total") != actual["total"]:
        result["reason"] = "The visible shopping list differs from the saved cart."
        return result
    task = sess["task"]
    if len(actual["items"]) != len(task["needs"]) or any(row["qty"] != 1 for row in actual["items"]):
        result["reason"] = "Keep exactly one item for each requested category, without duplicates or extras."
        return result
    cats = set()
    for row in actual["items"]:
        need = task["needs"].get(row["category"])
        if need is None or row["category"] in cats or any(row["attrs"].get(key) != val for key, val in need.items()):
            result["reason"] = "One or more selected items do not meet the requested specifications."
            return result
        if row["id"] not in sess["seen"]:
            result["reason"] = "Inspect the selected products' details before finishing."
            return result
        cats.add(row["category"])
    if actual["total"] > task["budget"]:
        result["reason"] = "The combined price exceeds the task budget."
        return result
    result.update(success=True, reason="The products, quantities, and combined price meet the requirements.", outcome_kind="procurement_verified",
                  evidence=[{"source": "visible_cart_dom", **row} for row in shown])
    return result


async def evaluate(page, taskid, finish):
    try:
        cookies = await page.context.cookies()
        token = next((row["value"] for row in cookies if row["name"] == COOKIE and row["path"] == PREFIX), None)
        state = await page.evaluate(_SNAPSHOT)
        with _LOCK:
            if not has_session(token) or _SESSIONS[token]["task"]["id"] != taskid:
                raise ValueError("Procurement session does not match this task")
            return _grade(_SESSIONS[token], state, finish)
    except Exception as exc:
        return {"benchmark": BENCHMARK, "success": None, "reason": "The procurement state could not be verified.",
                "evidence": [], "error_type": type(exc).__name__}
