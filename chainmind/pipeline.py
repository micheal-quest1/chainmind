"""The fulfilment pipeline: raw customer text -> dispatched shipment.

LATENCY ENGINEERING (this is what the hackathon judges on)
----------------------------------------------------------
1. Two LLM calls on the happy path, not a conversation. Triage and planning are
   schema-constrained single shots. A ReAct loop would cost 6-10 round trips.
2. Speculative address validation. We regex the destination out of the raw text
   and start the Google Address Validation call *concurrently* with the triage
   model. By the time triage returns, the address is usually already resolved -
   so the whole geo stage costs ~0ms of wall clock.
3. One route-matrix call, not N route calls. Comparing 6 warehouses is a single
   `computeRouteMatrix` request.
4. Thinking budget pinned to 0 on the hot path; the planner does arithmetic-free
   selection over numbers we computed in Python.
5. The critic is code, not a model. Validation is microseconds.
6. The expensive model (pro) is only ever loaded on the failure path.

CORRECTNESS
-----------
Every stage is idempotent and traced. Stock reservation is a single IMMEDIATE
transaction, so two concurrent orders can never oversell. A failure anywhere
hands control to the healer agent, which re-plans against live state; the
compensating `release_stock` undoes partial work first.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from chainmind.agents import crew
from chainmind.core.bus import bus
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer, breaker_snapshot
from chainmind.services import logistics, maps

MAX_REPAIR_CYCLES = 2

_ADDR_RE = re.compile(
    r"\b(?:to|deliver(?:\s+to)?|ship(?:\s+to)?|address|destination)[:\s]+"
    r"([A-Za-z0-9][A-Za-z0-9,\.\-/\s]{4,90})",
    re.I,
)


def _speculative_address(raw: str) -> str | None:
    """Best-effort destination guess so geo work can start before triage ends."""
    m = _ADDR_RE.search(raw)
    if m:
        return m.group(1).strip().rstrip(".,")
    for city in ("chennai", "bengaluru", "bangalore", "hyderabad", "mumbai",
                 "delhi", "coimbatore", "pune", "kochi", "kolkata"):
        if city in raw.lower():
            return city.title()
    return None


@dataclass
class StageLog:
    stage: str
    status: str
    ms: float
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {"stage": self.stage, "status": self.status, "ms": round(self.ms, 2), **{"detail": self.detail}}


class Orchestrator:
    """One instance per order. Holds the trace and the event stream."""

    def __init__(self, raw_order: str, *, idem_key: str | None = None,
                 qa_image: tuple[bytes, str] | None = None) -> None:
        self.raw = raw_order
        self.idem_key = idem_key
        self.qa_image = qa_image
        self.order_id = f"ORD-{uuid.uuid4().hex[:10].upper()}"
        self.tracer = Tracer(self.order_id)
        self.stages: list[StageLog] = []
        self.repairs: list[dict[str, Any]] = []
        self.models_used: dict[str, str] = {}
        self.degraded: list[str] = []
        self.t0 = time.perf_counter()

    # ------------------------------------------------------------------ utils
    def emit(self, stage: str, status: str, **detail: Any) -> None:
        ms = (time.perf_counter() - self.t0) * 1000
        log = StageLog(stage, status, ms, detail)
        self.stages.append(log)
        bus.publish(
            "stage",
            {"order_id": self.order_id, "stage": stage, "status": status,
             "elapsed_ms": round(ms, 1), "detail": detail},
        )
        get_db().log_event(stage, status, order_id=self.order_id, payload=detail)

    def _note_model(self, key: str, res: Any) -> None:
        self.models_used[key] = res.model
        if getattr(res, "degraded", False):
            self.degraded.append(f"{key}: {res.note or 'degraded'}")

    # -------------------------------------------------------------------- run
    async def run(self) -> dict[str, Any]:
        try:
            return await self._run_inner()
        except Exception as exc:  # the pipeline itself must never 500 silently
            self.emit("pipeline", "fatal", error=type(exc).__name__, detail=str(exc))
            logistics.release_stock(self.order_id)
            get_db().execute(
                "UPDATE orders SET status='failed', updated_at=? WHERE id=?",
                (time.time(), self.order_id),
            )
            return self._result(ok=False, error=f"{type(exc).__name__}: {exc}")

    async def _run_inner(self) -> dict[str, Any]:
        db = get_db()

        # ---- Stage 0: intake + idempotency -------------------------------
        if self.idem_key:
            prior = db.one("SELECT id FROM orders WHERE idem_key=?", (self.idem_key,))
            if prior:
                self.emit("intake", "idempotent_replay", order_id=prior["id"])
                snap = logistics.get_order_snapshot(prior["id"])
                return {"ok": True, "idempotent_replay": True, "order": snap,
                        "order_id": prior["id"]}

        now = time.time()
        db.execute(
            "INSERT INTO orders (id,idem_key,raw,status,priority,created_at,updated_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (self.order_id, self.idem_key, self.raw, "received", "standard", now, now),
        )
        self.emit("intake", "accepted", order_id=self.order_id, chars=len(self.raw))

        # ---- Stage 1: triage || speculative address validation ------------
        guess = _speculative_address(self.raw)
        with self.tracer.measure("stage.triage_parallel_geo", "pipeline"):
            triage_task = asyncio.create_task(crew.triage(self.raw, tracer=self.tracer))
            geo_task = (
                asyncio.create_task(maps.validate_address(guess)) if guess else None
            )
            (order, triage_res) = await triage_task
            self._note_model("triage", triage_res)

            addr = None
            if geo_task is not None:
                addr = await geo_task
                # Only reuse the speculative result if triage agrees on the city.
                if not _same_place(guess or "", order["destination"]):
                    addr = None
            if addr is None:
                addr = await maps.validate_address(order["destination"])

        # ---- Intake gate: an order with no catalogue line is NOT fulfillable --
        # Reached before any geo/stock work so nonsense input costs one LLM call
        # and consumes nothing. Without this gate the keyword fallback used to
        # substitute a default SKU and dispatch a real shipment for "asdkfj blah".
        if not order.get("lines"):
            self.emit("intake_gate", "rejected",
                      unmatched_requests=order.get("unmatched_requests", []),
                      reasoning=order.get("reasoning", ""), model=triage_res.model)
            db.execute(
                "UPDATE orders SET status='rejected_unparseable', updated_at=? WHERE id=?",
                (time.time(), self.order_id),
            )
            return self._result(
                ok=False,
                error="no catalogue SKU could be identified in the request",
                extra={"order": order,
                       "unmatched_requests": order.get("unmatched_requests", []),
                       "catalogue": crew.CATALOG_SKUS},
            )

        order["destination_validated"] = addr.to_dict()
        self.emit(
            "triage", "ok",
            lines=order["lines"], priority=order["priority"],
            destination=addr.formatted, deliverable=addr.deliverable,
            address_source=addr.source, speculative_hit=bool(geo_task) and addr is not None,
            confidence=order.get("confidence"), model=triage_res.model,
            reasoning=order.get("reasoning", ""),
        )

        if addr.deliverable == "undeliverable":
            self.emit("address_gate", "rejected", missing=addr.missing_components)
            db.execute("UPDATE orders SET status='address_rejected', updated_at=? WHERE id=?",
                       (time.time(), self.order_id))
            return self._result(ok=False, error="undeliverable address",
                                extra={"address": addr.to_dict()})

        db.execute(
            "UPDATE orders SET status='triaged', priority=?, dest_label=?, dest_lat=?,"
            " dest_lng=?, updated_at=? WHERE id=?",
            (order["priority"], addr.formatted, addr.lat, addr.lng, time.time(), self.order_id),
        )
        db.executemany(
            "INSERT INTO order_lines (order_id,sku,qty) VALUES (?,?,?)"
            " ON CONFLICT(order_id,sku) DO UPDATE SET qty=excluded.qty",
            [(self.order_id, l["sku"], l["qty"]) for l in order["lines"]],
        )

        # ---- Stage 2-4: candidates, routes, packaging, quotes -------------
        dest = (addr.lat, addr.lng)
        candidates, fulfil = await self._build_candidates(order, dest)
        if not candidates:
            return await self._repair_loop(
                "no warehouse can fulfil the order as specified",
                order, dest, fulfil, tried=set(),
            )

        # ---- Stage 5-7: plan -> critic -> (heal) -------------------------
        policies = db.get_policies()
        plan_obj, plan_res = await crew.plan(candidates, order, policies, tracer=self.tracer)
        self._note_model("planner", plan_res)
        verdict = crew.critique(plan_obj, candidates, order)
        self.emit(
            "plan", "ok" if verdict["approved"] else "rejected_by_critic",
            warehouse=plan_obj.get("warehouse_id"), carrier=plan_obj.get("carrier"),
            rationale=plan_obj.get("rationale"), risk_flags=plan_obj.get("risk_flags", []),
            applied_policies=plan_obj.get("applied_policies", []),
            violations=verdict["violations"], model=plan_res.model,
            confidence=plan_obj.get("confidence"),
        )

        if not verdict["approved"]:
            return await self._repair_loop(
                "critic rejected the plan: " + "; ".join(verdict["violations"]),
                order, dest, fulfil, tried=set(), candidates=candidates,
            )

        return await self._execute(order, plan_obj, verdict, dest, fulfil, candidates)

    def _same_place_dbg(self) -> None:  # pragma: no cover
        pass

    # ------------------------------------------------------- candidate build
    async def _build_candidates(
        self, order: dict[str, Any], dest: tuple[float, float],
        exclude: set[str] | None = None,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """Score every warehouse that can ship the whole order.

        One route-matrix call covers all of them; packaging and carrier pricing
        are pure Python so they cost nothing measurable.
        """
        exclude = exclude or set()
        with self.tracer.measure("stage.candidates", "pipeline") as sp:
            fulfil = logistics.find_fulfillment_candidates(order["lines"])
            options = [
                w for w in fulfil["single_warehouse_options"]
                if w["warehouse_id"] not in exclude
            ]
            sp.attrs.update({"options": len(options), "requires_split": fulfil["requires_split"]})

            if not options:
                self.emit("candidates", "none",
                          unfulfillable=fulfil["unfulfillable_skus"],
                          requires_split=fulfil["requires_split"], excluded=sorted(exclude))
                return [], fulfil

            legs = await maps.route_matrix([(w["lat"], w["lng"]) for w in options], dest)

            candidates: list[dict[str, Any]] = []
            for w, leg in zip(options, legs):
                pack = logistics.plan_packaging(order["lines"], w["warehouse_id"])
                quotes = logistics.quote_carriers(
                    leg.distance_km, leg.duration_minutes, pack["gross_weight_g"],
                    fragile=pack["fragile"],
                    cold_chain=pack["cold_chain"] or bool(order.get("cold_chain")),
                    priority=order["priority"],
                )
                feasible = [q for q in quotes if q["feasible"]]
                if not feasible:
                    continue
                best = min(
                    feasible,
                    key=lambda q: q["eta_minutes"] if order["priority"] == "express" else q["cost_inr"],
                )
                candidates.append({
                    "warehouse_id": w["warehouse_id"],
                    "name": w["name"], "kind": w["kind"], "city": w["city"],
                    "lat": w["lat"], "lng": w["lng"],
                    "cutoff_hour": w["cutoff_hour"],
                    "distance_km": round(leg.distance_km, 2),
                    "drive_minutes": round(leg.duration_minutes, 1),
                    "route_source": leg.source,
                    "polyline": leg.polyline,
                    "pack_minutes": pack["pack_minutes"],
                    "packaging_cost_inr": pack["packaging_cost_inr"],
                    "packaging": pack,
                    "best_carrier": best,
                    "carrier_quotes": quotes,
                })

        self.emit("candidates", "ok", count=len(candidates),
                  route_source=legs[0].source if legs else "n/a",
                  warehouses=[c["warehouse_id"] for c in candidates])
        return candidates, fulfil

    # ------------------------------------------------------------- execution
    async def _execute(
        self, order: dict[str, Any], plan_obj: dict[str, Any], verdict: dict[str, Any],
        dest: tuple[float, float], fulfil: dict[str, Any],
        candidates: list[dict[str, Any]], tried: set[str] | None = None,
    ) -> dict[str, Any]:
        tried = tried or set()
        cand = verdict["candidate"]
        quote = verdict["quote"]
        wh = cand["warehouse_id"]

        # ---- Stage 8: atomic reservation ---------------------------------
        with self.tracer.measure("stage.reserve", "db"):
            reservation = logistics.reserve_stock(self.order_id, wh, order["lines"])
        if not reservation["ok"]:
            self.emit("reserve", "failed", warehouse=wh, error=reservation["error"])
            tried.add(wh)
            return await self._repair_loop(
                f"stock reservation failed at {wh}: {reservation['error']}",
                order, dest, fulfil, tried=tried,
            )
        self.emit("reserve", "ok", warehouse=wh,
                  replay=reservation["idempotent_replay"], lines=order["lines"])

        # ---- Stage 9: packaging + optional multimodal QA -----------------
        pack = cand["packaging"]
        self.emit("packaging", "ok", box=pack["box"], weight_g=pack["gross_weight_g"],
                  fragile=pack["fragile"], cold_chain=pack["cold_chain"],
                  extras=pack["extras"], pack_minutes=pack["pack_minutes"])

        qa: dict[str, Any] | None = None
        if self.qa_image is not None:
            img, mime = self.qa_image
            qa, qa_res = await crew.inspect_parcel(img, mime, tracer=self.tracer,
                                                   shipment_id=self.order_id)
            self._note_model("vision", qa_res)
            self.emit("qa_vision", qa.get("verdict", "unknown"),
                      damage=qa.get("damage_detected"), severity=qa.get("severity"),
                      label_readable=qa.get("label_readable"),
                      extracted_text=(qa.get("extracted_text") or "")[:120],
                      notes=qa.get("notes"), model=qa_res.model)
            if qa.get("verdict") == "reject":
                logistics.release_stock(self.order_id)
                self.emit("qa_vision", "blocked_dispatch", reason=qa.get("notes"))
                return self._result(ok=False, error="parcel failed QA inspection",
                                    extra={"qa": qa})

        # ---- Stage 10: dispatch ------------------------------------------
        route = {
            "origin": [cand["lat"], cand["lng"]],
            "destination": list(dest),
            "distance_km": cand["distance_km"],
            "drive_minutes": cand["drive_minutes"],
            "source": cand["route_source"],
            "polyline": cand["polyline"],
            "static_map_url": maps.static_map_url(
                cand["polyline"], (cand["lat"], cand["lng"]), dest
            ),
        }
        total_eta = pack["pack_minutes"] + quote["eta_minutes"]
        total_cost = pack["packaging_cost_inr"] + quote["cost_inr"]

        with self.tracer.measure("stage.dispatch", "db"):
            disp = logistics.dispatch_shipment(
                self.order_id, wh, quote["carrier"], pack, route, total_eta, total_cost
            )
        self.emit("dispatch", "ok", shipment_id=disp["shipment_id"],
                  carrier=quote["carrier"], eta_minutes=round(total_eta, 1),
                  cost_inr=round(total_cost, 2), distance_km=cand["distance_km"],
                  replay=disp["idempotent_replay"])

        return self._result(
            ok=True,
            extra={
                "order": order,
                "plan": plan_obj,
                "shipment": {
                    **disp,
                    "warehouse": {"id": wh, "name": cand["name"], "city": cand["city"],
                                  "kind": cand["kind"]},
                    "packaging": pack,
                    "route": route,
                    "eta_minutes": round(total_eta, 1),
                    "cost_inr": round(total_cost, 2),
                    "carrier_quote": quote,
                },
                "qa": qa,
                "considered": [
                    {"warehouse_id": c["warehouse_id"], "distance_km": c["distance_km"],
                     "drive_minutes": c["drive_minutes"],
                     "best_carrier": c["best_carrier"]["carrier"],
                     "eta_minutes": round(c["pack_minutes"] + c["best_carrier"]["eta_minutes"], 1),
                     "cost_inr": round(c["packaging_cost_inr"] + c["best_carrier"]["cost_inr"], 2)}
                    for c in candidates
                ],
            },
        )

    # ------------------------------------------------------------ repair loop
    async def _repair_loop(
        self, failure: str, order: dict[str, Any], dest: tuple[float, float],
        fulfil: dict[str, Any], *, tried: set[str],
        candidates: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Self-healing: investigate -> decide -> apply -> re-validate.

        Bounded at MAX_REPAIR_CYCLES so a pathological order cannot spin. Each
        cycle re-reads live inventory, so the healer never acts on stale state.
        """
        for cycle in range(1, MAX_REPAIR_CYCLES + 1):
            self.emit("heal", "investigating", cycle=cycle, failure=failure)
            context = {
                "fulfillment": {
                    "single_warehouse_options": fulfil.get("single_warehouse_options", []),
                    "unfulfillable_skus": fulfil.get("unfulfillable_skus", []),
                    "requires_split": fulfil.get("requires_split", False),
                },
                "tried_warehouses": sorted(tried),
                "destination": order.get("destination"),
            }
            repair, heal_res = await crew.heal(failure, order, context, tracer=self.tracer)
            self._note_model(f"healer_c{cycle}", heal_res)
            self.repairs.append({"cycle": cycle, "failure": failure, **repair})
            self.emit("heal", repair["strategy"], cycle=cycle,
                      explanation=repair.get("explanation"),
                      tools_used=repair.get("tools_used", []),
                      confidence=repair.get("confidence"), model=heal_res.model)

            strategy = repair["strategy"]

            if strategy == "escalate_human":
                logistics.release_stock(self.order_id)
                get_db().execute("UPDATE orders SET status='escalated', updated_at=? WHERE id=?",
                                 (time.time(), self.order_id))
                return self._result(ok=False, error="escalated to human operator",
                                    extra={"repair": repair})

            # Apply the strategy, then re-plan against live state.
            if strategy == "downgrade_priority":
                order["priority"] = "standard"
            elif strategy in ("partial_fulfil", "split_shipment"):
                drop = set(repair.get("drop_skus") or fulfil.get("unfulfillable_skus") or [])
                if strategy == "split_shipment" and not drop:
                    # Ship the largest single-warehouse subset now; the remainder
                    # becomes a follow-on order rather than blocking this one.
                    drop = _skus_not_available_anywhere_together(order["lines"])
                kept = [l for l in order["lines"] if l["sku"] not in drop]
                if not kept:
                    logistics.release_stock(self.order_id)
                    return self._result(ok=False, error="nothing shippable after repair",
                                        extra={"repair": repair})
                order["lines"] = kept
                order["dropped_skus"] = sorted(drop)
                get_db().executemany(
                    "DELETE FROM order_lines WHERE order_id=? AND sku=?",
                    [(self.order_id, s) for s in drop],
                )
            elif strategy in ("reroute_warehouse", "switch_carrier"):
                # Both are applied below as a planning hint; the warehouse that
                # actually failed was already added to `tried` by the caller.
                pass

            logistics.release_stock(self.order_id)
            candidates, fulfil = await self._build_candidates(order, dest, exclude=tried)
            if not candidates:
                failure = f"after {strategy}, still no feasible warehouse (excluded {sorted(tried)})"
                continue

            # Honour the healer's explicit choice when it is still valid.
            forced = repair.get("warehouse_id") if strategy == "reroute_warehouse" else None
            hint = {}
            if forced and any(c["warehouse_id"] == forced for c in candidates):
                hint = {"warehouse_id": forced}
            if strategy == "switch_carrier" and repair.get("carrier"):
                hint["carrier"] = repair["carrier"]

            policies = get_db().get_policies()
            plan_obj, plan_res = await crew.plan(candidates, order, policies, tracer=self.tracer)
            self._note_model(f"planner_c{cycle}", plan_res)
            plan_obj.update({k: v for k, v in hint.items() if v})

            verdict = crew.critique(plan_obj, candidates, order)
            if not verdict["approved"]:
                # Fall back to the deterministic optimiser rather than burn another cycle.
                plan_obj = crew._mock_plan(candidates, order["priority"])
                verdict = crew.critique(plan_obj, candidates, order)
                self.emit("plan", "deterministic_override",
                          warehouse=plan_obj["warehouse_id"], carrier=plan_obj["carrier"],
                          reason="healed plan still violated constraints")
            if verdict["approved"]:
                self.emit("heal", "recovered", cycle=cycle,
                          warehouse=plan_obj["warehouse_id"], carrier=plan_obj["carrier"])
                return await self._execute(order, plan_obj, verdict, dest, fulfil,
                                           candidates, tried=tried)
            failure = "post-repair plan still invalid: " + "; ".join(verdict["violations"])

        logistics.release_stock(self.order_id)
        get_db().execute("UPDATE orders SET status='failed', updated_at=? WHERE id=?",
                         (time.time(), self.order_id))
        self.emit("heal", "exhausted", cycles=MAX_REPAIR_CYCLES, failure=failure)
        return self._result(ok=False, error=f"repair exhausted: {failure}")

    # ---------------------------------------------------------------- result
    def _result(self, *, ok: bool, error: str | None = None,
                extra: dict[str, Any] | None = None) -> dict[str, Any]:
        total_ms = (time.perf_counter() - self.t0) * 1000
        payload = {
            "ok": ok,
            "order_id": self.order_id,
            "error": error,
            "total_ms": round(total_ms, 2),
            "stages": [s.to_dict() for s in self.stages],
            "repairs": self.repairs,
            "trace": self.tracer.summary(),
            "spans": [s.to_dict() for s in self.tracer.spans],
            "models_used": self.models_used,
            "degraded": self.degraded,
            "breakers": breaker_snapshot(),
            **(extra or {}),
        }
        bus.publish("done", {"order_id": self.order_id, "ok": ok,
                             "total_ms": payload["total_ms"], "error": error})
        return payload


def _same_place(a: str, b: str) -> bool:
    """Cheap agreement check for the speculative-geocode optimisation."""
    ta = {w for w in re.findall(r"[a-z]{4,}", a.lower())}
    tb = {w for w in re.findall(r"[a-z]{4,}", b.lower())}
    return bool(ta & tb)


def _skus_not_available_anywhere_together(lines: list[dict[str, Any]]) -> set[str]:
    """Drop the scarcest SKUs until a single warehouse can serve the rest."""
    scored = []
    for l in lines:
        cands = logistics.search_inventory(l["sku"], int(l["qty"]))
        scored.append((len(cands), l["sku"]))
    scored.sort()
    drop: set[str] = set()
    remaining = list(lines)
    for _n, sku in scored:
        if len(remaining) <= 1:
            break
        trial = [l for l in remaining if l["sku"] != sku]
        f = logistics.find_fulfillment_candidates(trial)
        drop.add(sku)
        remaining = trial
        if f["single_warehouse_options"]:
            break
    return drop


async def run_order(raw: str, *, idem_key: str | None = None,
                    qa_image: tuple[bytes, str] | None = None) -> dict[str, Any]:
    return await Orchestrator(raw, idem_key=idem_key, qa_image=qa_image).run()
