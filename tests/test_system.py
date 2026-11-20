from fastapi.testclient import TestClient

from warehouse.api import create_app
from warehouse.models import OrderStatus
from warehouse.simulation import SimConfig, Simulation


def test_simulation_is_deterministic():
    a, b = Simulation(SimConfig(seed=3)), Simulation(SimConfig(seed=3))
    a.run_days(2)
    b.run_days(2)
    assert a.metrics() == b.metrics()


def test_inventory_never_negative_and_reservations_balance():
    sim = Simulation(SimConfig(seed=11))
    sim.run_days(8)
    for wh in sim.warehouses.values():
        open_tasks = [t for j in wh.fleet.queue for t in j.tasks] + [
            t for r in wh.fleet.robots if r.job for t in r.job.tasks if not t.done
        ]
        for loc in wh.inventory.bin_sku:
            assert wh.inventory.on_hand[loc] >= 0
            assert wh.physical[loc] >= 0
            expected = sum(t.qty for t in open_tasks if t.location_id == loc)
            assert wh.inventory.reserved[loc] == expected, (wh.warehouse_id, loc)


def test_network_survives_outage_and_fills_orders():
    sim = Simulation(SimConfig(seed=5, outage=("WH-CENTRAL", 1, 3)))
    sim.run_days(6)
    m = sim.metrics()
    assert m["reroutes"] > 0
    assert m["shipped"] / m["orders"] > 0.95
    shipped = [o for o in sim.orders.values() if o.status == OrderStatus.SHIPPED]
    for o in shipped:
        assert sum(a.picked for a in o.allocations if not a.cancelled) == sum(o.demand().values())


def test_api_order_lifecycle():
    client = TestClient(create_app(Simulation(SimConfig(seed=1, outage=None))))
    r = client.post("/orders", json={"lines": [{"sku_id": "SKU-001", "qty": 2}], "destination": [10, 10]})
    assert r.status_code == 201
    oid = r.json()["order_id"]
    assert r.json()["status"] == "allocated"
    client.post("/simulation/step", json={"ticks": 200})
    assert client.get(f"/orders/{oid}").json()["status"] == "shipped"

    assert client.post("/orders", json={"lines": [{"sku_id": "NOPE", "qty": 1}], "destination": [0, 0]}).status_code == 422
    assert client.get("/forecast/SKU-001").json()["forecast"]
    assert len(client.get("/warehouses").json()) == 3
    assert client.post("/warehouses/WH-NORTH/outage").json()["online"] is False
    assert client.get("/metrics").status_code == 200


def test_dashboard_endpoints():
    client = TestClient(create_app(Simulation(SimConfig(seed=2, outage=None))))
    client.post("/simulation/step", json={"ticks": 120})
    assert "Fulfillment Control" in client.get("/dashboard").text
    s = client.get("/dashboard/state").json()
    assert len(s["warehouses"]) == 3 and s["warehouses"][0]["bins"] and s["warehouses"][0]["robots"]
    inv = client.get("/inventory/overview").json()
    assert {"sku_id", "available", "days_of_cover", "warehouses"} <= set(inv[0])
    assert client.get("/purchase-orders").status_code == 200
    orders = client.get("/orders?limit=5").json()
    assert orders[0]["order_id"] > orders[-1]["order_id"]  # newest first
