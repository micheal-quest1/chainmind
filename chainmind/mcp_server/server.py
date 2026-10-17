"""ChainMind MCP server - the logistics network as a Model Context Protocol tool set.

Two transports off the same tool definitions:

  stdio            `python -m chainmind.mcp_server` - for Claude Desktop, Cursor,
                   or any local MCP client.
  streamable-http  mounted into the FastAPI app at /mcp - so the *deployed*
                   instance is a remote MCP server anyone can connect to.

Why this exists beyond ticking the "MCP integration" box: these same functions
are what the in-process agents call directly. One implementation, two consumers.
The pipeline calls them as Python (no IPC on the hot path - that is worth tens
of milliseconds per order), while external clients get the identical surface
over MCP. There is no second code path to drift out of sync.
"""
from __future__ import annotations

import asyncio
from typing import Any, Literal

import os

from mcp.server.mcpserver import MCPServer
from mcp.server.transport_security import TransportSecuritySettings

from chainmind.core.db import get_db
from chainmind.services import logistics, maps


def transport_security() -> TransportSecuritySettings:
    """Host/origin policy for the streamable-HTTP transport.

    The MCP SDK ships DNS-rebinding protection that validates the Host header
    against an allow-list defaulting to localhost. Behind Cloud Run the Host is
    the service's own generated domain, so an unconfigured server answers every
    remote MCP request with "Invalid Host header" - which reads like a routing
    bug and is not.

    That protection exists to stop a malicious web page in a victim's browser
    from reaching an MCP server bound to their loopback interface. This server
    is a deliberately public, unauthenticated, read-mostly demo endpoint on a
    domain Cloud Run generates, so that threat model does not apply and the
    check only breaks legitimate clients. It is therefore off by default here.

    Passed to `streamable_http_app(transport_security=...)`. Note that leaving
    that argument as None does NOT mean 'no policy': the SDK auto-applies a
    localhost-only allow-list, which is exactly what rejects the Cloud Run host.

    Turn it back on for any deployment that is not a public demo:

        MCP_ALLOWED_HOSTS=chainmind.example.com,api.example.com

    which enables protection and permits exactly those hosts plus localhost.
    """
    raw = os.getenv("MCP_ALLOWED_HOSTS", "").strip()
    if not raw or raw == "*":
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)

    hosts = ["localhost", "localhost:8080", "127.0.0.1", "127.0.0.1:8080"]
    origins = ["http://localhost:8080", "http://127.0.0.1:8080"]
    for h in (x.strip() for x in raw.split(",") if x.strip()):
        hosts.append(h)
        origins.append(f"https://{h}")
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=hosts,
        allowed_origins=origins,
    )


mcp = MCPServer(
    "chainmind-logistics",
    instructions=(
        "Autonomous supply-chain control plane. Query live inventory across a six-node "
        "Indian distribution network, compute Google Maps traffic-aware routes, price "
        "carriers against learned historical bias, reserve stock atomically, dispatch "
        "shipments, and feed delivery outcomes back into the planner's learned policies. "
        "Call `network_status` first to see the network."
    ),
)


# ------------------------------------------------------------------- read side
@mcp.tool(
    description="Live network overview: every warehouse, its coordinates, available "
                "units, plus carrier performance statistics learned from past deliveries."
)
def network_status() -> dict[str, Any]:
    return logistics.network_snapshot()


@mcp.tool(
    description="Find warehouses that can ship a given SKU and quantity right now. "
                "Availability is on_hand minus already-reserved stock."
)
def search_inventory(sku: str, qty: int = 1) -> dict[str, Any]:
    results = logistics.search_inventory(sku, qty)
    return {"sku": sku, "qty": qty, "warehouse_count": len(results), "warehouses": results}


@mcp.tool(
    description="Given full order lines, report which single warehouses can ship the "
                "whole order, which SKUs are unfulfillable, and whether a split is forced."
)
def check_fulfillment(lines: list[dict]) -> dict[str, Any]:
    out = logistics.find_fulfillment_candidates(
        [{"sku": str(l["sku"]), "qty": int(l["qty"])} for l in lines]
    )
    out.pop("warehouse_meta", None)
    return out


@mcp.tool(description="Current status of one order, including its shipment if dispatched.")
def order_status(order_id: str) -> dict[str, Any]:
    return logistics.get_order_snapshot(order_id)


# -------------------------------------------------------------- planning side
@mcp.tool(
    description="Google Maps Routes API v2 traffic-aware driving route between two "
                "coordinate pairs. Returns road distance in km and live-traffic duration."
)
async def compute_route(
    origin_lat: float, origin_lng: float, dest_lat: float, dest_lng: float
) -> dict[str, Any]:
    leg = await maps.compute_route((origin_lat, origin_lng), (dest_lat, dest_lng))
    return leg.to_dict()


@mcp.tool(
    description="Validate and geocode a delivery address with Google Address Validation, "
                "falling back to Geocoding. Use before dispatch to catch undeliverable addresses."
)
async def validate_address(address: str) -> dict[str, Any]:
    return (await maps.validate_address(address)).to_dict()


@mcp.tool(
    description="Solve the multi-drop last-mile problem: given a depot and up to 25 stops, "
                "return the optimal visiting order using Google's waypoint optimisation."
)
async def optimize_multidrop(depot_lat: float, depot_lng: float,
                             stops: list[dict]) -> dict[str, Any]:
    pts = [(float(s["lat"]), float(s["lng"])) for s in stops]
    route = await maps.optimize_multidrop((depot_lat, depot_lng), pts)
    return route.to_dict()


@mcp.tool(
    description="Choose packaging for an order at a warehouse: box size, gross weight, "
                "fragile/cold-chain handling, packaging cost and pack-line minutes."
)
def plan_packaging(lines: list[dict], warehouse_id: str) -> dict[str, Any]:
    return logistics.plan_packaging(
        [{"sku": str(l["sku"]), "qty": int(l["qty"])} for l in lines], warehouse_id
    )


@mcp.tool(
    description="Price every carrier for a leg. ETAs are corrected by each carrier's "
                "learned bias from real delivery outcomes, so this reflects actual performance."
)
def quote_carriers(
    distance_km: float,
    drive_minutes: float,
    gross_weight_g: int,
    fragile: bool = False,
    cold_chain: bool = False,
    priority: Literal["standard", "express"] = "standard",
) -> dict[str, Any]:
    return {
        "quotes": logistics.quote_carriers(
            distance_km, drive_minutes, gross_weight_g,
            fragile=fragile, cold_chain=cold_chain, priority=priority,
        )
    }


# --------------------------------------------------------------- write side
@mcp.tool(
    description="Run the full autonomous fulfilment pipeline on a free-text order: "
                "triage, address validation, warehouse selection, routing, carrier choice, "
                "critic review, self-healing recovery, stock reservation and dispatch. "
                "This is the whole agent flow in one call."
)
async def fulfil_order(order_text: str, idempotency_key: str | None = None) -> dict[str, Any]:
    from chainmind.pipeline import run_order

    result = await run_order(order_text, idem_key=idempotency_key)
    # Trim the span firehose; an MCP client wants the decision, not the trace.
    result.pop("spans", None)
    return result


@mcp.tool(
    description="Atomically reserve stock for an order at a warehouse. Idempotent: "
                "re-running returns the existing allocation instead of double-reserving."
)
def reserve_stock(order_id: str, warehouse_id: str, lines: list[dict]) -> dict[str, Any]:
    return logistics.reserve_stock(
        order_id, warehouse_id,
        [{"sku": str(l["sku"]), "qty": int(l["qty"])} for l in lines],
    )


@mcp.tool(description="Release all stock reserved for an order (compensating transaction).")
def release_stock(order_id: str) -> dict[str, Any]:
    return logistics.release_stock(order_id)


# ------------------------------------------------------- inventory management
@mcp.tool(
    description="Full stock position across the network: every SKU at every warehouse "
                "with on-hand, reserved, available, reorder point and a health state "
                "(healthy / low / critical / out_of_stock)."
)
def stock_levels() -> dict[str, Any]:
    from chainmind.services import inventory as inv

    grid = inv.stock_grid()
    return {"by_state": grid["by_state"], "sku_totals": grid["sku_totals"],
            "needs_attention": grid["needs_attention"]}


@mcp.tool(
    description="Book goods into a warehouse (supplier delivery or returned stock). "
                "Increases on-hand immediately and records a goods-receipt note."
)
def receive_stock(warehouse_id: str, sku: str, qty: int,
                  source: str = "supplier", reference: str = "") -> dict[str, Any]:
    from chainmind.services import inventory as inv

    return inv.receive_stock(warehouse_id, sku, int(qty), source=source, reference=reference)


@mcp.tool(
    description="Find every warehouse at or below its reorder point, with the real "
                "surplus available elsewhere and the Google-Maps-priced cost of moving it."
)
async def replenishment_needs() -> dict[str, Any]:
    from chainmind import replenishment

    out = await replenishment.scan()
    return {"healthy": out["healthy"], "needs": out["context"]}


@mcp.tool(
    description="Run the autonomous inventory planner: decide and execute stock "
                "transfers between warehouses to fix low stock. Set execute=false to "
                "see the plan without moving anything."
)
async def run_replenishment(execute: bool = True) -> dict[str, Any]:
    from chainmind import replenishment

    return await replenishment.run(execute=bool(execute))


@mcp.tool(
    description="Move stock between two warehouses. Depletes the source immediately; "
                "the destination receives it when the transfer lands."
)
def transfer_stock(from_warehouse: str, to_warehouse: str, sku: str, qty: int,
                   reason: str = "manual") -> dict[str, Any]:
    from chainmind.services import inventory as inv

    return inv.create_transfer(from_warehouse, to_warehouse, sku, int(qty), reason=reason)


@mcp.tool(description="Land an in-flight stock transfer at its destination warehouse.")
def receive_transfer(transfer_id: str) -> dict[str, Any]:
    from chainmind.services import inventory as inv

    return inv.receive_transfer(transfer_id)


@mcp.tool(
    description="Process a customer return. Restockable goods go back into sellable "
                "stock at the warehouse that shipped them; damaged goods are written off."
)
def process_return(order_id: str, sku: str, qty: int, restockable: bool = True,
                   reason: str = "") -> dict[str, Any]:
    from chainmind.services import inventory as inv

    return inv.process_return(order_id, sku, int(qty),
                              restockable=bool(restockable), reason=reason)


@mcp.tool(
    description="Orders the system could not complete autonomously and that need a "
                "human decision, with the reason each one stalled."
)
def open_exceptions() -> dict[str, Any]:
    db = get_db()
    rows = db.query(
        "SELECT id, raw, status, dest_label FROM orders WHERE status IN"
        " ('escalated','failed','address_rejected') ORDER BY updated_at DESC LIMIT 40")
    return {"exceptions": [dict(r) for r in rows], "count": len(rows)}


# ------------------------------------------------------------- feedback side
@mcp.tool(
    description="Close the feedback loop: report a shipment's actual delivery time. "
                "This immediately updates the carrier's learned ETA bias, which changes "
                "carrier selection on the very next order."
)
def record_delivery(shipment_id: str, actual_minutes: float,
                    damaged: bool = False, notes: str = "") -> dict[str, Any]:
    from chainmind.feedback import record_delivery as rec

    return rec(shipment_id, actual_minutes, damaged=damaged, notes=notes)


@mcp.tool(
    description="Run the reflection agent over recent delivery outcomes to distil new "
                "routing policies, which the planner then reads on every future order."
)
async def run_reflection() -> dict[str, Any]:
    from chainmind.feedback import reflect

    out = await reflect()
    out.pop("active_policies", None)
    return out


@mcp.tool(
    description="Show what the system has learned: per-carrier ETA bias, prediction error "
                "over time, and the natural-language policies currently steering the planner."
)
def learning_state() -> dict[str, Any]:
    from chainmind.feedback import learning_curve

    return learning_curve()


# ------------------------------------------------------------------ resources
@mcp.resource("chainmind://network", description="The live distribution network as JSON.")
def network_resource() -> dict[str, Any]:
    return logistics.network_snapshot()


@mcp.resource("chainmind://policies", description="Learned routing policies currently in force.")
def policies_resource() -> dict[str, Any]:
    return get_db().get_policies()


def build_server() -> MCPServer:
    """Ensure the datastore exists before any tool is served."""
    from chainmind.core.seed import seed

    seed(get_db())
    return mcp
