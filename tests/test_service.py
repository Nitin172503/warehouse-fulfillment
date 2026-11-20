import json
import subprocess
import sys

import pytest
from fastapi.testclient import TestClient

from warehouse.api import create_app
from warehouse.service import MAX_DAYS, HttpError, Scenario, Service


def call(svc: Service, method: str, url: str, body=None):
    return json.loads(svc.dispatch_json(method, url, json.dumps(body) if body is not None else None))


def test_service_does_not_need_fastapi():
    """The browser build (Pyodide) only ships the service, not FastAPI."""
    code = "import sys, warehouse.service; assert 'fastapi' not in sys.modules and 'pydantic' not in sys.modules"
    subprocess.run([sys.executable, "-c", code], check=True)


def test_dispatch_routes_and_errors():
    svc = Service(Scenario(outage=None))
    assert call(svc, "POST", "/simulation/step", {"ticks": 240})["ok"]
    order = call(svc, "POST", "/orders", {"lines": [{"sku_id": "SKU-001", "qty": 2}], "destination": [5, 5]})["data"]
    assert order["status"] == "allocated"
    assert call(svc, "GET", f"/orders/{order['order_id']}")["data"]["order_id"] == order["order_id"]
    assert call(svc, "GET", "/orders?limit=3")["data"][0]["order_id"] == order["order_id"]
    assert len(call(svc, "GET", "/forecast/SKU-002?horizon=5&history=7")["data"]["forecast"]) == 5
    state = call(svc, "GET", "/dashboard/state")["data"]
    assert state["scenario"]["outage"] is None and len(state["warehouses"]) == 3

    assert call(svc, "GET", "/orders/NOPE") == {"ok": False, "status": 404, "detail": "unknown order NOPE"}
    assert call(svc, "POST", "/orders", {"lines": [{"sku_id": "NOPE", "qty": 1}], "destination": [0, 0]})["status"] == 422
    assert call(svc, "GET", "/nowhere")["status"] == 404
    assert json.loads(svc.dispatch_json("POST", "/simulation/step", "{not json"))["status"] == 400


def test_scenario_parsing_and_clamping():
    sc = Scenario.parse({"seed": "42", "outage": "south", "orders_per_day": 9999, "robots": 0})
    assert (sc.seed, sc.outage, sc.orders_per_day, sc.robots) == (42, "WH-SOUTH", 300.0, 1)
    assert Scenario.parse({"outage": "none"}).outage is None
    assert Scenario.parse({"outage": True}).outage == "WH-CENTRAL"
    with pytest.raises(HttpError):
        Scenario.parse({"outage": "mars"})
    with pytest.raises(HttpError):
        Scenario.parse({"seed": "abc"})


def test_reset_applies_scenario():
    svc = Service()
    out = call(svc, "POST", "/simulation/reset", {"scenario": {"seed": 3, "robots": 5, "outage": "north", "outage_start": 0}})["data"]
    assert out["scenario"]["robots"] == 5
    assert all(len(w.fleet.robots) == 5 for w in svc.sim.warehouses.values())
    svc.step(1)
    assert not svc.sim.warehouses["WH-NORTH"].online
    # reset with no body keeps the current scenario
    assert call(svc, "POST", "/simulation/reset")["data"]["scenario"]["seed"] == 3


def test_same_scenario_plays_out_identically():
    a, b = Service(Scenario(seed=11)), Service(Scenario(seed=11))
    a.step(960), b.step(960)
    assert a.metrics() == b.metrics()


def test_snapshot_round_trip_continues_identically():
    svc = Service(Scenario(seed=5))
    svc.step(1200)
    clone = Service.restore(svc.snapshot())
    assert json.dumps(clone.dashboard_state()) == json.dumps(svc.dashboard_state())
    svc.step(480), clone.step(480)
    assert clone.metrics() == svc.metrics()


def test_step_stops_at_max_days():
    svc = Service()
    for _ in range(MAX_DAYS // 5):
        svc.step(2400)
    assert svc.sim.day == MAX_DAYS
    assert call(svc, "POST", "/simulation/step", {"ticks": 10})["status"] == 409


def test_each_server_visitor_gets_their_own_simulation():
    app = create_app()
    alice, bob = TestClient(app), TestClient(app)
    alice.post("/simulation/step", json={"ticks": 480})
    assert alice.get("/health").json()["tick"] == 480
    assert bob.get("/health").json()["tick"] == 0
    bob.post("/simulation/reset", json={"scenario": {"seed": 1, "robots": 2}})
    assert alice.get("/dashboard/state").json()["scenario"]["robots"] == 3
    assert len(app.state.sessions) == 2


def test_session_store_evicts_oldest():
    app = create_app(max_sessions=2)
    clients = [TestClient(app) for _ in range(3)]
    clients[0].post("/simulation/step", json={"ticks": 100})
    for c in clients[1:]:
        c.get("/health")
    assert len(app.state.sessions) == 2
    assert clients[0].get("/health").json()["tick"] == 0  # evicted, got a fresh simulation


def test_share_link_redirect_keeps_query():
    client = TestClient(create_app(), follow_redirects=False)
    r = client.get("/?seed=4&outage=south")
    assert r.headers["location"] == "/dashboard?seed=4&outage=south"


def test_server_reset_with_no_outage():
    client = TestClient(create_app())
    out = client.post("/simulation/reset", json={"scenario": {"seed": 2, "outage": None}}).json()
    assert out["scenario"]["outage"] is None and out["scenario"]["seed"] == 2
