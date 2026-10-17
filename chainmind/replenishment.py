"""Autonomous stock replenishment.

The fulfilment pipeline reacts to orders. This runs *ahead* of them: it scans
for nodes below their reorder point, finds real surplus elsewhere, prices the
move against live Google road distances, and lets an agent decide which
transfers are worth making.

The agent's job here is genuinely a judgement call, which is why it is not
pure code: a 400 km truck to refill a darkstore by 12 units is technically a
valid fix and operationally stupid. Consolidating two needs onto one donor,
or declining to move at all and flagging a purchase instead, is the decision
we want a model to make. Everything it decides is then validated and executed
by code, and no transfer can move stock a donor does not actually have.
"""
from __future__ import annotations

import json
import time
from typing import Any

from chainmind.agents import llm, schemas
from chainmind.core.bus import bus
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer
from chainmind.services import inventory, maps

MAX_TRANSFERS_PER_RUN = 6

REPLENISH_SYSTEM = """You are ChainMind's inventory planner for an Indian distribution network.

Some warehouses have fallen to or below their reorder point. You are given, for each
need, the real surplus available at other nodes and the real road distance and cost of
moving it, already computed from Google Maps.

Decide which transfers to actually make.

Judgement rules:
- Prefer the nearest donor with enough surplus. Cost scales with distance.
- Do NOT move stock for a trivial shortfall if the transfer costs more than it is
  worth. Leave it and say so.
- Never take a donor below its own target stock - `can_send` already accounts for that.
- Serve `critical` urgency (zero available) before `normal`.
- One donor may serve several needs; prefer consolidating onto one donor.
- If no node has surplus, the network itself is short: put it in
  `purchase_suggestions` for the supplier, not in `transfers`.
- `qty` must never exceed that donor's `can_send`.

`summary` is for a warehouse manager, in plain operational English, max 35 words.
No jargon, no mention of models or tools."""


def _fallback_plan(needs_ctx: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministic planner: nearest donor that can fully cover each need."""
    transfers, purchases = [], []
    for n in needs_ctx:
        donors = n["donors"]
        if not donors:
            purchases.append({"sku": n["sku"], "qty": n["qty_needed"],
                              "why": "no surplus anywhere in the network"})
            continue
        best = min(donors, key=lambda d: d["distance_km"])
        qty = min(best["can_send"], n["qty_needed"])
        if qty <= 0:
            continue
        transfers.append({
            "from_warehouse": best["warehouse_id"], "to_warehouse": n["warehouse_id"],
            "sku": n["sku"], "qty": qty,
            "reason": f"nearest surplus, {best['distance_km']}km",
        })
    return {"transfers": transfers[:MAX_TRANSFERS_PER_RUN],
            "purchase_suggestions": purchases,
            "summary": f"Heuristic plan: {len(transfers)} transfers, "
                       f"{len(purchases)} purchase suggestions."}


async def scan(*, tracer: Tracer | None = None) -> dict[str, Any]:
    """Look for replenishment needs and price the options. Read-only."""
    tracer = tracer or Tracer(None)
    with tracer.measure("replenish.scan", "pipeline"):
        needs = inventory.replenishment_needs()
        if not needs:
            return {"needs": [], "context": [], "healthy": True}

        ctx: list[dict[str, Any]] = []
        for n in needs[:10]:
            donors = inventory.donor_options(n["sku"], n["qty_needed"], n["warehouse_id"])
            if donors:
                # One matrix call prices every donor for this need.
                legs = await maps.route_matrix(
                    [(d["lat"], d["lng"]) for d in donors], (n["lat"], n["lng"])
                )
                for d, leg in zip(donors, legs):
                    pricing = inventory.price_transfer(
                        leg.distance_km, d["can_send"], n["unit_weight_g"]
                    )
                    d.update(pricing)
                    d["route_source"] = leg.source
                    d.pop("lat", None)
                    d.pop("lng", None)
                donors.sort(key=lambda d: d["distance_km"])
            ctx.append({**{k: v for k, v in n.items() if k not in ("lat", "lng")},
                        "donors": donors[:4]})
        return {"needs": needs, "context": ctx, "healthy": False}


async def run(*, execute: bool = True, tracer: Tracer | None = None) -> dict[str, Any]:
    """Full replenishment cycle: scan, decide, validate, execute."""
    tracer = tracer or Tracer(None)
    t0 = time.perf_counter()
    scanned = await scan(tracer=tracer)

    if scanned["healthy"]:
        return {"ok": True, "needs": 0, "transfers_executed": [], "rejected": [],
                "purchase_suggestions": [],
                "summary": "All warehouses are at or above their reorder points.",
                "total_ms": round((time.perf_counter() - t0) * 1000, 1)}

    ctx = scanned["context"]
    res = await llm.structured(
        "planner",
        REPLENISH_SYSTEM,
        f"REPLENISHMENT NEEDS AND PRICED DONOR OPTIONS\n"
        f"{json.dumps(ctx, separators=(',', ':'))}",
        schemas.REPLENISHMENT_PLAN,
        tracer=tracer,
        span_name="agent.replenishment",
        fallback=lambda: _fallback_plan(ctx),
    )
    plan = res.data if isinstance(res.data, dict) else {}
    proposed = plan.get("transfers") or _fallback_plan(ctx)["transfers"]

    # ---- validate every proposal against live state before touching stock ----
    by_need = {(c["warehouse_id"], c["sku"]): c for c in ctx}
    executed, rejected = [], []
    for t in proposed[:MAX_TRANSFERS_PER_RUN]:
        src = str(t.get("from_warehouse", ""))
        dst = str(t.get("to_warehouse", ""))
        sku = str(t.get("sku", ""))
        qty = int(t.get("qty") or 0)
        need = by_need.get((dst, sku))

        if need is None:
            rejected.append({**t, "rejected_because": f"{dst}/{sku} is not a current need"})
            continue
        donor = next((d for d in need["donors"] if d["warehouse_id"] == src), None)
        if donor is None:
            rejected.append({**t, "rejected_because": f"{src} has no surplus of {sku}"})
            continue
        if qty <= 0 or qty > donor["can_send"]:
            rejected.append({**t, "rejected_because":
                             f"qty {qty} exceeds what {src} can send ({donor['can_send']})"})
            continue

        if not execute:
            executed.append({**t, "status": "proposed_only",
                             "cost_inr": donor["cost_inr"],
                             "distance_km": donor["distance_km"]})
            continue

        out = inventory.create_transfer(
            src, dst, sku, qty,
            reason=str(t.get("reason", "replenishment")),
            distance_km=donor["distance_km"],
            eta_minutes=donor["eta_minutes"],
            cost_inr=donor["cost_inr"],
            decided_by=res.model,
            rationale=str(t.get("reason", "")),
        )
        if out["ok"]:
            executed.append({**out, "reason": t.get("reason", ""),
                             "distance_km": donor["distance_km"]})
            bus.publish("replenish", {"transfer_id": out["transfer_id"], "sku": sku,
                                      "from": src, "to": dst, "qty": qty})
        else:
            rejected.append({**t, "rejected_because": out["error"]})

    get_db().log_event("inventory", "replenishment_run",
                       payload={"executed": len(executed), "rejected": len(rejected)})

    return {
        "ok": True,
        "needs": len(scanned["needs"]),
        "transfers_executed": executed,
        "rejected": rejected,
        "purchase_suggestions": plan.get("purchase_suggestions", []),
        "summary": plan.get("summary", ""),
        "model": res.model,
        "degraded": res.degraded,
        "total_ms": round((time.perf_counter() - t0) * 1000, 1),
    }
