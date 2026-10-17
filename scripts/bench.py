"""Latency benchmark. The judged metric is end-to-end flow time, so measure it.

Reports the database floor, the per-stage breakdown, and sequential vs
concurrent order throughput.
"""
from __future__ import annotations

import asyncio
import statistics
import sys
import time

sys.path.insert(0, ".")

ORDERS = [
    "2 laptops and 5 usb-c cables to Anna Nagar, Chennai",
    "1 keyboard and 3 t-shirts to Gandhipuram, Coimbatore",
    "2 monitors to Koramangala, Bengaluru, no rush",
    "4 phones to Banjara Hills, Hyderabad urgent",
]


def pct(vals: list[float], p: float) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[min(len(s) - 1, int(len(s) * p))]


async def main() -> int:
    from chainmind.agents import llm
    from chainmind.core.db import get_db
    from chainmind.core.seed import seed
    from chainmind.pipeline import run_order
    from chainmind.services import maps

    db = get_db()
    seed(db)

    # ---- 1. datastore floor ------------------------------------------
    print("=" * 72)
    print("DATASTORE (SQLite WAL, in-process)")
    n = 2000
    t0 = time.perf_counter()
    for _ in range(n):
        db.one("SELECT warehouse_id, on_hand FROM inventory WHERE sku=? LIMIT 1", ("SKU-PHONE-PRO",))
    read_us = (time.perf_counter() - t0) / n * 1e6
    t0 = time.perf_counter()
    for i in range(200):
        db.log_event("bench", "write", payload={"i": i})
    write_us = (time.perf_counter() - t0) / 200 * 1e6
    t0 = time.perf_counter()
    for _ in range(300):
        from chainmind.services.logistics import search_inventory
        search_inventory("SKU-CABLE-USBC", 1)
    idx_us = (time.perf_counter() - t0) / 300 * 1e6
    print(f"  point read        {read_us:8.1f} us")
    print(f"  durable write     {write_us:8.1f} us")
    print(f"  indexed scan      {idx_us:8.1f} us")
    print(f"  -> a managed DB round trip is ~1-20 ms, i.e. {1000/read_us:.0f}-{20000/read_us:.0f}x this.")

    await asyncio.gather(llm.resolve_models(), maps.probe_apis())

    # ---- 2. sequential ------------------------------------------------
    print("\n" + "=" * 72)
    print("END-TO-END, SEQUENTIAL")
    times: list[float] = []
    for i, text in enumerate(ORDERS, 1):
        t0 = time.perf_counter()
        r = await run_order(text)
        wall = (time.perf_counter() - t0) * 1000
        times.append(r["total_ms"])
        kinds = r["trace"]["ms_by_kind"]
        print(f"  #{i} {'ok ' if r['ok'] else 'FAIL'} {r['total_ms']:7.0f}ms "
              f"(wall {wall:7.0f}ms)  agent={kinds.get('agent',0):6.0f} "
              f"pipeline={kinds.get('pipeline',0):6.0f} db={kinds.get('db',0):5.1f}")
    if times:
        print(f"  min {min(times):.0f}ms  median {statistics.median(times):.0f}ms  "
              f"p95 {pct(times,0.95):.0f}ms  max {max(times):.0f}ms")

    # ---- 3. concurrent ------------------------------------------------
    print("\n" + "=" * 72)
    print("END-TO-END, 4 ORDERS CONCURRENTLY")
    t0 = time.perf_counter()
    results = await asyncio.gather(*[run_order(t) for t in ORDERS], return_exceptions=True)
    wall = (time.perf_counter() - t0) * 1000
    okc = sum(1 for r in results if isinstance(r, dict) and r.get("ok"))
    print(f"  wall {wall:.0f}ms for 4 orders  ({okc}/4 ok)  "
          f"=> {wall/4:.0f}ms/order amortised, {4000/wall:.2f} orders/sec")
    seq_total = sum(times) if times else 0
    if seq_total:
        print(f"  sequential would have been {seq_total:.0f}ms -> {seq_total/wall:.2f}x speedup")

    # ---- 4. stage breakdown -------------------------------------------
    print("\n" + "=" * 72)
    print("STAGE PERCENTILES (ms)")
    for r in db.query(
        "SELECT name, kind, COUNT(*) n, AVG(duration_ms) a, MAX(duration_ms) mx"
        " FROM spans GROUP BY name, kind ORDER BY a DESC LIMIT 12"
    ):
        print(f"  {r['name']:34s} {r['kind']:9s} n={r['n']:3d} avg={r['a']:8.1f} max={r['mx']:8.1f}")

    await maps.aclose()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
