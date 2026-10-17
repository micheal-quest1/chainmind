"""Tests for the parts that must never be wrong.

Deliberately not testing the agents' prose: that is non-deterministic and
testing it would be theatre. What is tested here is everything that can
silently corrupt state or oversell stock.

    .venv/bin/python -m pytest tests/ -q
"""
from __future__ import annotations

import asyncio
import os
import tempfile

import pytest

os.environ.setdefault("CHAINMIND_MOCK", "true")


@pytest.fixture()
def db(monkeypatch):
    """A throwaway database per test."""
    from chainmind.core import db as dbmod
    from chainmind.core.db import Database
    from chainmind.core.seed import seed

    path = os.path.join(tempfile.mkdtemp(), "t.db")
    inst = Database(path)
    seed(inst)
    monkeypatch.setattr(dbmod, "_db", inst)
    return inst


def test_reservation_is_atomic(db):
    """A multi-line reservation that cannot fully succeed must change nothing."""
    from chainmind.services.logistics import reserve_stock, search_inventory

    before = {c["warehouse_id"]: c["available"] for c in search_inventory("SKU-KEYBOARD", 1)}
    res = reserve_stock(
        "ORD-X", "WH-MAA",
        [{"sku": "SKU-KEYBOARD", "qty": 1}, {"sku": "SKU-LAPTOP-14", "qty": 99999}],
    )
    assert res["ok"] is False
    after = {c["warehouse_id"]: c["available"] for c in search_inventory("SKU-KEYBOARD", 1)}
    assert before == after, "a failed line must roll back the lines that succeeded"


def test_reservation_cannot_oversell(db):
    """Concurrent reservations must not drive available stock negative."""
    from chainmind.services.logistics import reserve_stock, search_inventory

    avail = next(c["available"] for c in search_inventory("SKU-VACCINE-KIT", 1)
                 if c["warehouse_id"] == "WH-HYD")
    ok = sum(
        1 for i in range(avail + 3)
        if reserve_stock(f"ORD-{i}", "WH-HYD", [{"sku": "SKU-VACCINE-KIT", "qty": 1}])["ok"]
    )
    assert ok == avail
    remaining = [c for c in search_inventory("SKU-VACCINE-KIT", 1) if c["warehouse_id"] == "WH-HYD"]
    assert remaining == [] or remaining[0]["available"] >= 0


def test_reservation_is_idempotent(db):
    from chainmind.services.logistics import reserve_stock, search_inventory

    lines = [{"sku": "SKU-CABLE-USBC", "qty": 5}]
    first = reserve_stock("ORD-IDEM", "WH-MAA", lines)
    before = next(c["available"] for c in search_inventory("SKU-CABLE-USBC", 1)
                  if c["warehouse_id"] == "WH-MAA")
    second = reserve_stock("ORD-IDEM", "WH-MAA", lines)
    after = next(c["available"] for c in search_inventory("SKU-CABLE-USBC", 1)
                 if c["warehouse_id"] == "WH-MAA")
    assert first["ok"] and second["idempotent_replay"] is True
    assert before == after, "a replayed reservation must not consume stock twice"


def test_release_restores_stock(db):
    from chainmind.services.logistics import release_stock, reserve_stock, search_inventory

    at = lambda: next(c["available"] for c in search_inventory("SKU-PHONE-PRO", 1)
                      if c["warehouse_id"] == "WH-BLR")
    before = at()
    reserve_stock("ORD-REL", "WH-BLR", [{"sku": "SKU-PHONE-PRO", "qty": 7}])
    assert at() == before - 7
    release_stock("ORD-REL")
    assert at() == before


def test_critic_rejects_infeasible_carrier(db):
    """The critic is the gate that makes a model's choice safe to execute."""
    from chainmind.agents.crew import critique

    cand = {
        "warehouse_id": "WH-MAA",
        "carrier_quotes": [
            {"carrier": "DroneX", "feasible": False,
             "blocked_because": ["over weight limit"], "eta_minutes": 30, "cost_inr": 500},
            {"carrier": "BlueDart", "feasible": True,
             "blocked_because": [], "eta_minutes": 90, "cost_inr": 200},
        ],
    }
    bad = critique({"warehouse_id": "WH-MAA", "carrier": "DroneX"}, [cand],
                   {"priority": "standard"})
    assert bad["approved"] is False and "infeasible" in bad["violations"][0]

    good = critique({"warehouse_id": "WH-MAA", "carrier": "BlueDart"}, [cand],
                    {"priority": "standard"})
    assert good["approved"] is True


def test_critic_rejects_hallucinated_warehouse(db):
    from chainmind.agents.crew import critique

    v = critique({"warehouse_id": "WH-ATLANTIS", "carrier": "BlueDart"},
                 [{"warehouse_id": "WH-MAA", "carrier_quotes": []}], {"priority": "standard"})
    assert v["approved"] is False


def test_cold_chain_carrier_is_enforced(db):
    from chainmind.agents.crew import critique

    cand = {"warehouse_id": "WH-HYD", "carrier_quotes": [
        {"carrier": "Ekart", "feasible": True, "blocked_because": [],
         "eta_minutes": 60, "cost_inr": 100}]}
    v = critique({"warehouse_id": "WH-HYD", "carrier": "Ekart"}, [cand],
                 {"priority": "standard", "cold_chain": True})
    assert v["approved"] is False and "cold-chain" in v["violations"][0]


def test_feedback_changes_the_next_quote(db):
    """The loop must be closed: a late delivery must reprice the next order."""
    import json
    import time

    from chainmind.feedback import record_delivery
    from chainmind.services.logistics import quote_carriers

    db.execute(
        "INSERT INTO orders (id,raw,status,priority,created_at,updated_at)"
        " VALUES ('O1','x','dispatched','standard',?,?)", (time.time(), time.time()))
    db.execute(
        "INSERT INTO shipments (id,order_id,warehouse_id,carrier,packaging,route,"
        "distance_km,eta_minutes,cost_inr,status,created_at)"
        " VALUES ('S1','O1','WH-MAA','Delhivery',?,?,100,60,250,'in_transit',?)",
        (json.dumps({}), json.dumps({}), time.time()))

    before = next(q["eta_minutes"] for q in quote_carriers(100, 90, 1000)
                  if q["carrier"] == "Delhivery")
    out = record_delivery("S1", 120.0)          # twice the promise
    assert out["eta_bias"]["after"] > out["eta_bias"]["before"]
    after = next(q["eta_minutes"] for q in quote_carriers(100, 90, 1000)
                 if q["carrier"] == "Delhivery")
    assert after > before, "a late delivery must inflate the next predicted ETA"


def test_packaging_respects_fragility_and_cold_chain(db):
    from chainmind.services.logistics import plan_packaging

    p = plan_packaging([{"sku": "SKU-VACCINE-KIT", "qty": 2}], "WH-HYD")
    assert p["fragile"] and p["cold_chain"]
    assert "gel-pack-cold-chain" in p["extras"] and "bubble-wrap" in p["extras"]
    assert p["gross_weight_g"] > 5000


def test_haversine_fallback_is_sane():
    from chainmind.services.maps import haversine_km

    chennai, bengaluru = (13.0827, 80.2707), (12.9716, 77.5946)
    d = haversine_km(chennai, bengaluru)
    assert 280 < d < 300, f"Chennai-Bengaluru great circle should be ~290km, got {d}"


def test_multidrop_fallback_visits_every_stop():
    from chainmind.services.maps import _fallback_multidrop

    depot = (13.0827, 80.2707)
    stops = [(12.97, 77.59), (17.38, 78.48), (11.01, 76.95), (9.93, 78.11)]
    r = _fallback_multidrop(depot, stops, True)
    assert sorted(r.ordered_stops) == list(range(len(stops)))
    assert r.total_distance_km > 0


def test_pipeline_completes_without_any_api_key(db, monkeypatch):
    """The whole flow must finish with no Gemini and no Maps key."""
    monkeypatch.setenv("CHAINMIND_MOCK", "true")
    from chainmind.core.config import get_settings

    get_settings.cache_clear()
    from chainmind.pipeline import run_order

    r = asyncio.run(run_order("2 laptops and 5 usb-c cables to Anna Nagar, Chennai"))
    assert r["ok"] is True
    assert r["shipment"]["carrier"]
    assert r["shipment"]["route"]["source"] == "haversine-fallback"
    get_settings.cache_clear()


# ============================== inventory management ==============================
def test_transfer_cannot_oversell(db):
    """A transfer must never move stock the donor does not have."""
    from chainmind.services.inventory import create_transfer
    from chainmind.services.logistics import search_inventory

    avail = next(c["available"] for c in search_inventory("SKU-MONITOR-27", 1)
                 if c["warehouse_id"] == "WH-BLR")
    bad = create_transfer("WH-BLR", "WH-MAA", "SKU-MONITOR-27", avail + 50, reason="test")
    assert bad["ok"] is False
    after = next(c["available"] for c in search_inventory("SKU-MONITOR-27", 1)
                 if c["warehouse_id"] == "WH-BLR")
    assert after == avail, "a rejected transfer must not touch stock"


def test_transfer_moves_stock_only_on_receipt(db):
    """In-flight stock must not be available at both ends at once."""
    from chainmind.services.inventory import create_transfer, receive_transfer
    from chainmind.services.logistics import search_inventory

    at = lambda wh: next(
        (c["available"] for c in search_inventory("SKU-KEYBOARD", 1)
         if c["warehouse_id"] == wh), 0)
    src_before, dst_before = at("WH-MAA"), at("WH-HYD")

    t = create_transfer("WH-MAA", "WH-HYD", "SKU-KEYBOARD", 20, reason="test")
    assert t["ok"]
    assert at("WH-MAA") == src_before - 20, "donor depletes on dispatch"
    assert at("WH-HYD") == dst_before, "receiver gains nothing until it lands"

    receive_transfer(t["transfer_id"])
    assert at("WH-HYD") == dst_before + 20
    assert at("WH-MAA") == src_before - 20


def test_transfer_receipt_is_idempotent(db):
    from chainmind.services.inventory import create_transfer, receive_transfer
    from chainmind.services.logistics import search_inventory

    t = create_transfer("WH-MAA", "WH-HYD", "SKU-KEYBOARD", 10, reason="test")
    receive_transfer(t["transfer_id"])
    after_first = next(c["available"] for c in search_inventory("SKU-KEYBOARD", 1)
                       if c["warehouse_id"] == "WH-HYD")
    second = receive_transfer(t["transfer_id"])
    after_second = next(c["available"] for c in search_inventory("SKU-KEYBOARD", 1)
                        if c["warehouse_id"] == "WH-HYD")
    assert second["idempotent_replay"] is True
    assert after_first == after_second, "re-receiving must not duplicate stock"


def test_receive_creates_sku_at_new_location(db):
    from chainmind.services.inventory import receive_stock
    from chainmind.services.logistics import search_inventory

    assert not [c for c in search_inventory("SKU-CHAIR-ERGO", 1)
                if c["warehouse_id"] == "WH-CJB"]
    out = receive_stock("WH-CJB", "SKU-CHAIR-ERGO", 7, source="supplier", reference="PO-1")
    assert out["ok"] and out["available_now"] == 7
    assert [c for c in search_inventory("SKU-CHAIR-ERGO", 1)
            if c["warehouse_id"] == "WH-CJB"]


def test_replenishment_detects_low_stock(db):
    """Falling to the reorder point must raise a need with the right shortfall."""
    from chainmind.services.inventory import replenishment_needs

    assert replenishment_needs() == []
    row = db.one("SELECT reorder_point, target_stock FROM inventory"
                 " WHERE warehouse_id='WH-HYD' AND sku='SKU-PHONE-PRO'")
    db.execute("UPDATE inventory SET on_hand=? WHERE warehouse_id='WH-HYD'"
               " AND sku='SKU-PHONE-PRO'", (row["reorder_point"],))
    needs = replenishment_needs()
    need = next(n for n in needs if n["warehouse_id"] == "WH-HYD")
    assert need["qty_needed"] == row["target_stock"] - row["reorder_point"]


def test_donor_never_drops_below_its_own_target(db):
    """`can_send` must only ever offer genuine surplus."""
    from chainmind.services.inventory import donor_options

    for d in donor_options("SKU-CABLE-USBC", 10_000, exclude="WH-HYD"):
        row = db.one("SELECT on_hand, reserved, target_stock FROM inventory"
                     " WHERE warehouse_id=? AND sku='SKU-CABLE-USBC'", (d["warehouse_id"],))
        available = row["on_hand"] - row["reserved"]
        assert available - d["surplus"] >= row["target_stock"]


def test_damaged_return_is_not_restocked(db):
    """Written-off goods must never re-enter sellable stock."""
    import json
    import time

    from chainmind.services.inventory import process_return
    from chainmind.services.logistics import search_inventory

    db.execute("INSERT INTO orders (id,raw,status,priority,created_at,updated_at)"
               " VALUES ('O9','x','dispatched','standard',?,?)", (time.time(), time.time()))
    db.execute(
        "INSERT INTO shipments (id,order_id,warehouse_id,carrier,packaging,route,"
        "distance_km,eta_minutes,cost_inr,status,created_at)"
        " VALUES ('S9','O9','WH-MAA','Ekart',?,?,10,30,100,'delivered',?)",
        (json.dumps({}), json.dumps({}), time.time()))

    at = lambda: next(c["available"] for c in search_inventory("SKU-KEYBOARD", 1)
                      if c["warehouse_id"] == "WH-MAA")
    before = at()
    out = process_return("O9", "SKU-KEYBOARD", 3, restockable=False, reason="water damage")
    assert out["ok"] and out["restocked"] is False
    assert at() == before, "damaged goods must not increase sellable stock"

    process_return("O9", "SKU-KEYBOARD", 3, restockable=True)
    assert at() == before + 3, "good returns go back into stock"


def test_stock_grid_states(db):
    """Health states must follow the reorder policy, not vibes."""
    from chainmind.services.inventory import stock_grid

    db.execute("UPDATE inventory SET on_hand=0 WHERE warehouse_id='WH-CJB' AND sku='SKU-TSHIRT-M'")
    rp = db.one("SELECT reorder_point FROM inventory WHERE warehouse_id='WH-MAA'"
                " AND sku='SKU-TSHIRT-M'")["reorder_point"]
    db.execute("UPDATE inventory SET on_hand=? WHERE warehouse_id='WH-MAA'"
               " AND sku='SKU-TSHIRT-M'", (rp,))

    items = {(i["warehouse_id"], i["sku"]): i for i in stock_grid()["items"]}
    assert items[("WH-CJB", "SKU-TSHIRT-M")]["state"] == "out_of_stock"
    assert items[("WH-MAA", "SKU-TSHIRT-M")]["state"] in ("low", "critical")


# ============================== partial fulfilment ==============================
def test_cap_to_available_picks_best_single_site(db):
    """An oversized order becomes "ship what one site has, backorder the rest"."""
    from chainmind.services.logistics import cap_lines_to_available

    kept, back, site = cap_lines_to_available([{"sku": "SKU-LAPTOP-14", "qty": 500}])
    assert site == "WH-BLR", "should pick the site that ships the most units"
    assert kept == [{"sku": "SKU-LAPTOP-14", "qty": 65}]
    assert back == [{"sku": "SKU-LAPTOP-14", "qty": 435}]
    assert kept[0]["qty"] + back[0]["qty"] == 500, "units must be conserved"


def test_cap_to_parcel_respects_carrier_payload(db):
    """Stock availability is not the only constraint - the parcel has a weight limit."""
    from chainmind.services.logistics import CARRIER_PROFILES, cap_lines_to_parcel

    heaviest = max(p[3] for p in CARRIER_PROFILES.values())
    kept, over = cap_lines_to_parcel([{"sku": "SKU-LAPTOP-14", "qty": 65}], "WH-BLR")
    shipped = kept[0]["qty"] if kept else 0
    assert shipped > 0, "a single laptop must always fit"
    assert shipped * 1800 <= heaviest, "parcel must fit the heaviest carrier"
    assert shipped + (over[0]["qty"] if over else 0) == 65, "units must be conserved"


def test_parcel_cap_never_silently_drops_units(db):
    from chainmind.services.logistics import cap_lines_to_parcel

    lines = [{"sku": "SKU-CABLE-USBC", "qty": 400}, {"sku": "SKU-MONITOR-27", "qty": 10}]
    kept, over = cap_lines_to_parcel(lines, "WH-BLR")
    want = {l["sku"]: l["qty"] for l in lines}
    got: dict[str, int] = {}
    for l in kept + over:
        got[l["sku"]] = got.get(l["sku"], 0) + l["qty"]
    assert got == want, "every requested unit must be either shipped or backordered"
