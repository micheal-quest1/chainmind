"""Inventory management: stock health, replenishment, goods-in and returns.

Order fulfilment is only half of a logistics handler's job. The other half is
keeping stock where it needs to be *before* an order arrives:

  * every (warehouse, SKU) has a reorder point and a target level
  * stock below the reorder point is a replenishment need
  * a need is met by moving surplus from another node (a transfer), which is
    the godown -> storehouse leg of the network
  * goods also enter from outside: supplier deliveries and customer returns

Transfers deplete the donor immediately and land at the receiver on arrival,
so in-flight stock is never double-counted as available at both ends.
"""
from __future__ import annotations

import time
import uuid
from typing import Any

from chainmind.core.db import get_db

# Inter-warehouse movement is a bulk line-haul: cheaper per km than a parcel.
TRANSFER_BASE_INR = 220.0
TRANSFER_PER_KM_INR = 0.85
TRANSFER_PER_KG_INR = 1.4
TRANSFER_SPEED_KMPH = 45.0


# ------------------------------------------------------------- stock health
def stock_grid() -> dict[str, Any]:
    """Every SKU at every warehouse, with its health state.

    This is the screen a warehouse manager actually looks at.
    """
    rows = get_db().query(
        """
        SELECT i.warehouse_id, i.sku, i.on_hand, i.reserved, i.reorder_point,
               i.target_stock, i.unit_weight_g, i.fragile, w.name, w.city, w.kind
        FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id
        ORDER BY i.sku, i.warehouse_id
        """
    )
    items = []
    for r in rows:
        available = r["on_hand"] - r["reserved"]
        rp = r["reorder_point"]
        if available <= 0:
            state = "out_of_stock"
        elif rp and available <= rp * 0.5:
            state = "critical"
        elif rp and available <= rp:
            state = "low"
        else:
            state = "healthy"
        items.append({
            "warehouse_id": r["warehouse_id"], "warehouse": r["name"],
            "city": r["city"], "kind": r["kind"], "sku": r["sku"],
            "on_hand": r["on_hand"], "reserved": r["reserved"],
            "available": available, "reorder_point": rp,
            "target_stock": r["target_stock"],
            "unit_weight_g": r["unit_weight_g"], "fragile": bool(r["fragile"]),
            "state": state,
            "shortfall": max(0, r["target_stock"] - available) if state != "healthy" else 0,
        })

    by_state: dict[str, int] = {}
    for it in items:
        by_state[it["state"]] = by_state.get(it["state"], 0) + 1

    totals = get_db().query(
        "SELECT sku, SUM(on_hand) oh, SUM(reserved) rs, COUNT(*) locs"
        " FROM inventory GROUP BY sku ORDER BY sku"
    )
    return {
        "items": items,
        "by_state": by_state,
        "sku_totals": [
            {"sku": r["sku"], "on_hand": r["oh"], "reserved": r["rs"],
             "available": r["oh"] - r["rs"], "locations": r["locs"]}
            for r in totals
        ],
        "needs_attention": [i for i in items if i["state"] != "healthy"],
    }


def set_reorder_policy(warehouse_id: str, sku: str, reorder_point: int,
                       target_stock: int) -> dict[str, Any]:
    if target_stock < reorder_point:
        return {"ok": False, "error": "target_stock must be >= reorder_point"}
    n = get_db().execute(
        "UPDATE inventory SET reorder_point=?, target_stock=? WHERE warehouse_id=? AND sku=?",
        (int(reorder_point), int(target_stock), warehouse_id, sku),
    )
    if not n:
        return {"ok": False, "error": f"no stock record for {sku} @ {warehouse_id}"}
    return {"ok": True, "warehouse_id": warehouse_id, "sku": sku,
            "reorder_point": reorder_point, "target_stock": target_stock}


# -------------------------------------------------------------- goods inward
def receive_stock(warehouse_id: str, sku: str, qty: int, *, source: str = "supplier",
                  reference: str = "", note: str = "") -> dict[str, Any]:
    """Book goods into a warehouse: supplier delivery, return, or transfer arrival."""
    if qty <= 0:
        return {"ok": False, "error": "qty must be positive"}
    db = get_db()
    if not db.one("SELECT 1 FROM warehouses WHERE id=?", (warehouse_id,)):
        return {"ok": False, "error": f"unknown warehouse {warehouse_id}"}

    rid = f"GRN-{uuid.uuid4().hex[:8].upper()}"
    now = time.time()
    with db.tx() as conn:
        existing = conn.execute(
            "SELECT on_hand FROM inventory WHERE warehouse_id=? AND sku=?",
            (warehouse_id, sku),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE inventory SET on_hand = on_hand + ? WHERE warehouse_id=? AND sku=?",
                (qty, warehouse_id, sku),
            )
        else:
            # New SKU at this node: inherit physical attributes from elsewhere.
            tmpl = conn.execute(
                "SELECT unit_weight_g, fragile FROM inventory WHERE sku=? LIMIT 1", (sku,)
            ).fetchone()
            conn.execute(
                "INSERT INTO inventory (warehouse_id,sku,on_hand,reserved,unit_weight_g,"
                "fragile,reorder_point,target_stock) VALUES (?,?,?,0,?,?,?,?)",
                (warehouse_id, sku, qty,
                 tmpl["unit_weight_g"] if tmpl else 500,
                 tmpl["fragile"] if tmpl else 0,
                 max(5, qty // 4), max(10, qty)),
            )
        conn.execute(
            "INSERT INTO receipts (id,warehouse_id,sku,qty,source,reference,note,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (rid, warehouse_id, sku, qty, source, reference, note, now),
        )

    db.log_event("inventory", f"received_{source}",
                 payload={"receipt": rid, "warehouse": warehouse_id, "sku": sku, "qty": qty})
    row = db.one("SELECT on_hand, reserved FROM inventory WHERE warehouse_id=? AND sku=?",
                 (warehouse_id, sku))
    return {"ok": True, "receipt_id": rid, "warehouse_id": warehouse_id, "sku": sku,
            "qty": qty, "source": source,
            "on_hand_now": row["on_hand"], "available_now": row["on_hand"] - row["reserved"]}


def process_return(order_id: str, sku: str, qty: int, *, restockable: bool = True,
                   reason: str = "") -> dict[str, Any]:
    """Reverse logistics: a customer sends goods back.

    Restockable goods go back to the warehouse that shipped them. Damaged goods
    are recorded but never re-enter sellable stock.
    """
    db = get_db()
    ship = db.one("SELECT warehouse_id FROM shipments WHERE order_id=?", (order_id,))
    if not ship:
        return {"ok": False, "error": f"no shipment found for order {order_id}"}
    wh = ship["warehouse_id"]

    if not restockable:
        db.execute(
            "INSERT INTO receipts (id,warehouse_id,sku,qty,source,reference,note,created_at)"
            " VALUES (?,?,?,?,?,?,?,?)",
            (f"RMA-{uuid.uuid4().hex[:8].upper()}", wh, sku, qty, "return_scrap",
             order_id, reason, time.time()),
        )
        db.execute("UPDATE orders SET status='returned_damaged', updated_at=? WHERE id=?",
                   (time.time(), order_id))
        return {"ok": True, "restocked": False, "warehouse_id": wh,
                "note": "written off, not returned to sellable stock"}

    out = receive_stock(wh, sku, qty, source="return", reference=order_id, note=reason)
    db.execute("UPDATE orders SET status='returned', updated_at=? WHERE id=?",
               (time.time(), order_id))
    return {**out, "restocked": True, "order_id": order_id}


# ------------------------------------------------------------ replenishment
def replenishment_needs() -> list[dict[str, Any]]:
    """Every (warehouse, SKU) sitting at or below its reorder point."""
    rows = get_db().query(
        """
        SELECT i.warehouse_id, i.sku, i.on_hand, i.reserved, i.reorder_point,
               i.target_stock, i.unit_weight_g, w.name, w.city, w.lat, w.lng
        FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.reorder_point > 0 AND (i.on_hand - i.reserved) <= i.reorder_point
        ORDER BY (CAST(i.on_hand - i.reserved AS REAL) / i.reorder_point) ASC
        """
    )
    needs = []
    for r in rows:
        available = r["on_hand"] - r["reserved"]
        needs.append({
            "warehouse_id": r["warehouse_id"], "warehouse": r["name"], "city": r["city"],
            "lat": r["lat"], "lng": r["lng"], "sku": r["sku"],
            "available": available, "reorder_point": r["reorder_point"],
            "target_stock": r["target_stock"],
            "qty_needed": max(0, r["target_stock"] - available),
            "unit_weight_g": r["unit_weight_g"],
            "urgency": "critical" if available <= 0 else
                       ("high" if available <= r["reorder_point"] * 0.5 else "normal"),
        })
    return needs


def donor_options(sku: str, qty: int, exclude: str) -> list[dict[str, Any]]:
    """Warehouses with genuine surplus of a SKU - stock above their own target.

    A node is only a donor for what it holds *above* its own target level.
    Robbing one node to fill another just moves the problem.
    """
    rows = get_db().query(
        """
        SELECT i.warehouse_id, i.on_hand, i.reserved, i.target_stock,
               w.name, w.city, w.lat, w.lng
        FROM inventory i JOIN warehouses w ON w.id = i.warehouse_id
        WHERE i.sku = ? AND i.warehouse_id != ?
        """,
        (sku, exclude),
    )
    out = []
    for r in rows:
        available = r["on_hand"] - r["reserved"]
        surplus = available - max(r["target_stock"], 0)
        if surplus <= 0:
            continue
        out.append({
            "warehouse_id": r["warehouse_id"], "warehouse": r["name"], "city": r["city"],
            "lat": r["lat"], "lng": r["lng"],
            "available": available, "target_stock": r["target_stock"],
            "surplus": surplus, "can_send": min(surplus, qty),
        })
    out.sort(key=lambda d: d["surplus"], reverse=True)
    return out


def price_transfer(distance_km: float, qty: int, unit_weight_g: int) -> dict[str, Any]:
    kg = (qty * unit_weight_g) / 1000.0
    return {
        "distance_km": round(distance_km, 2),
        "weight_kg": round(kg, 2),
        "cost_inr": round(TRANSFER_BASE_INR + TRANSFER_PER_KM_INR * distance_km
                          + TRANSFER_PER_KG_INR * kg, 2),
        "eta_minutes": round((distance_km / TRANSFER_SPEED_KMPH) * 60.0 + 90.0, 1),
    }


def create_transfer(from_wh: str, to_wh: str, sku: str, qty: int, *, reason: str,
                    distance_km: float = 0.0, eta_minutes: float = 0.0,
                    cost_inr: float = 0.0, decided_by: str = "operator",
                    rationale: str = "") -> dict[str, Any]:
    """Move stock between nodes. Depletes the donor now, lands on receipt."""
    if from_wh == to_wh:
        return {"ok": False, "error": "source and destination are the same warehouse"}
    if qty <= 0:
        return {"ok": False, "error": "qty must be positive"}

    db = get_db()
    tid = f"TRF-{uuid.uuid4().hex[:8].upper()}"
    now = time.time()
    try:
        with db.tx() as conn:
            cur = conn.execute(
                "UPDATE inventory SET on_hand = on_hand - ?"
                " WHERE warehouse_id=? AND sku=? AND (on_hand - reserved) >= ?",
                (qty, from_wh, sku, qty),
            )
            if cur.rowcount != 1:
                raise ValueError(f"{from_wh} does not have {qty} available of {sku}")
            conn.execute(
                "INSERT INTO transfers (id,from_wh,to_wh,sku,qty,reason,distance_km,"
                "eta_minutes,cost_inr,status,decided_by,rationale,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (tid, from_wh, to_wh, sku, qty, reason, distance_km, eta_minutes,
                 cost_inr, "in_transit", decided_by, rationale, now),
            )
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    db.log_event("inventory", "transfer_dispatched",
                 payload={"transfer": tid, "from": from_wh, "to": to_wh,
                          "sku": sku, "qty": qty})
    return {"ok": True, "transfer_id": tid, "from_wh": from_wh, "to_wh": to_wh,
            "sku": sku, "qty": qty, "eta_minutes": eta_minutes, "cost_inr": cost_inr,
            "status": "in_transit"}


def receive_transfer(transfer_id: str) -> dict[str, Any]:
    """Land an in-flight transfer at its destination. Idempotent."""
    db = get_db()
    t = db.one("SELECT * FROM transfers WHERE id=?", (transfer_id,))
    if not t:
        return {"ok": False, "error": f"unknown transfer {transfer_id}"}
    if t["status"] == "received":
        return {"ok": True, "idempotent_replay": True, "transfer_id": transfer_id}

    out = receive_stock(t["to_wh"], t["sku"], t["qty"],
                        source="transfer", reference=transfer_id)
    db.execute("UPDATE transfers SET status='received', received_at=? WHERE id=?",
               (time.time(), transfer_id))
    return {"ok": True, "transfer_id": transfer_id, "to_wh": t["to_wh"],
            "sku": t["sku"], "qty": t["qty"], "available_now": out.get("available_now")}


def list_transfers(limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in get_db().query(
        "SELECT * FROM transfers ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))]


def list_receipts(limit: int = 50) -> list[dict[str, Any]]:
    return [dict(r) for r in get_db().query(
        "SELECT * FROM receipts ORDER BY created_at DESC LIMIT ?", (min(limit, 200),))]
