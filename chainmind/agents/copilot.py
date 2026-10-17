"""The conversational ops copilot.

A warehouse manager types plain English; the copilot answers by *calling the
same tools the autonomous pipeline uses*. It is not a chatbot bolted onto a
read-only view - it can actually fulfil an order, reserve stock or close the
feedback loop, and every one of those actions is traced exactly like a
pipeline action.

Conversation state is held per session id, capped so a long chat cannot grow
the prompt without bound.
"""
from __future__ import annotations

import time
from typing import Any

from chainmind.agents import llm
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer
from chainmind.feedback import learning_curve, record_delivery
from chainmind.services import logistics, maps

MAX_HISTORY_TURNS = 8
SESSION_TTL_S = 3600

COPILOT_SYSTEM = """You are ChainMind's operations copilot, talking to a logistics manager.

You have live tools over a real six-node Indian distribution network (Chennai,
Bengaluru, Hyderabad, Mumbai, Delhi, Coimbatore). Always call a tool rather than
guessing - you have no reliable memory of stock levels or routes.

Behaviour:
- Be concise and operational. Numbers, not adjectives.
- `fulfil_order` runs the complete autonomous pipeline; use it when the manager
  wants an order actually shipped, and report the chosen warehouse, carrier, ETA
  and cost afterwards.
- Before any destructive action (dispatching, releasing stock), state what you are
  about to do in the same reply.
- If a tool returns an error, say exactly what failed and what you tried instead.
- Format money as Rs.X and times in minutes.
"""

_sessions: dict[str, dict[str, Any]] = {}


def _history(session_id: str) -> list[dict[str, str]]:
    entry = _sessions.get(session_id)
    if entry and time.time() - entry["ts"] < SESSION_TTL_S:
        return entry["turns"]
    _sessions[session_id] = {"turns": [], "ts": time.time()}
    return _sessions[session_id]["turns"]


def _remember(session_id: str, role: str, text: str) -> None:
    turns = _history(session_id)
    turns.append({"role": role, "text": text[:4000]})
    del turns[: max(0, len(turns) - MAX_HISTORY_TURNS * 2)]
    _sessions[session_id]["ts"] = time.time()


def copilot_tools() -> list[llm.Tool]:
    """The same operations the pipeline and the MCP server expose."""

    async def _fulfil(order_text: str) -> dict[str, Any]:
        from chainmind.pipeline import run_order

        r = await run_order(order_text)
        ship = r.get("shipment") or {}
        return {
            "ok": r["ok"],
            "order_id": r["order_id"],
            "error": r.get("error"),
            "total_ms": r["total_ms"],
            "warehouse": (ship.get("warehouse") or {}).get("name"),
            "carrier": ship.get("carrier"),
            "eta_minutes": ship.get("eta_minutes"),
            "cost_inr": ship.get("cost_inr"),
            "distance_km": (ship.get("route") or {}).get("distance_km"),
            "shipment_id": ship.get("shipment_id"),
            "repairs": [x.get("strategy") for x in r.get("repairs", [])],
        }

    async def _route(origin_lat: float, origin_lng: float,
                     dest_lat: float, dest_lng: float) -> dict[str, Any]:
        leg = await maps.compute_route((origin_lat, origin_lng), (dest_lat, dest_lng))
        return leg.to_dict()

    async def _validate(address: str) -> dict[str, Any]:
        return (await maps.validate_address(address)).to_dict()

    return [
        llm.Tool("network_status",
                 "Live warehouses, their stock totals, and learned carrier statistics.",
                 {"type": "OBJECT", "properties": {}},
                 lambda: logistics.network_snapshot()),
        llm.Tool("search_inventory",
                 "Warehouses holding available stock of one SKU.",
                 {"type": "OBJECT",
                  "properties": {"sku": {"type": "STRING"}, "qty": {"type": "INTEGER"}},
                  "required": ["sku"]},
                 lambda sku, qty=1: {"warehouses": logistics.search_inventory(sku, int(qty))}),
        llm.Tool("order_status",
                 "Status of one order and its shipment.",
                 {"type": "OBJECT", "properties": {"order_id": {"type": "STRING"}},
                  "required": ["order_id"]},
                 lambda order_id: logistics.get_order_snapshot(order_id)),
        llm.Tool("fulfil_order",
                 "Run the complete autonomous fulfilment pipeline on a free-text order "
                 "and actually dispatch it.",
                 {"type": "OBJECT", "properties": {"order_text": {"type": "STRING"}},
                  "required": ["order_text"]},
                 _fulfil),
        llm.Tool("compute_route",
                 "Google Maps traffic-aware driving route between two coordinates.",
                 {"type": "OBJECT",
                  "properties": {"origin_lat": {"type": "NUMBER"}, "origin_lng": {"type": "NUMBER"},
                                 "dest_lat": {"type": "NUMBER"}, "dest_lng": {"type": "NUMBER"}},
                  "required": ["origin_lat", "origin_lng", "dest_lat", "dest_lng"]},
                 _route),
        llm.Tool("validate_address",
                 "Validate and geocode a delivery address before dispatch.",
                 {"type": "OBJECT", "properties": {"address": {"type": "STRING"}},
                  "required": ["address"]},
                 _validate),
        llm.Tool("record_delivery",
                 "Close the feedback loop by reporting a shipment's actual delivery time.",
                 {"type": "OBJECT",
                  "properties": {"shipment_id": {"type": "STRING"},
                                 "actual_minutes": {"type": "NUMBER"},
                                 "damaged": {"type": "BOOLEAN"}},
                  "required": ["shipment_id", "actual_minutes"]},
                 lambda shipment_id, actual_minutes, damaged=False: record_delivery(
                     shipment_id, float(actual_minutes), damaged=bool(damaged))),
        llm.Tool("learning_state",
                 "What the system has learned: carrier ETA bias, error trend, active policies.",
                 {"type": "OBJECT", "properties": {}},
                 lambda: learning_curve()),
    ]


async def chat(message: str, *, session_id: str = "default") -> dict[str, Any]:
    tracer = Tracer(None)
    history = _history(session_id)
    transcript = "\n".join(f"{t['role'].upper()}: {t['text']}" for t in history[-MAX_HISTORY_TURNS:])
    prompt = (f"CONVERSATION SO FAR\n{transcript}\n\n" if transcript else "") + \
             f"MANAGER: {message}"

    res = await llm.tool_loop(
        "chat",
        COPILOT_SYSTEM,
        prompt,
        copilot_tools(),
        tracer=tracer,
        span_name="agent.copilot",
        max_turns=4,   # each turn is an API call; free-tier budget is scarce
        fallback_text=_offline_reply(message),
    )
    _remember(session_id, "manager", message)
    _remember(session_id, "copilot", res.text or "")

    get_db().log_event("copilot", "turn", payload={
        "session": session_id, "tools": [c["tool"] for c in res.tool_calls],
        "turns": res.turns, "ms": round(res.latency_ms, 1),
    })
    return {
        "reply": res.text or _offline_reply(message),
        "tool_calls": res.tool_calls,
        "turns": res.turns,
        "model": res.model,
        "latency_ms": round(res.latency_ms, 1),
        "degraded": res.degraded,
        "degraded_reason": res.note,
        "trace": tracer.summary(),
        "session_id": session_id,
    }


def _offline_reply(message: str) -> str:
    """Still useful with no LLM: answer from the database directly."""
    snap = logistics.network_snapshot()
    lines = [f"{w['id']} {w['name']} ({w['city']}): {w['available_units']} units"
             for w in snap["warehouses"]]
    return (
        "Conversational model is offline, so here is the live network straight from the "
        "database:\n" + "\n".join(lines) +
        f"\n\nOrders: {snap['orders']}, shipments: {snap['shipments']}. "
        "Set GEMINI_API_KEY to enable the full copilot."
    )
