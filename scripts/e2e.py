"""End-to-end exercise of the whole system. This is the demo script.

Runs: a normal order, an express cold-chain order, an order engineered to force
self-healing, the feedback loop, the reflection agent, and the copilot.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time

sys.path.insert(0, ".")


async def main() -> int:
    from chainmind.agents import copilot, llm
    from chainmind.core.db import get_db
    from chainmind.core.seed import seed
    from chainmind.feedback import learning_curve, record_delivery, reflect
    from chainmind.pipeline import run_order
    from chainmind.services import maps

    seed(get_db())
    models, probe = await asyncio.gather(llm.resolve_models(), maps.probe_apis())
    print(f"models : {models}")
    print(f"google : {probe}\n")

    cases = [
        ("NORMAL", "2 laptops and 5 usb-c cables to Anna Nagar, Chennai"),
        ("EXPRESS COLD CHAIN", "URGENT: 10 vaccine kits to Apollo Hospital, Hyderabad. Cold chain required."),
        ("FORCES SELF-HEAL", "500 laptops to Bandra, Mumbai tomorrow"),
        ("BULKY", "Send 1 ergonomic chair and 2 monitors to Koramangala, Bengaluru. No rush."),
    ]

    results = []
    first_ship = None
    for label, text in cases:
        t0 = time.perf_counter()
        r = await run_order(text)
        wall = (time.perf_counter() - t0) * 1000
        results.append((label, r, wall))
        ship = r.get("shipment") or {}
        status = "OK " if r["ok"] else "FAIL"
        print(f"[{status}] {label:20s} {r['total_ms']:7.0f}ms  "
              f"{ship.get('carrier','-'):10s} eta={ship.get('eta_minutes','-')}min "
              f"wh={(ship.get('warehouse') or {}).get('id','-')} "
              f"route={(ship.get('route') or {}).get('source','-')}")
        if not r["ok"]:
            print(f"        error: {r['error']}")
        for rep in r.get("repairs", []):
            print(f"        HEAL[{rep['cycle']}] {rep['strategy']}: {rep.get('explanation','')}")
        print(f"        spans={r['trace']['span_count']} by_kind={r['trace']['ms_by_kind']}")
        if ship.get("shipment_id") and first_ship is None:
            first_ship = ship["shipment_id"]
        print()

    # ---- feedback loop ------------------------------------------------
    print("=" * 78)
    print("FEEDBACK LOOP")
    delivered = 0
    for label, r, _w in results:
        ship = r.get("shipment") or {}
        sid = ship.get("shipment_id")
        if not sid:
            continue
        promised = float(ship.get("eta_minutes") or 60)
        actual = promised * 1.32          # every carrier runs 32% late
        fb = record_delivery(sid, actual, damaged=False)
        delivered += 1
        print(f"  {fb['carrier']:10s} promised {fb['predicted_minutes']:6.1f}m "
              f"actual {fb['actual_minutes']:6.1f}m ({fb['error_pct']:+.1f}%)  "
              f"bias {fb['eta_bias']['before']} -> {fb['eta_bias']['after']}")

    if delivered >= 2:
        ref = await reflect()
        print(f"\n  reflection by {ref.get('model')}: {ref.get('summary','')}")
        for p in ref.get("policies_written", []):
            print(f"    [{p['policy_key']}] {p['rule']}  (conf {p['confidence']})")

    curve = learning_curve()
    print(f"\n  mean ETA error {curve['mean_abs_error_pct']}%  improving={curve['improving']}")

    # ---- prove the loop changed behaviour -----------------------------
    print("\n" + "=" * 78)
    print("RE-RUN AFTER LEARNING (same order as case 1)")
    r2 = await run_order(cases[0][1])
    s2 = r2.get("shipment") or {}
    s1 = results[0][1].get("shipment") or {}
    print(f"  before: {s1.get('carrier')} eta={s1.get('eta_minutes')}m")
    print(f"  after : {s2.get('carrier')} eta={s2.get('eta_minutes')}m  "
          f"<- ETA inflated by learned bias" if s2.get("eta_minutes", 0) > s1.get("eta_minutes", 0)
          else f"  after : {s2.get('carrier')} eta={s2.get('eta_minutes')}m")
    print(f"  planner applied policies: {(r2.get('plan') or {}).get('applied_policies')}")

    # ---- copilot ------------------------------------------------------
    print("\n" + "=" * 78)
    print("CONVERSATIONAL COPILOT")
    chat = await copilot.chat("Which warehouses have vaccine kits, and what did we learn about carrier reliability?")
    print(f"  tools: {[c['tool'] for c in chat['tool_calls']]}  turns={chat['turns']} "
          f"{chat['latency_ms']}ms {chat['model']}")
    print("  " + (chat["reply"] or "")[:600].replace("\n", "\n  "))

    # ---- timing summary -----------------------------------------------
    print("\n" + "=" * 78)
    oks = [r["total_ms"] for _l, r, _w in results if r["ok"]]
    print(f"SUMMARY  {len(oks)}/{len(results)} succeeded  "
          f"fastest={min(oks):.0f}ms  mean={sum(oks)/len(oks):.0f}ms" if oks else "SUMMARY all failed")
    await maps.aclose()
    return 0 if len(oks) == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
