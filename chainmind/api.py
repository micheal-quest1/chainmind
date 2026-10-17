"""HTTP surface.

Also mounts the MCP server at /mcp over streamable HTTP, so the deployed
instance is simultaneously a REST API, a live dashboard, and a remote MCP
server that any MCP client can connect to.
"""
from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from chainmind.agents import copilot, llm
from chainmind.core.bus import bus
from chainmind.core.config import get_settings
from chainmind.core.db import get_db
from chainmind.core.reliability import Tracer, breaker_snapshot
from chainmind.core.seed import seed
from chainmind.feedback import learning_curve, record_delivery, reflect
from chainmind.pipeline import run_order
from chainmind import replenishment
from chainmind.services import inventory as inv
from chainmind.services import logistics, maps

WEB_DIR = Path(__file__).parent / "web"
_boot: dict[str, Any] = {}


# ---------------------------------------------------------------- MCP app
# Built at import time: `streamable_http_app()` lazily creates the session
# manager, and the lifespan below needs it to already exist.
_mcp_server = None
_mcp_asgi = None
try:
    from chainmind.mcp_server.server import build_server as _build_mcp
    from chainmind.mcp_server.server import transport_security as _mcp_security

    _mcp_server = _build_mcp()
    # transport_security must be passed here: left as None the SDK installs a
    # localhost-only Host allow-list, so every request to the deployed domain
    # comes back as "Invalid Host header".
    _mcp_asgi = _mcp_server.streamable_http_app(transport_security=_mcp_security())
except Exception as _exc:  # MCP must never prevent the API from starting
    _mcp_import_error = str(_exc)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Boot: seed, resolve models and probe Google APIs in parallel, and start
    the MCP session manager.

    Model resolution costs one round trip and is done once here rather than on
    the first customer's order, so the first request is as fast as the hundredth.

    The MCP part matters: a mounted ASGI sub-app does NOT get its own lifespan
    run by the parent, and `streamable_http_app()` returns 500 "task group is
    not initialized" until its session manager is running. So the parent owns it.
    """
    t0 = time.perf_counter()
    counts = seed(get_db())
    models, probe = await asyncio.gather(llm.resolve_models(), maps.probe_apis())
    _boot.update({
        "seeded": counts,
        "models": models,
        "google_apis": probe,
        "mcp_mounted": _mcp_asgi is not None,
        "boot_ms": round((time.perf_counter() - t0) * 1000, 1),
        "started_at": time.time(),
    })
    get_db().log_event("system", "boot", payload=_boot)

    async with AsyncExitStack() as stack:
        if _mcp_server is not None:
            try:
                await stack.enter_async_context(_mcp_server.session_manager.run())
            except Exception as exc:
                get_db().log_event("system", "mcp_session_failed", level="error",
                                   payload={"error": str(exc)})
        try:
            yield
        finally:
            await maps.aclose()


app = FastAPI(
    title="ChainMind",
    version=get_settings().version,
    description=(
        "Autonomous supply-chain orchestrator. Multi-model Gemini agent pipeline, "
        "Google Maps routing, MCP tool surface, closed feedback loop, self-healing recovery."
    ),
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"]
)


@app.middleware("http")
async def request_context(request: Request, call_next):
    """Request id + server-timing on every response. Cheap, and it makes the
    'which stage was slow' question answerable from curl alone."""
    rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
    t0 = time.perf_counter()
    try:
        response = await call_next(request)
    except Exception as exc:
        ms = (time.perf_counter() - t0) * 1000
        get_db().log_event("http", "unhandled_error", level="error",
                           payload={"path": str(request.url.path), "error": str(exc)})
        return JSONResponse(
            status_code=500,
            content={"error": type(exc).__name__, "detail": str(exc), "request_id": rid},
            headers={"x-request-id": rid, "server-timing": f"total;dur={ms:.1f}"},
        )
    ms = (time.perf_counter() - t0) * 1000
    response.headers["x-request-id"] = rid
    response.headers["server-timing"] = f"total;dur={ms:.1f}"
    return response


# --------------------------------------------------------------------- models
class OrderRequest(BaseModel):
    text: str = Field(..., min_length=3, max_length=4000,
                      examples=["2 laptops and 5 usb-c cables to Anna Nagar, Chennai, urgent"])
    idempotency_key: str | None = Field(None, max_length=128)


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=4000)
    session_id: str = Field("default", max_length=64)


class DeliveryRequest(BaseModel):
    shipment_id: str
    actual_minutes: float = Field(..., gt=0, lt=100000)
    damaged: bool = False
    notes: str = ""


# ----------------------------------------------------------------- lifecycle
# Liveness is served on BOTH paths on purpose. Cloud Run's front end intercepts
# the exact path "/healthz" and answers it itself with a Google 404 page - the
# request never reaches the container - while "/healthz/" and every other path
# route normally. That is invisible locally and breaks the health check only
# once deployed, so "/health" is the canonical path here and "/healthz" stays
# as an alias for Kubernetes-style probes and local tooling.
@app.get("/health", tags=["ops"])
@app.get("/healthz", tags=["ops"], include_in_schema=False)
async def health() -> dict[str, Any]:
    """Liveness. Must stay cheap: no external calls."""
    return {"status": "ok", "version": get_settings().version,
            "uptime_s": round(time.time() - _boot.get("started_at", time.time()), 1)}


@app.get("/readyz", tags=["ops"])
async def readyz() -> JSONResponse:
    """Readiness: the DB must answer and the agent tiers must be resolved."""
    checks: dict[str, Any] = {}
    try:
        get_db().one("SELECT 1 AS ok")
        checks["database"] = True
    except Exception as exc:
        checks["database"] = f"error: {exc}"
    checks["models_resolved"] = bool(_boot.get("models"))
    checks["google_apis"] = _boot.get("google_apis", {})
    checks["breakers"] = breaker_snapshot()
    ready = checks["database"] is True and checks["models_resolved"]
    return JSONResponse({"ready": ready, **checks}, status_code=200 if ready else 503)


@app.get("/api/capabilities", tags=["ops"])
async def capabilities() -> dict[str, Any]:
    s = get_settings()
    return {
        "app": s.app_name, "version": s.version,
        **s.capabilities(),
        "resolved_models": _boot.get("models", {}),
        "thinking_modes": llm.thinking_report(),
        "model_pool_cooling": llm.pool_report(),
        "google_apis": _boot.get("google_apis", {}),
        "boot": {k: v for k, v in _boot.items() if k != "google_apis"},
        "mcp": {"stdio": "python -m chainmind.mcp_server",
                "http": "/mcp (streamable-http)",
                "tools": 15},
    }


# --------------------------------------------------------------------- core
@app.post("/api/orders", tags=["pipeline"])
async def create_order(req: OrderRequest) -> dict[str, Any]:
    """Run the full autonomous flow. This is the endpoint the judges time."""
    return await run_order(req.text, idem_key=req.idempotency_key)


@app.post("/api/orders/with-photo", tags=["pipeline"])
async def create_order_with_photo(
    text: str = Form(...),
    idempotency_key: str | None = Form(None),
    photo: UploadFile = File(...),
) -> dict[str, Any]:
    """Same flow plus the multimodal QA gate: a parcel photo is inspected by the
    vision agent at the packing station and can block dispatch."""
    data = await photo.read()
    if len(data) > 8 * 1024 * 1024:
        raise HTTPException(413, "image too large (max 8MB)")
    mime = photo.content_type or "image/jpeg"
    if not mime.startswith("image/"):
        raise HTTPException(415, f"expected an image, got {mime}")
    return await run_order(text, idem_key=idempotency_key, qa_image=(data, mime))


@app.get("/api/orders/{order_id}", tags=["pipeline"])
async def get_order(order_id: str) -> dict[str, Any]:
    snap = logistics.get_order_snapshot(order_id)
    if not snap["found"]:
        raise HTTPException(404, f"unknown order {order_id}")
    return snap


@app.post("/api/chat", tags=["agents"])
async def chat(req: ChatRequest) -> dict[str, Any]:
    return await copilot.chat(req.message, session_id=req.session_id)


# ----------------------------------------------------------------- feedback
@app.post("/api/deliveries", tags=["feedback"])
async def post_delivery(req: DeliveryRequest) -> dict[str, Any]:
    out = record_delivery(req.shipment_id, req.actual_minutes,
                          damaged=req.damaged, notes=req.notes)
    if not out.get("ok"):
        raise HTTPException(404, out.get("error", "unknown shipment"))
    return out


@app.post("/api/reflect", tags=["feedback"])
async def post_reflect() -> dict[str, Any]:
    return await reflect(tracer=Tracer(None))


@app.get("/api/feedback", tags=["feedback"])
async def get_feedback() -> dict[str, Any]:
    return learning_curve()


# ------------------------------------------------------------------ network
@app.get("/api/network", tags=["network"])
async def get_network() -> dict[str, Any]:
    return logistics.network_snapshot()


@app.get("/api/inventory", tags=["inventory"])
async def get_inventory(sku: str | None = None) -> dict[str, Any]:
    """Full stock position with health states, or one SKU's locations."""
    if sku:
        return {"sku": sku, "warehouses": logistics.search_inventory(sku, 1)}
    return inv.stock_grid()


class ReceiveRequest(BaseModel):
    warehouse_id: str
    sku: str
    qty: int = Field(..., gt=0, le=100000)
    source: str = Field("supplier", max_length=32)
    reference: str = Field("", max_length=64)
    note: str = Field("", max_length=240)


@app.post("/api/inventory/receive", tags=["inventory"])
async def receive(req: ReceiveRequest) -> dict[str, Any]:
    """Goods inward: book a supplier delivery into a warehouse."""
    out = inv.receive_stock(req.warehouse_id, req.sku, req.qty,
                            source=req.source, reference=req.reference, note=req.note)
    if not out["ok"]:
        raise HTTPException(400, out["error"])
    return out


class ReorderPolicyRequest(BaseModel):
    warehouse_id: str
    sku: str
    reorder_point: int = Field(..., ge=0, le=100000)
    target_stock: int = Field(..., ge=0, le=100000)


@app.post("/api/inventory/policy", tags=["inventory"])
async def set_policy(req: ReorderPolicyRequest) -> dict[str, Any]:
    out = inv.set_reorder_policy(req.warehouse_id, req.sku,
                                 req.reorder_point, req.target_stock)
    if not out["ok"]:
        raise HTTPException(400, out["error"])
    return out


class ReturnRequest(BaseModel):
    order_id: str
    sku: str
    qty: int = Field(..., gt=0, le=10000)
    restockable: bool = True
    reason: str = Field("", max_length=240)


@app.post("/api/returns", tags=["inventory"])
async def post_return(req: ReturnRequest) -> dict[str, Any]:
    """Reverse logistics: take goods back, restock them if they are sellable."""
    out = inv.process_return(req.order_id, req.sku, req.qty,
                             restockable=req.restockable, reason=req.reason)
    if not out["ok"]:
        raise HTTPException(400, out["error"])
    return out


@app.get("/api/replenishment", tags=["inventory"])
async def replenishment_scan() -> dict[str, Any]:
    """What needs restocking and what it would cost to fix. Read-only."""
    return await replenishment.scan()


@app.post("/api/replenishment/run", tags=["inventory"])
async def replenishment_run(execute: bool = True) -> dict[str, Any]:
    """Run the inventory planner. `execute=false` proposes without moving stock."""
    return await replenishment.run(execute=execute)


@app.get("/api/transfers", tags=["inventory"])
async def get_transfers(limit: int = 50) -> dict[str, Any]:
    return {"transfers": inv.list_transfers(limit)}


class TransferRequest(BaseModel):
    from_warehouse: str
    to_warehouse: str
    sku: str
    qty: int = Field(..., gt=0, le=100000)
    reason: str = Field("manual", max_length=120)


@app.post("/api/transfers/manual", tags=["inventory"])
async def transfer_manual(req: TransferRequest) -> dict[str, Any]:
    """Operator-initiated stock move, priced against the real road distance.

    Goes through the same validated path the replenishment agent uses, so a
    manual move can no more oversell a warehouse than an automated one can.
    """
    db = get_db()
    src = db.one("SELECT lat, lng FROM warehouses WHERE id=?", (req.from_warehouse,))
    dst = db.one("SELECT lat, lng FROM warehouses WHERE id=?", (req.to_warehouse,))
    if not src or not dst:
        raise HTTPException(404, "unknown warehouse")
    stock = db.one("SELECT unit_weight_g FROM inventory WHERE warehouse_id=? AND sku=?",
                   (req.from_warehouse, req.sku))
    if not stock:
        raise HTTPException(400, f"{req.from_warehouse} does not stock {req.sku}")

    leg = await maps.compute_route((src["lat"], src["lng"]), (dst["lat"], dst["lng"]))
    pricing = inv.price_transfer(leg.distance_km, req.qty, stock["unit_weight_g"])
    out = inv.create_transfer(
        req.from_warehouse, req.to_warehouse, req.sku, req.qty,
        reason=req.reason, distance_km=pricing["distance_km"],
        eta_minutes=pricing["eta_minutes"], cost_inr=pricing["cost_inr"],
        decided_by="operator",
    )
    if not out["ok"]:
        raise HTTPException(400, out["error"])
    return out


@app.post("/api/transfers/{transfer_id}/receive", tags=["inventory"])
async def transfer_receive(transfer_id: str) -> dict[str, Any]:
    out = inv.receive_transfer(transfer_id)
    if not out["ok"]:
        raise HTTPException(404, out["error"])
    return out


@app.get("/api/receipts", tags=["inventory"])
async def get_receipts(limit: int = 50) -> dict[str, Any]:
    return {"receipts": inv.list_receipts(limit)}


@app.get("/api/shipments", tags=["shipments"])
async def get_shipments(status: str | None = None, limit: int = 60) -> dict[str, Any]:
    """The tracking board: what is in transit, what landed, what it cost."""
    db = get_db()
    sql = ("SELECT s.*, o.dest_label, o.priority, o.raw FROM shipments s"
           " JOIN orders o ON o.id = s.order_id")
    params: list[Any] = []
    if status:
        sql += " WHERE s.status = ?"
        params.append(status)
    sql += " ORDER BY s.created_at DESC LIMIT ?"
    params.append(min(limit, 200))

    out = []
    for r in db.query(sql, params):
        route = json.loads(r["route"] or "{}")
        pack = json.loads(r["packaging"] or "{}")
        out.append({
            "shipment_id": r["id"], "order_id": r["order_id"],
            "warehouse_id": r["warehouse_id"], "carrier": r["carrier"],
            "status": r["status"], "destination": r["dest_label"],
            "priority": r["priority"],
            "eta_minutes": round(r["eta_minutes"], 1),
            "actual_minutes": round(r["actual_minutes"], 1) if r["actual_minutes"] else None,
            "distance_km": round(r["distance_km"], 1),
            "cost_inr": round(r["cost_inr"], 2),
            "box": pack.get("box"), "cold_chain": pack.get("cold_chain", False),
            "fragile": pack.get("fragile", False),
            "map_url": route.get("static_map_url"),
            "created_at": r["created_at"],
            "on_time": (r["actual_minutes"] <= r["eta_minutes"] * 1.1)
                       if r["actual_minutes"] else None,
        })
    counts = {row["status"]: row["c"] for row in db.query(
        "SELECT status, COUNT(*) c FROM shipments GROUP BY status")}
    return {"shipments": out, "counts": counts}


@app.get("/api/exceptions", tags=["shipments"])
async def get_exceptions(limit: int = 40) -> dict[str, Any]:
    """Orders the system could not complete on its own - the human work queue."""
    db = get_db()
    rows = db.query(
        "SELECT id, raw, status, priority, dest_label, created_at, updated_at"
        " FROM orders WHERE status IN"
        " ('escalated','failed','address_rejected','returned_damaged')"
        " ORDER BY updated_at DESC LIMIT ?", (min(limit, 200),))
    out = []
    for r in rows:
        last = db.one(
            "SELECT stage, message, payload FROM events WHERE order_id=?"
            " AND level!='info' ORDER BY seq DESC LIMIT 1", (r["id"],))
        detail = db.one(
            "SELECT message, payload FROM events WHERE order_id=? AND stage='heal'"
            " ORDER BY seq DESC LIMIT 1", (r["id"],))
        payload = json.loads((detail or last or {}).get("payload") or "{}") if (detail or last) else {}
        out.append({
            "order_id": r["id"], "order_text": r["raw"], "status": r["status"],
            "priority": r["priority"], "destination": r["dest_label"],
            "created_at": r["created_at"],
            "reason": payload.get("explanation") or payload.get("failure")
                      or (last["message"] if last else "unknown"),
        })
    return {"exceptions": out, "count": len(out)}


@app.post("/api/multidrop", tags=["network"])
async def multidrop(payload: dict) -> dict[str, Any]:
    """Google waypoint optimisation over a depot and N delivery stops."""
    depot = payload.get("depot") or {}
    stops = payload.get("stops") or []
    if not stops:
        raise HTTPException(400, "stops must be a non-empty list of {lat,lng}")
    route = await maps.optimize_multidrop(
        (float(depot["lat"]), float(depot["lng"])),
        [(float(s["lat"]), float(s["lng"])) for s in stops],
    )
    return route.to_dict()


# ------------------------------------------------------------------ metrics
@app.get("/api/metrics", tags=["ops"])
async def metrics() -> dict[str, Any]:
    """Latency distribution per pipeline span, straight out of SQLite."""
    db = get_db()
    rows = db.query(
        "SELECT name, kind, COUNT(*) n, AVG(duration_ms) avg_ms,"
        " MIN(duration_ms) min_ms, MAX(duration_ms) max_ms,"
        " SUM(CASE WHEN ok=0 THEN 1 ELSE 0 END) failures"
        " FROM spans GROUP BY name, kind ORDER BY avg_ms DESC"
    )
    pcts: dict[str, Any] = {}
    for r in db.query("SELECT DISTINCT name FROM spans"):
        vals = [x["duration_ms"] for x in db.query(
            "SELECT duration_ms FROM spans WHERE name=? ORDER BY duration_ms", (r["name"],))]
        if vals:
            pcts[r["name"]] = {
                "p50": round(vals[len(vals) // 2], 2),
                "p95": round(vals[min(len(vals) - 1, int(len(vals) * 0.95))], 2),
                "p99": round(vals[min(len(vals) - 1, int(len(vals) * 0.99))], 2),
            }
    orders = db.query(
        "SELECT o.id, o.status, s.duration_ms FROM orders o"
        " LEFT JOIN spans s ON s.order_id=o.id AND s.name='stage.triage_parallel_geo'"
        " ORDER BY o.created_at DESC LIMIT 50"
    )
    return {
        "spans": [dict(r) for r in rows],
        "percentiles_ms": pcts,
        "orders_recent": [dict(r) for r in orders],
        "breakers": breaker_snapshot(),
        "totals": {
            "orders": db.one("SELECT COUNT(*) c FROM orders")["c"],
            "shipments": db.one("SELECT COUNT(*) c FROM shipments")["c"],
            "delivered": db.one(
                "SELECT COUNT(*) c FROM shipments WHERE actual_minutes IS NOT NULL")["c"],
            "spans": db.one("SELECT COUNT(*) c FROM spans")["c"],
        },
    }


@app.get("/api/events", tags=["ops"])
async def events(limit: int = 80) -> dict[str, Any]:
    rows = get_db().query(
        "SELECT seq, order_id, stage, level, message, payload, ts FROM events"
        " ORDER BY seq DESC LIMIT ?", (min(limit, 500),))
    return {"events": [
        {**dict(r), "payload": json.loads(r["payload"] or "{}")} for r in rows]}


@app.get("/api/stream", tags=["ops"])
async def stream(request: Request) -> StreamingResponse:
    """Server-sent events: every pipeline stage, live."""
    q = bus.subscribe()

    async def gen():
        try:
            for evt in bus.recent(25):
                yield bus.sse(evt)
            yield bus.sse({"kind": "hello", "capabilities": get_settings().capabilities()})
            while True:
                if await request.is_disconnected():
                    break
                try:
                    evt = await asyncio.wait_for(q.get(), timeout=15.0)
                    yield bus.sse(evt)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
        finally:
            bus.unsubscribe(q)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={
        "Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"})


# ---------------------------------------------------------------- dashboard
@app.get("/", include_in_schema=False)
async def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


# ---------------------------------------------------------- MCP over HTTP
# The sub-app defines its own "/mcp" route and mcp 2.3 exposes no setting to
# change it, so mounting at "" (rather than "/mcp") serves it at /mcp instead
# of /mcp/mcp. This is registered last, so every API route above wins first.
if _mcp_asgi is not None:
    app.mount("", _mcp_asgi)
