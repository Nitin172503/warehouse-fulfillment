import numpy as np
import pytest

from warehouse.agents import DecisionLog, FulfillmentAgent
from warehouse.events import EventBus
from warehouse.forecasting import RidgeForecaster, backtest
from warehouse.inventory import InsufficientStock, Inventory
from warehouse.layout import Layout
from warehouse.models import Order, OrderLine, OrderStatus
from warehouse.network import Warehouse
from warehouse.robots import astar, manhattan
from warehouse.vision import Detection, StockReconciler


# --- layout & path planning ----------------------------------------------------
def test_astar_avoids_shelves_and_is_shortest():
    layout = Layout.grid(n_aisles=2, aisle_length=4)
    goal = layout.pick_faces["B2-03"]
    path = astar(layout, layout.dock, goal)
    assert path[-1] == goal
    assert all(layout.passable(c) for c in path)
    assert all(manhattan(a, b) == 1 for a, b in zip([layout.dock, *path], path))
    assert len(path) == manhattan(layout.dock, goal)  # an open aisle reaches it with no detour


def test_every_pick_face_is_reachable():
    layout = Layout.grid()
    for loc, face in layout.pick_faces.items():
        assert layout.passable(face), loc
        astar(layout, layout.dock, face)


# --- inventory -----------------------------------------------------------------
def test_reserve_commit_and_release():
    inv = Inventory("W")
    inv.receive("A", "L1", 5)
    inv.receive("A", "L2", 3)
    picks = inv.reserve("A", 7)
    assert sum(q for _, q in picks) == 7 and picks[0] == ("L1", 5)
    assert inv.available("A") == 1
    with pytest.raises(InsufficientStock):
        inv.reserve("A", 2)
    inv.commit_pick("L1", 5, 5)
    inv.release("L2", 2)
    assert inv.on_hand_total("A") == 3 and inv.available("A") == 3


def test_bins_are_single_sku():
    inv = Inventory("W")
    inv.receive("A", "L1", 1)
    with pytest.raises(ValueError):
        inv.receive("B", "L1", 1)


# --- vision reconciliation ------------------------------------------------------
def test_reconciler_needs_confident_repeated_sightings():
    bus = EventBus()
    inv = Inventory("W", bus)
    inv.receive("A", "L1", 10)
    rec = StockReconciler(inv, bus, min_confidence=0.8, confirmations=2)
    det = lambda n, c=0.95: Detection("cam", "L1", "A", n, c)

    assert rec.process([det(7, c=0.5)]) == 0  # low confidence ignored
    assert rec.process([det(7)]) == 0         # first sighting only
    assert inv.on_hand["L1"] == 10
    assert rec.process([det(7)]) == 1         # confirmed
    assert inv.on_hand["L1"] == 7
    assert bus.counts["vision.low_confidence"] == 1


# --- forecasting -----------------------------------------------------------------
def test_forecaster_beats_seasonal_naive_on_weekly_demand():
    rng = np.random.default_rng(0)
    dow = np.array([1.0, 1.05, 1.1, 1.05, 1.25, 0.85, 0.7])
    days = np.arange(120)
    y = rng.poisson(20 * dow[days % 7] * (1 + days / 400))
    res = backtest(y, horizon=14)
    assert res["wape_model"] < res["wape_seasonal_naive"]
    assert res["wape_model"] < 0.25


def test_forecaster_falls_back_on_short_history():
    f = RidgeForecaster(min_history=21)
    m = f.fit([4, 6, 5])
    assert m.weights is None
    assert np.allclose(f.predict(m, [4, 6, 5], 3), 5.0)


# --- fulfillment agent ---------------------------------------------------------------
@pytest.fixture
def network():
    bus = EventBus()
    near = Warehouse("NEAR", (0, 0), bus, n_robots=1)
    far = Warehouse("FAR", (100, 100), bus, n_robots=1)
    near.slot("A", "A1-01", 5)
    near.slot("B", "A1-02", 0)
    far.slot("A", "A1-01", 10)
    far.slot("B", "A1-02", 10)
    orders: dict[str, Order] = {}
    agent = FulfillmentAgent({"NEAR": near, "FAR": far}, orders, bus, DecisionLog(bus))
    return bus, near, far, orders, agent


def new_order(orders, oid, lines, dest=(1, 1)):
    o = Order(oid, [OrderLine(s, q) for s, q in lines], dest)
    orders[oid] = o
    return o


def run_until_idle(bus, agent, *whs, limit=2000):
    for t in range(limit):
        bus.tick = t
        agent.step()
        for w in whs:
            w.step()
        if all(w.backlog == 0 for w in whs) and not agent._reroutes:
            return


def test_routes_to_nearest_warehouse_that_can_fill(network):
    bus, near, far, orders, agent = network
    o = new_order(orders, "O1", [("A", 3)])
    assert agent.route(o) == {"NEAR": {"A": 3}}
    run_until_idle(bus, agent, near, far)
    assert o.status == OrderStatus.SHIPPED


def test_whole_order_goes_far_rather_than_split(network):
    bus, near, far, orders, agent = network
    o = new_order(orders, "O2", [("A", 1), ("B", 1)])
    assert agent.route(o) == {"FAR": {"A": 1, "B": 1}}


def test_splits_when_no_single_site_has_everything(network):
    _, _, _, orders, agent = network
    o = new_order(orders, "O3", [("A", 12)])
    plan = agent.route(o)
    assert sum(q for lines in plan.values() for q in lines.values()) == 12
    assert set(plan) == {"NEAR", "FAR"}


def test_backorders_then_fills_when_stock_arrives(network):
    bus, near, far, orders, agent = network
    o = new_order(orders, "O4", [("B", 15)])
    agent.route(o)
    assert o.status == OrderStatus.BACKORDERED and o.unallocated() == {"B": 5}
    near.receive("B", 5)
    bus.tick = 0
    agent.step()  # backorder retry runs on tick multiples
    assert not o.unallocated()
    run_until_idle(bus, agent, near, far)
    assert o.status == OrderStatus.SHIPPED


def test_outage_reroutes_queued_jobs(network):
    bus, near, far, orders, agent = network
    o = new_order(orders, "O5", [("A", 2)])
    agent.route(o)
    assert o.warehouses() == ["NEAR"]
    near.set_online(False, "test")
    agent.step()
    assert o.warehouses() == ["FAR"]
    assert near.inventory.available("A") == 5  # reservation released
    run_until_idle(bus, agent, near, far)
    assert o.status == OrderStatus.SHIPPED and o.reroute_count == 1


def test_short_pick_corrects_stock_and_reroutes_shortfall(network):
    bus, near, far, orders, agent = network
    near.physical["A1-01"] = 1  # 4 units vanished; system still thinks 5
    o = new_order(orders, "O6", [("A", 3)])
    agent.route(o)
    run_until_idle(bus, agent, near, far)
    assert bus.counts["pick.short"] == 1
    assert near.inventory.on_hand["A1-01"] == 0
    assert o.status == OrderStatus.SHIPPED
    assert sum(a.picked for a in o.allocations) == 3
    assert o.warehouses() == ["FAR", "NEAR"]
