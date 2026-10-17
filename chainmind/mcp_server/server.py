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

from mcp.server.mcpserver import MCPServer

from chainmind.core.db import get_db
from chainmind.services import logistics, maps

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
