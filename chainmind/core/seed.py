"""Deterministic seed data: a 6-node Indian distribution network.

Seeding is idempotent, so boot order never matters and a redeploy never
duplicates rows.
"""
from __future__ import annotations

import time

from chainmind.core.db import Database

WAREHOUSES = [
    # id,     name,                  kind,         lat,     lng,      city,        lines, cutoff
    ("WH-MAA", "Chennai Central Godown", "godown", 13.0827, 80.2707, "Chennai", 4, 20),
    ("WH-BLR", "Bengaluru Storehouse", "storehouse", 12.9716, 77.5946, "Bengaluru", 3, 19),
    ("WH-HYD", "Hyderabad Godown", "godown", 17.3850, 78.4867, "Hyderabad", 2, 18),
    ("WH-BOM", "Mumbai Port Storehouse", "storehouse", 19.0760, 72.8777, "Mumbai", 5, 21),
    ("WH-DEL", "Delhi NCR Hub", "godown", 28.6139, 77.2090, "Delhi", 4, 20),
    ("WH-CJB", "Coimbatore Darkstore", "darkstore", 11.0168, 76.9558, "Coimbatore", 1, 16),
]

# sku, unit_weight_g, fragile, per-warehouse on_hand
CATALOG = [
    ("SKU-LAPTOP-14", 1800, 1, {"WH-MAA": 40, "WH-BLR": 65, "WH-BOM": 25, "WH-DEL": 30}),
    ("SKU-PHONE-PRO", 420, 1, {"WH-MAA": 120, "WH-BLR": 200, "WH-HYD": 80, "WH-DEL": 150}),
    ("SKU-MONITOR-27", 5200, 1, {"WH-BLR": 35, "WH-BOM": 48, "WH-DEL": 22}),
    ("SKU-KEYBOARD", 900, 0, {"WH-MAA": 300, "WH-CJB": 60, "WH-HYD": 140, "WH-BOM": 210}),
    ("SKU-CABLE-USBC", 60, 0, {"WH-MAA": 900, "WH-BLR": 1200, "WH-HYD": 700, "WH-BOM": 850, "WH-DEL": 1000, "WH-CJB": 300}),
    ("SKU-CHAIR-ERGO", 14000, 0, {"WH-BOM": 18, "WH-DEL": 12, "WH-BLR": 9}),
    ("SKU-VACCINE-KIT", 2500, 1, {"WH-MAA": 15, "WH-HYD": 10}),  # cold-chain, scarce
    ("SKU-TSHIRT-M", 180, 0, {"WH-CJB": 2000, "WH-MAA": 1500, "WH-BLR": 800}),
]

CARRIERS = [
    ("BlueDart", 1.0),
    ("Delhivery", 1.0),
    ("Ekart", 1.0),
    ("IndiaPost", 1.0),
    ("DroneX", 1.0),
]


def seed(db: Database, *, force: bool = False) -> dict[str, int]:
    if force:
        for t in ("inventory", "warehouses", "carrier_stats"):
            db.execute(f"DELETE FROM {t}")

    db.executemany(
        "INSERT INTO warehouses (id,name,kind,lat,lng,city,packing_lines,cutoff_hour)"
        " VALUES (?,?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        WAREHOUSES,
    )

    # Reorder point ~35% of nominal stock, target back up to nominal. Real
    # systems derive these from lead time x demand; this is a stand-in that
    # behaves the same way and makes the replenishment agent demonstrable.
    rows = []
    for sku, weight, fragile, stock in CATALOG:
        for wh, qty in stock.items():
            # target_stock must sit BELOW nominal holding, otherwise every node
            # is exactly at target, surplus is zero everywhere, and no transfer
            # can ever be proposed. Nodes carry ~30% above target as the buffer
            # that makes them donors.
            reorder_point = max(2, int(qty * 0.35))
            target_stock = max(reorder_point + 1, int(qty * 0.70))
            rows.append((wh, sku, qty, 0, weight, fragile, reorder_point, target_stock))
    db.executemany(
        "INSERT INTO inventory (warehouse_id,sku,on_hand,reserved,unit_weight_g,fragile,"
        "reorder_point,target_stock) VALUES (?,?,?,?,?,?,?,?)"
        " ON CONFLICT(warehouse_id,sku) DO UPDATE SET"
        " reorder_point=excluded.reorder_point, target_stock=excluded.target_stock",
        rows,
    )

    now = time.time()
    db.executemany(
        "INSERT INTO carrier_stats (carrier,shipments,on_time,eta_bias,damage_rate,updated_at)"
        " VALUES (?,0,0,?,0.0,?) ON CONFLICT(carrier) DO NOTHING",
        [(c, bias, now) for c, bias in CARRIERS],
    )

    return {
        "warehouses": len(WAREHOUSES),
        "inventory_rows": len(rows),
        "carriers": len(CARRIERS),
    }
