"""Exercise the inventory half of the system: stock health, replenishment,
transfers, goods-in and returns."""
from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")


async def main() -> int:
    from chainmind import replenishment
    from chainmind.agents import llm
    from chainmind.core.db import get_db
    from chainmind.core.seed import seed
    from chainmind.pipeline import run_order
    from chainmind.services import inventory as inv, maps

    db = get_db()
    seed(db)
    await asyncio.gather(llm.resolve_models(), maps.probe_apis())

    grid = inv.stock_grid()
    print(f"STOCK HEALTH (baseline): {grid['by_state']}")

    # Drive a node below its reorder point the way reality would: ship from it.
    print("\nDraining WH-HYD vaccine kits to trigger a reorder...")
    row = db.one("SELECT on_hand, reorder_point, target_stock FROM inventory"
                 " WHERE warehouse_id='WH-HYD' AND sku='SKU-VACCINE-KIT'")
    print(f"  before: on_hand={row['on_hand']} reorder_point={row['reorder_point']}")
    db.execute("UPDATE inventory SET on_hand=1 WHERE warehouse_id='WH-HYD'"
               " AND sku='SKU-VACCINE-KIT'")

    needs = inv.replenishment_needs()
    print(f"\nREPLENISHMENT NEEDS: {len(needs)}")
    for n in needs[:5]:
        print(f"  {n['warehouse_id']:7s} {n['sku']:18s} available={n['available']:5d} "
              f"reorder_at={n['reorder_point']:5d} need={n['qty_needed']:5d} [{n['urgency']}]")

    print("\nRunning the inventory planner...")
    out = await replenishment.run(execute=True)
    print(f"  model={out.get('model')} degraded={out.get('degraded')} {out['total_ms']}ms")
    print(f"  summary: {out['summary']}")
    for t in out["transfers_executed"]:
        print(f"  MOVE {t.get('qty')} x {t.get('sku')} {t.get('from_wh', t.get('from_warehouse'))}"
              f" -> {t.get('to_wh', t.get('to_warehouse'))}"
              f" ({t.get('distance_km')}km, Rs.{t.get('cost_inr')}) : {t.get('reason','')}")
    for r in out["rejected"]:
        print(f"  REJECTED {r.get('sku')} -> {r.get('rejected_because')}")
    for p in out.get("purchase_suggestions", []):
        print(f"  BUY {p.get('qty')} x {p.get('sku')} : {p.get('why')}")

    # Land the transfers.
    transfers = [t for t in inv.list_transfers(20) if t["status"] == "in_transit"]
    print(f"\nLANDING {len(transfers)} in-flight transfers")
    for t in transfers:
        r = inv.receive_transfer(t["id"])
        print(f"  {t['id']} -> {t['to_wh']} now has {r.get('available_now')} of {t['sku']}")

    after = db.one("SELECT on_hand FROM inventory WHERE warehouse_id='WH-HYD'"
                   " AND sku='SKU-VACCINE-KIT'")
    print(f"  WH-HYD vaccine kits: 1 -> {after['on_hand']}")

    # Goods inward from a supplier.
    print("\nGOODS INWARD")
    g = inv.receive_stock("WH-CJB", "SKU-LAPTOP-14", 25, source="supplier", reference="PO-9001")
    print(f"  {g['receipt_id']}: WH-CJB now holds {g['available_now']} laptops "
          f"(new SKU at this node)")

    # Returns.
    print("\nRETURNS")
    r = await run_order("1 keyboard to Gandhipuram, Coimbatore")
    oid = r["order_id"]
    if r["ok"]:
        ret = inv.process_return(oid, "SKU-KEYBOARD", 1, restockable=True,
                                 reason="customer changed mind")
        print(f"  restocked at {ret['warehouse_id']}, available now {ret.get('available_now')}")
        ret2 = inv.process_return(oid, "SKU-KEYBOARD", 1, restockable=False,
                                  reason="water damage")
        print(f"  damaged return: restocked={ret2['restocked']} ({ret2.get('note','')})")
    else:
        print(f"  skipped, order failed: {r['error']}")

    final = inv.stock_grid()
    print(f"\nSTOCK HEALTH (final): {final['by_state']}")
    print(f"transfers={len(inv.list_transfers(50))} receipts={len(inv.list_receipts(50))}")
    await maps.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
