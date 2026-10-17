"""The closed feedback loop.

Two tiers, because "a feedback loop" that only writes a log is not a loop:

TIER 1 - statistical, every delivery, no LLM (microseconds)
    Each completed delivery updates the carrier's `eta_bias` with an EWMA of
    actual/predicted duration, plus its on-time and damage rates. `quote_carriers`
    reads `eta_bias` on the very next order, so one late delivery measurably
    changes the next plan. This is the part you can *prove* on stage: run the
    demo twice and the ETA shrinks toward reality.

TIER 2 - semantic, batched, LLM (gemini)
    Periodically the reflection agent reads recent outcomes and distils them into
    natural-language policies stored in `policies`. The planner's prompt includes
    those policies on every subsequent run, so learned judgement ("DroneX is
    unreliable to Coimbatore after 18:00") re-enters the decision, not just a
    number.

Both tiers are observable at GET /feedback.
"""
from __future__ import annotations

import json
import time
from typing import Any

from chainmind.agents import llm, schemas
from chainmind.core.bus import bus
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer

EWMA_ALPHA = 0.35          # how fast we trust new evidence
ON_TIME_GRACE = 1.10       # within 10% of promise counts as on time


def record_delivery(
    shipment_id: str,
    actual_minutes: float,
    *,
    damaged: bool = False,
    notes: str = "",
) -> dict[str, Any]:
    """TIER 1. Ground truth arrives -> the next plan is already different."""
    db = get_db()
    ship = db.one("SELECT * FROM shipments WHERE id=?", (shipment_id,))
    if not ship:
        return {"ok": False, "error": f"unknown shipment {shipment_id}"}
    if ship["actual_minutes"] is not None:
        return {"ok": True, "idempotent_replay": True, "shipment_id": shipment_id}

    predicted = float(ship["eta_minutes"]) or 1.0
    actual = float(actual_minutes)
    ratio = max(0.2, min(5.0, actual / predicted))
    on_time = actual <= predicted * ON_TIME_GRACE
    carrier = ship["carrier"]

    stats = db.one("SELECT * FROM carrier_stats WHERE carrier=?", (carrier,))
    if stats is None:
        db.execute(
            "INSERT INTO carrier_stats (carrier,shipments,on_time,eta_bias,damage_rate,updated_at)"
            " VALUES (?,0,0,1.0,0.0,?)",
            (carrier, time.time()),
        )
        stats = db.one("SELECT * FROM carrier_stats WHERE carrier=?", (carrier,))

    n = int(stats["shipments"])
    old_bias = float(stats["eta_bias"])
    # EWMA, but the first sample is trusted more than a 0.35 nudge would allow,
    # otherwise a cold-start carrier takes ~10 deliveries to say anything.
    alpha = EWMA_ALPHA if n else 0.6
    new_bias = round(max(0.5, min(3.0, (1 - alpha) * old_bias + alpha * ratio)), 4)
    new_damage = round(
        ((float(stats["damage_rate"]) * n) + (1.0 if damaged else 0.0)) / (n + 1), 4
    )

    with db.tx() as conn:
        conn.execute(
            "UPDATE shipments SET actual_minutes=?, status=? WHERE id=?",
            (actual, "damaged" if damaged else "delivered", shipment_id),
        )
        conn.execute(
            "UPDATE carrier_stats SET shipments=shipments+1, on_time=on_time+?,"
            " eta_bias=?, damage_rate=?, updated_at=? WHERE carrier=?",
            (1 if on_time else 0, new_bias, new_damage, time.time(), carrier),
        )
        conn.execute(
            "UPDATE orders SET status=?, updated_at=? WHERE id=?",
            ("delivered" if not damaged else "delivered_damaged", time.time(), ship["order_id"]),
        )

    db.log_event(
        "feedback", "delivery_recorded", order_id=ship["order_id"],
        payload={"shipment_id": shipment_id, "carrier": carrier, "predicted": predicted,
                 "actual": actual, "on_time": on_time, "eta_bias": new_bias},
    )
    out = {
        "ok": True,
        "shipment_id": shipment_id,
        "carrier": carrier,
        "predicted_minutes": round(predicted, 1),
        "actual_minutes": round(actual, 1),
        "error_pct": round((actual - predicted) / predicted * 100, 1),
        "on_time": on_time,
        "damaged": damaged,
        "eta_bias": {"before": round(old_bias, 4), "after": new_bias},
        "damage_rate": new_damage,
        "learning_applied_to": "quote_carriers() on the next order",
        "notes": notes,
    }
    bus.publish("feedback", out)
    return out


REFLECT_SYSTEM = """You are ChainMind's reflection agent. You review recent delivery
outcomes and distil *actionable routing policies* that the fulfilment planner will
read on every future order.

Rules:
- Only state a policy that the data actually supports. No speculation.
- A policy must change a decision. "Monitor carriers" is useless;
  "prefer BlueDart over Ekart for fragile goods above 5kg" is a policy.
- `policy_key` is stable snake_case so repeat runs update rather than duplicate.
- confidence reflects sample size: <3 shipments => max 0.4.
- At most 4 insights. `rule` max 25 words."""


async def reflect(*, limit: int = 40, tracer: Tracer | None = None) -> dict[str, Any]:
    """TIER 2. Batched semantic learning -> policies the planner reads."""
    db = get_db()
    tracer = tracer or Tracer(None)

    rows = db.query(
        """
        SELECT s.carrier, s.eta_minutes, s.actual_minutes, s.distance_km, s.cost_inr,
               s.status, s.warehouse_id, s.packaging, o.priority, o.dest_label
        FROM shipments s JOIN orders o ON o.id = s.order_id
        WHERE s.actual_minutes IS NOT NULL
        ORDER BY s.created_at DESC LIMIT ?
        """,
        (limit,),
    )
    if len(rows) < 2:
        return {"ok": True, "skipped": "need at least 2 completed deliveries",
                "completed_deliveries": len(rows), "policies": db.get_policies()}

    outcomes = []
    for r in rows:
        pack = json.loads(r["packaging"] or "{}")
        outcomes.append({
            "carrier": r["carrier"],
            "priority": r["priority"],
            "distance_km": round(r["distance_km"], 1),
            "predicted_min": round(r["eta_minutes"], 1),
            "actual_min": round(r["actual_minutes"], 1),
            "error_pct": round((r["actual_minutes"] - r["eta_minutes"]) / max(1, r["eta_minutes"]) * 100, 1),
            "status": r["status"],
            "warehouse": r["warehouse_id"],
            "fragile": pack.get("fragile", False),
            "weight_g": pack.get("gross_weight_g"),
            "destination": r["dest_label"],
        })

    stats = [dict(r) for r in db.query("SELECT * FROM carrier_stats ORDER BY carrier")]

    res = await llm.structured(
        "planner",   # reflection is batch work: the cheap model is fine
        REFLECT_SYSTEM,
        f"CARRIER STATS\n{json.dumps(stats, default=str)}\n\n"
        f"RECENT DELIVERY OUTCOMES ({len(outcomes)})\n{json.dumps(outcomes)}",
        schemas.FEEDBACK_INSIGHT,
        tracer=tracer,
        span_name="agent.reflect",
        fallback=lambda: _statistical_insights(stats, outcomes),
        temperature=0.2,
    )
    data = res.data if isinstance(res.data, dict) else {}
    insights = data.get("insights") or _statistical_insights(stats, outcomes)["insights"]

    written = []
    for ins in insights[:4]:
        key = str(ins.get("policy_key") or "").strip() or "unnamed_policy"
        db.upsert_policy(
            key,
            {"rule": ins.get("rule", "")},
            confidence=float(ins.get("confidence") or 0.4),
            samples=len(outcomes),
            note=f"learned {time.strftime('%Y-%m-%d %H:%M')}",
        )
        written.append({"policy_key": key, "rule": ins.get("rule"),
                        "confidence": ins.get("confidence")})

    out = {
        "ok": True,
        "completed_deliveries": len(outcomes),
        "summary": data.get("summary", ""),
        "policies_written": written,
        "model": res.model,
        "degraded": res.degraded,
        "latency_ms": round(res.latency_ms, 1),
        "active_policies": db.get_policies(),
    }
    db.log_event("feedback", "reflection_complete", payload={"written": len(written)})
    bus.publish("reflect", {"policies_written": len(written), "summary": out["summary"]})
    return out


def _statistical_insights(stats: list[dict], outcomes: list[dict]) -> dict[str, Any]:
    """Deterministic reflection: real rules from the numbers, no model needed."""
    insights = []
    by_carrier: dict[str, list[dict]] = {}
    for o in outcomes:
        by_carrier.setdefault(o["carrier"], []).append(o)

    for carrier, items in sorted(by_carrier.items()):
        avg_err = sum(i["error_pct"] for i in items) / len(items)
        conf = min(0.4 + 0.1 * len(items), 0.9) if len(items) >= 3 else 0.4
        if avg_err > 15:
            insights.append({
                "policy_key": f"eta_inflation_{carrier.lower()}",
                "rule": f"{carrier} runs {abs(round(avg_err))}% late on average; inflate its ETA "
                        f"and avoid it for express orders.",
                "confidence": conf,
            })
        elif avg_err < -15:
            insights.append({
                "policy_key": f"eta_optimistic_{carrier.lower()}",
                "rule": f"{carrier} beats its promise by {abs(round(avg_err))}%; prefer it when "
                        f"ETA is the binding constraint.",
                "confidence": conf,
            })

    damaged = [s for s in stats if float(s.get("damage_rate") or 0) > 0.1]
    for s in damaged:
        insights.append({
            "policy_key": f"damage_risk_{str(s['carrier']).lower()}",
            "rule": f"{s['carrier']} damage rate {round(float(s['damage_rate']) * 100)}%; "
                    f"do not assign fragile goods.",
            "confidence": 0.5,
        })

    if not insights:
        avg = sum(o["error_pct"] for o in outcomes) / len(outcomes)
        insights.append({
            "policy_key": "network_eta_calibration",
            "rule": f"Network ETA error averages {round(avg, 1)}%; current estimates are "
                    f"well calibrated, keep cost-first selection for standard orders.",
            "confidence": 0.45,
        })

    return {
        "insights": insights[:4],
        "summary": f"Deterministic reflection over {len(outcomes)} deliveries "
                   f"across {len(by_carrier)} carriers.",
    }


def learning_curve() -> dict[str, Any]:
    """Evidence the loop is closing: prediction error over time."""
    db = get_db()
    rows = db.query(
        "SELECT carrier, eta_minutes, actual_minutes, created_at FROM shipments"
        " WHERE actual_minutes IS NOT NULL ORDER BY created_at ASC"
    )
    points = [
        {
            "n": i + 1,
            "carrier": r["carrier"],
            "predicted": round(r["eta_minutes"], 1),
            "actual": round(r["actual_minutes"], 1),
            "abs_error_pct": round(
                abs(r["actual_minutes"] - r["eta_minutes"]) / max(1, r["eta_minutes"]) * 100, 1
            ),
        }
        for i, r in enumerate(rows)
    ]
    first3 = points[:3]
    last3 = points[-3:]
    return {
        "points": points,
        "mean_abs_error_pct": round(
            sum(p["abs_error_pct"] for p in points) / len(points), 2
        ) if points else None,
        "first_3_mean_error_pct": round(sum(p["abs_error_pct"] for p in first3) / len(first3), 2) if first3 else None,
        "last_3_mean_error_pct": round(sum(p["abs_error_pct"] for p in last3) / len(last3), 2) if last3 else None,
        "improving": (
            bool(first3 and last3 and len(points) >= 4
                 and sum(p["abs_error_pct"] for p in last3) / len(last3)
                 < sum(p["abs_error_pct"] for p in first3) / len(first3))
        ),
        "carrier_bias": [dict(r) for r in db.query(
            "SELECT carrier, shipments, on_time, eta_bias, damage_rate FROM carrier_stats"
            " ORDER BY carrier")],
        "active_policies": db.get_policies(),
    }
