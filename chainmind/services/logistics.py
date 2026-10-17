"""Logistics domain operations - the deterministic core of the system.

Design rule: **agents decide, code executes.** The LLM chooses *which*
warehouse and *which* carrier and *how* to recover from an exception; it never
does arithmetic, never mutates stock directly, and never invents an ETA. Every
state change goes through one of these functions, each of which is atomic and
idempotent. That is what makes the flow both fast (no LLM round-trip for
mechanical work) and trustworthy (no hallucinated inventory).

These same functions are the MCP tool surface - see chainmind/mcp_server/.
"""
from __future__ import annotations

import json
import math
import time
import uuid
from typing import Any

from chainmind.core.db import get_db
from chainmind.services import maps

# ---------------------------------------------------------------- packaging
# (name, max_weight_g, inner_cm3, cost_inr, protects_fragile)
BOXES = [
    ("FLYER-S", 500, 2_000, 12.0, False),
    ("BOX-S", 2_000, 8_000, 24.0, False),
    ("BOX-M", 6_000, 27_000, 42.0, True),
    ("BOX-L", 18_000, 90_000, 78.0, True),
    ("CRATE-XL", 60_000, 300_000, 190.0, True),
]

# (carrier, base_inr, per_km_inr, speed_kmph, max_weight_g, fragile_ok, cold_chain)
CARRIER_PROFILES = {
    "BlueDart": (65.0, 2.4, 52.0, 30_000, True, True),
    "Delhivery": (48.0, 1.9, 46.0, 25_000, True, False),
    "Ekart": (40.0, 1.6, 42.0, 20_000, False, False),
    "IndiaPost": (25.0, 0.9, 28.0, 15_000, False, False),
    "DroneX": (140.0, 6.5, 95.0, 2_500, True, True),
}

COLD_CHAIN_SKUS = {"SKU-VACCINE-KIT"}


# ------------------------------------------------------------------ inventory
def search_inventory(sku: str, qty: int = 1) -> list[dict[str, Any]]:
    """Warehouses that can serve `qty` of `sku` right now (available = on_hand - reserved)."""
    rows = get_db().query(
        """
        SELECT i.warehouse_id, i.on_hand, i.reserved, i.unit_weight_g, i.fragile,
               w.name, w.kind, w.lat, w.lng, w.city, w.packing_lines, w.cutoff_hour
        FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.sku = ? AND (i.on_hand - i.reserved) >= ?
        ORDER BY (i.on_hand - i.reserved) DESC
        """,
        (sku, qty),
    )
    return [
        {
            "warehouse_id": r["warehouse_id"],
            "name": r["name"],
            "kind": r["kind"],
            "city": r["city"],
            "lat": r["lat"],
            "lng": r["lng"],
            "available": r["on_hand"] - r["reserved"],
            "unit_weight_g": r["unit_weight_g"],
            "fragile": bool(r["fragile"]),
            "packing_lines": r["packing_lines"],
            "cutoff_hour": r["cutoff_hour"],
        }
        for r in rows
    ]


def find_fulfillment_candidates(lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Which warehouses can ship the *whole* order, and which can only split it.

    A single-warehouse shipment is always preferred: one pick, one pack, one
    truck. Splitting doubles packaging and carrier cost, so the planner is told
    explicitly when a split is unavoidable.
    """
    per_sku: dict[str, set[str]] = {}
    detail: dict[str, list[dict]] = {}
    for line in lines:
        sku, qty = line["sku"], int(line["qty"])
        cands = search_inventory(sku, qty)
        detail[sku] = cands
        per_sku[sku] = {c["warehouse_id"] for c in cands}

    complete = set.intersection(*per_sku.values()) if per_sku else set()
    unfulfillable = [sku for sku, whs in per_sku.items() if not whs]

    meta: dict[str, dict] = {}
    for sku, cands in detail.items():
        for c in cands:
            meta.setdefault(c["warehouse_id"], {
                "warehouse_id": c["warehouse_id"],
                "name": c["name"],
                "kind": c["kind"],
                "city": c["city"],
                "lat": c["lat"],
                "lng": c["lng"],
                "packing_lines": c["packing_lines"],
                "cutoff_hour": c["cutoff_hour"],
            })

    return {
        "single_warehouse_options": [meta[w] for w in sorted(complete)],
        "partial_options": {sku: sorted(whs) for sku, whs in per_sku.items()},
        "unfulfillable_skus": unfulfillable,
        "requires_split": bool(per_sku) and not complete and not unfulfillable,
        "warehouse_meta": meta,
    }


def cap_lines_to_available(
    lines: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str | None]:
    """Largest subset a SINGLE warehouse can actually ship, plus the backorder.

    The honest answer to "500 laptops" is not "order rejected" and not "shipped":
    it is "65 ship today from WH-BLR, 435 are backordered". This picks the
    warehouse that maximises units shipped now, caps each line to that site's
    real availability, and returns the remainder so it can be reported rather
    than silently dropped.

    Deterministic and index-only - no model call, microseconds.
    """
    db = get_db()
    skus = [str(l["sku"]) for l in lines]
    if not skus:
        return [], [], None

    placeholders = ",".join("?" * len(skus))
    rows = db.query(
        f"SELECT warehouse_id, sku, (on_hand - reserved) AS available FROM inventory"
        f" WHERE sku IN ({placeholders}) AND (on_hand - reserved) > 0",
        skus,
    )
    avail: dict[str, dict[str, int]] = {}
    for r in rows:
        avail.setdefault(r["warehouse_id"], {})[r["sku"]] = int(r["available"])
    if not avail:
        return [], [{"sku": l["sku"], "qty": int(l["qty"])} for l in lines], None

    def shippable(wh: str) -> tuple[int, int]:
        have = avail[wh]
        units = sum(min(int(l["qty"]), have.get(str(l["sku"]), 0)) for l in lines)
        covered = sum(1 for l in lines if have.get(str(l["sku"]), 0) > 0)
        return units, covered

    best_wh = max(avail, key=lambda w: shippable(w))
    have = avail[best_wh]

    capped: list[dict[str, Any]] = []
    backorder: list[dict[str, Any]] = []
    for l in lines:
        sku, want = str(l["sku"]), int(l["qty"])
        can = min(want, have.get(sku, 0))
        if can > 0:
            capped.append({"sku": sku, "qty": can})
        if want - can > 0:
            backorder.append({"sku": sku, "qty": want - can})
    return capped, backorder, best_wh


def reserve_stock(order_id: str, warehouse_id: str, lines: list[dict[str, Any]]) -> dict[str, Any]:
    """Atomically reserve stock. Either every line reserves, or none does.

    Idempotent per (order_id, warehouse_id): re-running returns the existing
    allocation instead of double-reserving. This is what makes a retry safe.
    """
    db = get_db()
    existing = db.query(
        "SELECT sku, qty FROM allocations WHERE order_id=? AND warehouse_id=?",
        (order_id, warehouse_id),
    )
    if existing:
        return {
            "ok": True,
            "idempotent_replay": True,
            "warehouse_id": warehouse_id,
            "reserved": [{"sku": r["sku"], "qty": r["qty"]} for r in existing],
        }

    now = time.time()
    try:
        with db.tx() as conn:
            for line in lines:
                sku, qty = line["sku"], int(line["qty"])
                cur = conn.execute(
                    "UPDATE inventory SET reserved = reserved + ?"
                    " WHERE warehouse_id=? AND sku=? AND (on_hand - reserved) >= ?",
                    (qty, warehouse_id, sku, qty),
                )
                if cur.rowcount != 1:
                    raise ValueError(f"insufficient stock: {sku} x{qty} @ {warehouse_id}")
                conn.execute(
                    "INSERT INTO allocations (id,order_id,warehouse_id,sku,qty,created_at)"
                    " VALUES (?,?,?,?,?,?)",
                    (uuid.uuid4().hex, order_id, warehouse_id, sku, qty, now),
                )
    except ValueError as exc:
        return {"ok": False, "error": str(exc), "warehouse_id": warehouse_id}

    return {
        "ok": True,
        "idempotent_replay": False,
        "warehouse_id": warehouse_id,
        "reserved": lines,
    }


def release_stock(order_id: str) -> dict[str, Any]:
    """Compensating transaction - used by the healer when a plan is abandoned."""
    db = get_db()
    allocs = db.query("SELECT warehouse_id, sku, qty FROM allocations WHERE order_id=?", (order_id,))
    if not allocs:
        return {"ok": True, "released": 0}
    with db.tx() as conn:
        for a in allocs:
            conn.execute(
                "UPDATE inventory SET reserved = MAX(0, reserved - ?)"
                " WHERE warehouse_id=? AND sku=?",
                (a["qty"], a["warehouse_id"], a["sku"]),
            )
        conn.execute("DELETE FROM allocations WHERE order_id=?", (order_id,))
    return {"ok": True, "released": len(allocs)}


def cap_lines_to_parcel(lines: list[dict[str, Any]], warehouse_id: str
                       ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Cap an order to what a single parcel can legally carry.

    Stock availability is only half the constraint. 65 laptops is 117 kg and the
    heaviest carrier in the network takes 30 kg, so an order capped purely by
    stock still has no feasible carrier and dies at candidate selection. This
    trims quantities to the largest carrier payload and returns the remainder
    as backorder, which is what actually happens in a warehouse: you ship what
    fits on the vehicle and the rest goes on the next run.
    """
    db = get_db()
    budget_g = max(p[3] for p in CARRIER_PROFILES.values())  # heaviest payload
    budget_g = int(budget_g / 1.08) - 60                     # leave room for dunnage

    kept: list[dict[str, Any]] = []
    backorder: list[dict[str, Any]] = []
    remaining = budget_g

    # Heaviest-per-unit first, so a single bulky line cannot crowd out the
    # cheap-to-carry lines entirely.
    def unit_weight(sku: str) -> int:
        row = db.one("SELECT unit_weight_g FROM inventory WHERE sku=? LIMIT 1", (sku,))
        return row["unit_weight_g"] if row else 500

    for line in sorted(lines, key=lambda l: -unit_weight(l["sku"])):
        uw = max(1, unit_weight(line["sku"]))
        want = int(line["qty"])
        fits = max(0, min(want, remaining // uw))
        if fits:
            kept.append({"sku": line["sku"], "qty": fits})
            remaining -= fits * uw
        if fits < want:
            backorder.append({"sku": line["sku"], "qty": want - fits})

    return kept, backorder


# ------------------------------------------------------------------ packaging
def plan_packaging(lines: list[dict[str, Any]], warehouse_id: str) -> dict[str, Any]:
    """Pick the cheapest box that fits the weight and respects fragility."""
    db = get_db()
    total_g = 0
    any_fragile = False
    cold_chain = False
    for line in lines:
        row = db.one(
            "SELECT unit_weight_g, fragile FROM inventory WHERE warehouse_id=? AND sku=?",
            (warehouse_id, line["sku"]),
        )
        unit = row["unit_weight_g"] if row else 500
        frag = bool(row["fragile"]) if row else False
        total_g += unit * int(line["qty"])
        any_fragile = any_fragile or frag
        cold_chain = cold_chain or line["sku"] in COLD_CHAIN_SKUS

    packed_g = int(total_g * 1.08) + 60  # dunnage + label

    chosen = None
    for name, maxw, vol, cost, protects in BOXES:
        if packed_g <= maxw and (not any_fragile or protects):
            chosen = (name, maxw, vol, cost, protects)
            break
    if chosen is None:
        chosen = BOXES[-1]

    name, maxw, _vol, cost, _p = chosen
    extras = []
    if any_fragile:
        extras.append("bubble-wrap")
        cost += 15.0
    if cold_chain:
        extras.append("gel-pack-cold-chain")
        cost += 120.0

    lines_at_wh = db.one("SELECT packing_lines FROM warehouses WHERE id=?", (warehouse_id,))
    pack_lines = lines_at_wh["packing_lines"] if lines_at_wh else 1
    # Each pack line handles ~1 parcel/90s; more lines = less queueing.
    pack_minutes = round(4.0 + (len(lines) * 1.5) / max(1, pack_lines), 1)

    return {
        "box": name,
        "box_limit_g": maxw,
        "gross_weight_g": packed_g,
        "fragile": any_fragile,
        "cold_chain": cold_chain,
        "extras": extras,
        "packaging_cost_inr": round(cost, 2),
        "pack_minutes": pack_minutes,
        "warehouse_id": warehouse_id,
    }


# -------------------------------------------------------------------- carriers
def quote_carriers(
    distance_km: float,
    drive_minutes: float,
    gross_weight_g: int,
    *,
    fragile: bool = False,
    cold_chain: bool = False,
    priority: str = "standard",
) -> list[dict[str, Any]]:
    """Price every feasible carrier, ETA-corrected by learned bias.

    `eta_bias` comes from the feedback loop: carriers that historically run late
    get their predicted ETA inflated, so the planner stops over-promising. This
    is the learned signal, not a hardcoded constant.
    """
    db = get_db()
    stats = {
        r["carrier"]: {"eta_bias": r["eta_bias"], "damage_rate": r["damage_rate"],
                       "shipments": r["shipments"], "on_time": r["on_time"]}
        for r in db.query("SELECT * FROM carrier_stats")
    }

    out = []
    for carrier, (base, per_km, speed, maxw, frag_ok, cold_ok) in CARRIER_PROFILES.items():
        reasons = []
        if gross_weight_g > maxw:
            reasons.append(f"over weight limit ({gross_weight_g}g > {maxw}g)")
        if fragile and not frag_ok:
            reasons.append("not rated for fragile")
        if cold_chain and not cold_ok:
            reasons.append("no cold-chain")

        st = stats.get(carrier, {"eta_bias": 1.0, "damage_rate": 0.0, "shipments": 0, "on_time": 0})
        bias = float(st["eta_bias"])
        # Carrier's own line-haul speed vs. the road distance, then bias-corrected.
        transit = (distance_km / speed) * 60.0
        # Blend in the live-traffic drive time from Google Routes.
        eta = (0.65 * transit + 0.35 * drive_minutes) * bias
        handoff = 25.0 if priority == "express" else 55.0
        cost = base + per_km * distance_km + (35.0 if priority == "express" else 0.0)

        out.append({
            "carrier": carrier,
            "feasible": not reasons,
            "blocked_because": reasons,
            "eta_minutes": round(eta + handoff, 1),
            "cost_inr": round(cost, 2),
            "eta_bias_applied": round(bias, 3),
            "damage_rate": round(float(st["damage_rate"]), 4),
            "historical_shipments": int(st["shipments"]),
            "on_time_rate": round(st["on_time"] / st["shipments"], 3) if st["shipments"] else None,
        })

    out.sort(key=lambda c: (not c["feasible"], c["eta_minutes"] if priority == "express" else c["cost_inr"]))
    return out


# -------------------------------------------------------------------- dispatch
def dispatch_shipment(
    order_id: str,
    warehouse_id: str,
    carrier: str,
    packaging: dict[str, Any],
    route: dict[str, Any],
    eta_minutes: float,
    cost_inr: float,
) -> dict[str, Any]:
    """Commit the shipment, convert reservations into real stock depletion."""
    db = get_db()
    existing = db.one("SELECT id FROM shipments WHERE order_id=?", (order_id,))
    if existing:
        return {"ok": True, "idempotent_replay": True, "shipment_id": existing["id"]}

    ship_id = f"SHP-{uuid.uuid4().hex[:10].upper()}"
    now = time.time()
    allocs = db.query("SELECT sku, qty FROM allocations WHERE order_id=?", (order_id,))

    with db.tx() as conn:
        for a in allocs:
            conn.execute(
                "UPDATE inventory SET on_hand = on_hand - ?, reserved = MAX(0, reserved - ?)"
                " WHERE warehouse_id=? AND sku=?",
                (a["qty"], a["qty"], warehouse_id, a["sku"]),
            )
        conn.execute(
            "INSERT INTO shipments (id,order_id,warehouse_id,carrier,packaging,route,"
            "distance_km,eta_minutes,cost_inr,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                ship_id, order_id, warehouse_id, carrier,
                json.dumps(packaging), json.dumps(route),
                float(route.get("distance_km", 0.0)), float(eta_minutes), float(cost_inr),
                "in_transit", now,
            ),
        )
        conn.execute("UPDATE orders SET status='dispatched', updated_at=? WHERE id=?", (now, order_id))

    return {
        "ok": True,
        "idempotent_replay": False,
        "shipment_id": ship_id,
        "carrier": carrier,
        "eta_minutes": round(eta_minutes, 1),
        "cost_inr": round(cost_inr, 2),
        "tracking_url": f"/track/{ship_id}",
    }


def get_order_snapshot(order_id: str) -> dict[str, Any]:
    db = get_db()
    o = db.one("SELECT * FROM orders WHERE id=?", (order_id,))
    if not o:
        return {"found": False, "order_id": order_id}
    lines = db.query("SELECT sku, qty FROM order_lines WHERE order_id=?", (order_id,))
    ship = db.one("SELECT * FROM shipments WHERE order_id=?", (order_id,))
    return {
        "found": True,
        "order_id": o["id"],
        "status": o["status"],
        "priority": o["priority"],
        "destination": o["dest_label"],
        "lines": [{"sku": r["sku"], "qty": r["qty"]} for r in lines],
        "shipment": (
            {
                "shipment_id": ship["id"],
                "carrier": ship["carrier"],
                "status": ship["status"],
                "eta_minutes": ship["eta_minutes"],
                "actual_minutes": ship["actual_minutes"],
                "distance_km": ship["distance_km"],
                "cost_inr": ship["cost_inr"],
                "warehouse_id": ship["warehouse_id"],
            }
            if ship
            else None
        ),
    }


def network_snapshot() -> dict[str, Any]:
    db = get_db()
    whs = db.query(
        "SELECT w.*, (SELECT COALESCE(SUM(on_hand-reserved),0) FROM inventory i"
        " WHERE i.warehouse_id=w.id) AS units FROM warehouses w ORDER BY w.id"
    )
    return {
        "warehouses": [
            {
                "id": r["id"], "name": r["name"], "kind": r["kind"], "city": r["city"],
                "lat": r["lat"], "lng": r["lng"], "available_units": r["units"],
            }
            for r in whs
        ],
        "orders": db.one("SELECT COUNT(*) c FROM orders")["c"],
        "shipments": db.one("SELECT COUNT(*) c FROM shipments")["c"],
        "carriers": [dict(r) for r in db.query("SELECT * FROM carrier_stats ORDER BY carrier")],
    }
