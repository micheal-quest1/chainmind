"""The agent crew. Five specialists, each on the cheapest model that works.

  TriageAgent   flash-lite  free-text/voice order -> structured order
  PlannerAgent  flash       choose warehouse + carrier from scored candidates
  CriticAgent   no LLM      hard-constraint validator (microseconds, not ms)
  HealerAgent   pro         only runs when the critic rejects or a tool fails
  QAAgent       flash       multimodal parcel photo inspection

The critic being *code* rather than a model is deliberate: constraint checking
is exactly the work an LLM is worst and slowest at, and it is the gate that
makes the planner's output safe to execute.
"""
from __future__ import annotations

import json
import re
from typing import Any

from chainmind.agents import llm, schemas
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer
from chainmind.services import logistics

CATALOG_SKUS = [
    "SKU-LAPTOP-14", "SKU-PHONE-PRO", "SKU-MONITOR-27", "SKU-KEYBOARD",
    "SKU-CABLE-USBC", "SKU-CHAIR-ERGO", "SKU-VACCINE-KIT", "SKU-TSHIRT-M",
]

_SKU_HINTS = {
    "SKU-LAPTOP-14": ["laptop", "notebook", "macbook", "ultrabook"],
    "SKU-PHONE-PRO": ["phone", "mobile", "smartphone", "handset", "iphone"],
    "SKU-MONITOR-27": ["monitor", "display", "screen"],
    "SKU-KEYBOARD": ["keyboard", "keybord"],
    "SKU-CABLE-USBC": ["cable", "usb", "usb-c", "charger", "cord"],
    "SKU-CHAIR-ERGO": ["chair", "seating", "ergonomic"],
    "SKU-VACCINE-KIT": ["vaccine", "vial", "cold chain", "medical kit", "medicine"],
    "SKU-TSHIRT-M": ["tshirt", "t-shirt", "shirt", "apparel", "tee"],
}


# ================================================================== 1. TRIAGE
TRIAGE_SYSTEM = f"""You are the intake agent for ChainMind, a supply-chain orchestrator.
Convert a customer's free-text order into a structured order.

Catalogue (use these SKUs EXACTLY, never invent one):
{json.dumps(CATALOG_SKUS)}

Rules:
- Map each requested product to the closest catalogue SKU. If nothing matches, put the
  raw request string in `unmatched_requests` and do NOT invent a SKU.
- Default qty to 1 when unstated. Default priority to "standard".
- priority = "express" only for explicit urgency (today, urgent, ASAP, same-day, emergency)
  or medical/cold-chain goods.
- cold_chain = true for SKU-VACCINE-KIT or any temperature-sensitive request.
- `destination` = the delivery address exactly as the customer stated it.
- Be terse. `reasoning` is one sentence, max 25 words."""


def _mock_triage(raw: str) -> dict[str, Any]:
    """Keyword intake used when Gemini is unavailable. Same output shape."""
    low = raw.lower()
    lines: list[dict[str, Any]] = []
    for sku, hints in _SKU_HINTS.items():
        for h in hints:
            if h in low:
                m = re.search(rf"(\d+)\s*(?:x\s*)?[\w\- ]{{0,12}}{re.escape(h)}", low)
                qty = int(m.group(1)) if m else 1
                lines.append({"sku": sku, "qty": max(1, min(qty, 500))})
                break
    if not lines:
        lines = [{"sku": "SKU-CABLE-USBC", "qty": 1}]

    dest = "Chennai"
    m = re.search(r"\bto\s+([A-Za-z0-9,\.\-\s]{3,60})", raw, re.I)
    if m:
        dest = m.group(1).strip().rstrip(".")
    express = any(w in low for w in ("urgent", "asap", "today", "same-day", "same day",
                                     "emergency", "immediately", "rush"))
    cold = any(w in low for w in ("vaccine", "cold chain", "cold-chain", "refrigerat"))
    return {
        "lines": lines,
        "destination": dest,
        "priority": "express" if (express or cold) else "standard",
        "cold_chain": cold,
        "customer_note": "",
        "unmatched_requests": [],
        "confidence": 0.62,
        "reasoning": "Deterministic keyword intake (LLM offline).",
    }


async def triage(raw_order: str, *, tracer: Tracer) -> tuple[dict[str, Any], llm.LLMResult]:
    res = await llm.structured(
        "triage",
        TRIAGE_SYSTEM,
        f"Customer order:\n\"\"\"{raw_order}\"\"\"",
        schemas.ORDER_INTAKE,
        tracer=tracer,
        span_name="agent.triage",
        fallback=lambda: _mock_triage(raw_order),
    )
    data = res.data if isinstance(res.data, dict) else {}
    # Defence in depth: the schema guarantees shape, we still validate semantics.
    clean_lines = []
    for ln in data.get("lines") or []:
        sku = str(ln.get("sku", "")).upper().strip()
        if sku in CATALOG_SKUS:
            clean_lines.append({"sku": sku, "qty": max(1, int(ln.get("qty") or 1))})
    if not clean_lines:
        data = _mock_triage(raw_order)
        res.degraded = True
        res.note = (res.note + " | LLM returned no valid SKU, fell back to keyword intake").strip(" |")
        clean_lines = data["lines"]
    data["lines"] = clean_lines
    data.setdefault("destination", "Chennai")
    data.setdefault("priority", "standard")
    data["priority"] = data["priority"] if data["priority"] in ("standard", "express") else "standard"
    return data, res


# ================================================================= 2. PLANNER
PLANNER_SYSTEM = """You are the fulfilment planner for ChainMind.

You are given candidate warehouses that ALREADY have every SKU in stock, each with a
real Google Maps driving distance and live-traffic duration to the customer, plus
carrier quotes whose ETAs are already corrected by learned historical bias.

Choose exactly one warehouse_id and one carrier.

Decision policy:
- priority "express": minimise total ETA (pack_minutes + carrier eta_minutes).
- priority "standard": minimise cost, but never accept an ETA more than 35% worse
  than the fastest feasible option.
- NEVER choose a carrier whose `feasible` is false.
- Respect every learned policy supplied under LEARNED POLICIES; name the ones you
  used in `applied_policies`.
- Flag genuine risks in `risk_flags` (e.g. near cutoff hour, single-source SKU,
  carrier with elevated damage rate).

`rationale` max 40 words and must cite the actual numbers you compared."""


def _score(cand: dict[str, Any], priority: str) -> float:
    eta = cand["pack_minutes"] + cand["best_carrier"]["eta_minutes"]
    cost = cand["packaging_cost_inr"] + cand["best_carrier"]["cost_inr"]
    return eta + cost * 0.15 if priority == "express" else cost + eta * 0.35


def _mock_plan(candidates: list[dict[str, Any]], priority: str) -> dict[str, Any]:
    best = min(candidates, key=lambda c: _score(c, priority))
    return {
        "warehouse_id": best["warehouse_id"],
        "carrier": best["best_carrier"]["carrier"],
        "rationale": (
            f"Heuristic optimiser: {best['warehouse_id']} at {best['distance_km']}km, "
            f"ETA {round(best['pack_minutes'] + best['best_carrier']['eta_minutes'], 1)}min, "
            f"cost Rs.{round(best['packaging_cost_inr'] + best['best_carrier']['cost_inr'], 2)}."
        ),
        "risk_flags": ["llm_offline_heuristic_plan"],
        "applied_policies": [],
        "confidence": 0.6,
    }


async def plan(
    candidates: list[dict[str, Any]],
    order: dict[str, Any],
    policies: dict[str, dict],
    *,
    tracer: Tracer,
) -> tuple[dict[str, Any], llm.LLMResult]:
    policy_text = (
        "\n".join(
            f"- [{k}] {v['value'].get('rule', v['value'])} "
            f"(confidence {v['confidence']:.2f}, from {v['samples']} shipments)"
            for k, v in policies.items()
        )
        or "- none learned yet"
    )
    prompt = (
        f"ORDER\npriority: {order['priority']}\nlines: {json.dumps(order['lines'])}\n"
        f"destination: {order['destination']}\ncold_chain: {order.get('cold_chain', False)}\n\n"
        f"LEARNED POLICIES\n{policy_text}\n\n"
        f"CANDIDATES\n{json.dumps(candidates, separators=(',', ':'))}"
    )
    res = await llm.structured(
        "planner",
        PLANNER_SYSTEM,
        prompt,
        schemas.FULFILLMENT_PLAN,
        tracer=tracer,
        span_name="agent.planner",
        fallback=lambda: _mock_plan(candidates, order["priority"]),
    )
    data = res.data if isinstance(res.data, dict) else {}
    if not data.get("warehouse_id") or not data.get("carrier"):
        data = _mock_plan(candidates, order["priority"])
        res.degraded = True
    return data, res


# ================================================================== 3. CRITIC
def critique(plan_obj: dict[str, Any], candidates: list[dict[str, Any]],
             order: dict[str, Any]) -> dict[str, Any]:
    """Deterministic hard-constraint gate. Runs in microseconds, never wrong.

    This is the in-run feedback loop: a rejection here sends the plan to the
    healer rather than letting a bad plan reach the warehouse.
    """
    violations: list[str] = []
    by_id = {c["warehouse_id"]: c for c in candidates}
    cand = by_id.get(plan_obj.get("warehouse_id", ""))

    if cand is None:
        violations.append(
            f"warehouse '{plan_obj.get('warehouse_id')}' is not a valid candidate "
            f"(valid: {sorted(by_id)})"
        )
        return {"approved": False, "violations": violations, "candidate": None}

    carrier = plan_obj.get("carrier", "")
    quote = next((q for q in cand["carrier_quotes"] if q["carrier"] == carrier), None)
    if quote is None:
        violations.append(f"carrier '{carrier}' was not quoted for {cand['warehouse_id']}")
    elif not quote["feasible"]:
        violations.append(f"carrier '{carrier}' infeasible: {'; '.join(quote['blocked_because'])}")

    if order.get("cold_chain") and quote and quote["feasible"]:
        from chainmind.services.logistics import CARRIER_PROFILES

        if not CARRIER_PROFILES.get(carrier, (0, 0, 0, 0, False, False))[5]:
            violations.append(f"cold-chain order assigned to non-cold-chain carrier '{carrier}'")

    if quote and quote["feasible"]:
        feasible = [q for q in cand["carrier_quotes"] if q["feasible"]]
        fastest = min((q["eta_minutes"] for q in feasible), default=quote["eta_minutes"])
        if order["priority"] == "express" and quote["eta_minutes"] > fastest * 1.5:
            violations.append(
                f"express order given a carrier {round(quote['eta_minutes'] / fastest, 2)}x "
                f"slower than the fastest feasible option"
            )

    return {
        "approved": not violations,
        "violations": violations,
        "candidate": cand,
        "quote": quote,
    }


# ================================================================== 4. HEALER
HEALER_SYSTEM = """You are ChainMind's recovery agent. A fulfilment attempt just failed.

You have tools to inspect live inventory, re-quote carriers and check routes.
Investigate, then return ONE repair strategy:

- reroute_warehouse : another warehouse has the stock -> give warehouse_id
- switch_carrier    : same warehouse, a feasible carrier -> give carrier
- split_shipment    : no single warehouse has everything
- downgrade_priority: express is impossible, standard is achievable
- partial_fulfil    : ship what exists now -> list drop_skus
- escalate_human    : nothing is shippable; say precisely what is missing

Use the tools before deciding. `explanation` max 40 words, cite real numbers."""


def _healer_tools() -> list[llm.Tool]:
    return [
        llm.Tool(
            name="search_inventory",
            description="Warehouses with available stock for one SKU and quantity.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "sku": {"type": "STRING"},
                    "qty": {"type": "INTEGER"},
                },
                "required": ["sku", "qty"],
            },
            handler=lambda sku, qty=1: {"candidates": logistics.search_inventory(sku, int(qty))},
        ),
        llm.Tool(
            name="quote_carriers",
            description="Feasible carriers with cost and learned-bias-corrected ETA.",
            parameters={
                "type": "OBJECT",
                "properties": {
                    "distance_km": {"type": "NUMBER"},
                    "drive_minutes": {"type": "NUMBER"},
                    "gross_weight_g": {"type": "INTEGER"},
                    "fragile": {"type": "BOOLEAN"},
                    "cold_chain": {"type": "BOOLEAN"},
                    "priority": {"type": "STRING", "enum": ["standard", "express"]},
                },
                "required": ["distance_km", "drive_minutes", "gross_weight_g"],
            },
            handler=lambda distance_km, drive_minutes, gross_weight_g, fragile=False,
            cold_chain=False, priority="standard": {
                "quotes": logistics.quote_carriers(
                    float(distance_km), float(drive_minutes), int(gross_weight_g),
                    fragile=bool(fragile), cold_chain=bool(cold_chain), priority=priority,
                )
            },
        ),
        llm.Tool(
            name="network_snapshot",
            description="All warehouses, their cities, coordinates and total available units.",
            parameters={"type": "OBJECT", "properties": {}},
            handler=lambda: logistics.network_snapshot(),
        ),
    ]


def _stock_facts(lines: list[dict[str, Any]]) -> str:
    """A compact, authoritative stock table for the healer's prompt."""
    if not lines:
        return "(no order lines)"
    out = []
    for ln in lines:
        sku, qty = ln["sku"], int(ln["qty"])
        full = logistics.search_inventory(sku, qty)
        any_stock = logistics.search_inventory(sku, 1)
        if full:
            where = ", ".join(f"{c['warehouse_id']}={c['available']}" for c in full)
            out.append(f"- {sku} x{qty}: CAN be served by {where}")
        elif any_stock:
            where = ", ".join(f"{c['warehouse_id']}={c['available']}" for c in any_stock)
            best = max(c["available"] for c in any_stock)
            out.append(
                f"- {sku} x{qty}: CANNOT be served in full. Largest single location holds "
                f"{best}. Available: {where}"
            )
        else:
            out.append(f"- {sku} x{qty}: OUT OF STOCK everywhere")
    return "\n".join(out)


async def heal(
    failure: str,
    order: dict[str, Any],
    context: dict[str, Any],
    *,
    tracer: Tracer,
) -> tuple[dict[str, Any], llm.LLMResult]:
    """Two-phase recovery: agentic investigation, then a structured decision.

    Phase 1 is a tool loop (the agent gathers facts). Phase 2 is schema-bound so
    the output is directly executable by the pipeline - no prose to parse.
    """
    # Hand the healer the live stock picture up front. Two reasons: it stops the
    # model claiming it has no data when a tool call returns nothing useful, and
    # it means the common cases need zero tool round trips - which matters,
    # because every recovery turn is latency the customer is waiting through.
    stock_facts = _stock_facts(order.get("lines") or [])
    brief = (
        f"FAILURE: {failure}\n\n"
        f"ORDER: {json.dumps(order)}\n\n"
        f"LIVE STOCK (authoritative, read from the database just now):\n{stock_facts}\n\n"
        f"CONTEXT: {json.dumps(context)[:1800]}"
    )

    probe = await llm.tool_loop(
        "repair",
        HEALER_SYSTEM,
        brief + "\n\nIf the stock picture above already answers the question, say so in one "
                "line and call no tools. Only call a tool if something is genuinely missing.",
        _healer_tools(),
        tracer=tracer,
        span_name="agent.healer.investigate",
        max_turns=2,
    )

    decision = await llm.structured(
        "repair",
        HEALER_SYSTEM,
        brief + f"\n\nYOUR INVESTIGATION FINDINGS:\n{probe.text or '(no extra lookup needed)'}\n\n"
        f"TOOLS YOU CALLED: {json.dumps(probe.tool_calls)[:900]}\n\n"
        "Now commit to one repair strategy. Base it on the LIVE STOCK numbers above - "
        "they are accurate. Never claim you lack data.",
        schemas.REPAIR_PLAN,
        tracer=tracer,
        span_name="agent.healer.decide",
        fallback=lambda: _mock_heal(failure, order, context),
        temperature=0.1,
    )
    data = decision.data if isinstance(decision.data, dict) else {}
    if not data.get("strategy"):
        data = _mock_heal(failure, order, context)
        decision.degraded = True
    data["investigation"] = probe.text
    data["tools_used"] = [c["tool"] for c in probe.tool_calls]
    return data, decision


def _mock_heal(failure: str, order: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
    """Deterministic recovery ladder, mirroring the agent's own options."""
    fulfil = context.get("fulfillment") or {}
    singles = fulfil.get("single_warehouse_options") or []
    tried = set(context.get("tried_warehouses") or [])
    alt = next((w["warehouse_id"] for w in singles if w["warehouse_id"] not in tried), None)
    if alt:
        return {"strategy": "reroute_warehouse", "warehouse_id": alt,
                "explanation": f"Rerouting to {alt}; previous warehouse failed ({failure[:60]}).",
                "confidence": 0.7}
    if fulfil.get("requires_split"):
        return {"strategy": "split_shipment",
                "explanation": "No single warehouse holds the full order; splitting.", "confidence": 0.65}
    if fulfil.get("unfulfillable_skus"):
        return {"strategy": "partial_fulfil", "drop_skus": fulfil["unfulfillable_skus"],
                "explanation": f"Dropping out-of-stock SKUs {fulfil['unfulfillable_skus']}.",
                "confidence": 0.6}
    if order.get("priority") == "express":
        return {"strategy": "downgrade_priority",
                "explanation": "Express constraints unmet; downgrading to standard.", "confidence": 0.55}
    return {"strategy": "escalate_human",
            "explanation": f"No automated recovery available: {failure[:80]}", "confidence": 0.5}


# ====================================================================== 5. QA
QA_SYSTEM = """You are ChainMind's parcel QA inspector at the packing station.
Inspect the photo of a sealed parcel before it is handed to the carrier.

- damage: crushed corners, punctures, water staining, tape failure, bulging
- label: is the shipping label fully visible and machine-scannable? Transcribe all
  visible text and barcode digits into `extracted_text`.
- verdict: "pass" (ship it), "repack" (fixable), "reject" (contents likely damaged)
- severity: 0.0 pristine -> 1.0 destroyed

If the image is not a parcel at all, verdict "reject", note "not a parcel".
`notes` max 30 words."""


async def inspect_parcel(image_bytes: bytes, mime_type: str, *, tracer: Tracer,
                         shipment_id: str = "") -> tuple[dict[str, Any], llm.LLMResult]:
    res = await llm.vision(
        QA_SYSTEM,
        f"Inspect this parcel for shipment {shipment_id or 'N/A'}. Return the structured verdict.",
        image_bytes,
        mime_type,
        schemas.PACKAGE_QA,
        tracer=tracer,
        fallback=lambda: {
            "verdict": "pass", "damage_detected": False, "damage_types": [],
            "label_readable": True, "extracted_text": "", "severity": 0.0,
            "notes": "Vision model offline - parcel auto-passed by policy.",
        },
    )
    return (res.data if isinstance(res.data, dict) else {}), res
