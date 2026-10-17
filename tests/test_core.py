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
