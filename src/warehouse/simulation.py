"""Discrete-time simulation of a three-warehouse fulfillment network.

One tick ≈ one minute of a shift and one robot move. Each tick: new orders
arrive and get routed, agents act, robots move, and periodically cameras scan.
Each day end: shrinkage hits the shelves, demand is recorded, the forecaster is
refit, the replenishment agent places POs, and forecast accuracy is scored.
"""

from __future__ import annotations

import math
from collections import defaultdict
from dataclasses import dataclass

import numpy as np

from .agents import DecisionLog, FulfillmentAgent, ReplenishmentAgent
from .events import Event, EventBus
from .forecasting import ForecastService
from .models import SKU, Coord, Order, OrderLine, OrderStatus
from .network import Warehouse
from .vision import SimulatedDetector

DOW_FACTORS = np.array([1.0, 1.05, 1.1, 1.05, 1.25, 0.85, 0.7])
PRODUCT_NAMES = [
    "USB-C cable", "Wireless mouse", "Desk lamp", "Water bottle", "Notebook A5", "Phone case",
    "Headphones", "Coffee mug", "Backpack", "Power bank", "Keyboard", "Monitor stand",
    "Yoga mat", "Sunglasses", "Board game", "Plant pot",
]
WAREHOUSE_SITES: dict[str, Coord] = {"WH-NORTH": (20, 80), "WH-CENTRAL": (50, 45), "WH-SOUTH": (80, 15)}
MEAN_LINES = 2.0  # lines per order ~ U{1,2,3}
MEAN_QTY = 2.0    # units per line ~ U{1,2,3}


@dataclass
class SimConfig:
    seed: int = 7
    n_skus: int = 12
    ticks_per_day: int = 480
    orders_per_day: float = 60.0
    robots_per_warehouse: int = 3
    bins_per_sku: int = 2
    initial_cover_days: int = 6
    scan_every_ticks: int = 60
    shrinkage_events_per_day: float = 3.0  # per warehouse, units silently vanish
    history_days: int = 56                 # synthetic demand history for the forecaster
    outage: tuple[str, int, int] | None = ("WH-CENTRAL", 4, 6)  # warehouse, first day down, day restored


class Simulation:
    def __init__(self, config: SimConfig | None = None) -> None:
        self.cfg = cfg = config or SimConfig()
        if cfg.n_skus > len(PRODUCT_NAMES):
            raise ValueError(f"n_skus must be <= {len(PRODUCT_NAMES)}")
        self.rng = np.random.default_rng(cfg.seed)
        self.bus = EventBus()
        self.tick = 0
        self.day = 0
        self.orders: dict[str, Order] = {}
        self._order_seq = 0

        self.catalog = {
            f"SKU-{i + 1:03d}": SKU(f"SKU-{i + 1:03d}", PRODUCT_NAMES[i], lead_time_days=int(self.rng.integers(2, 5)))
            for i in range(cfg.n_skus)
        }
        pop = 1.0 / np.arange(1, cfg.n_skus + 1) ** 0.8
        self.popularity = pop / pop.sum()
        self.sku_ids = list(self.catalog)

        self.warehouses = {
            wid: Warehouse(wid, pos, self.bus, n_robots=cfg.robots_per_warehouse,
                           detector=SimulatedDetector(np.random.default_rng(cfg.seed + i + 1)))
            for i, (wid, pos) in enumerate(WAREHOUSE_SITES.items())
        }
        self._seed_stock()

        self.decisions = DecisionLog(self.bus)
        self.forecasts = ForecastService()
        self._seed_history()
        self.fulfillment = FulfillmentAgent(self.warehouses, self.orders, self.bus, self.decisions)
        self.replenishment = ReplenishmentAgent(self.warehouses, self.catalog, self.forecasts, self.bus, self.decisions)

        # day accumulators + forecast scoring
        self._today_demand: dict[str, int] = defaultdict(int)
        self._today_alloc: dict[tuple[str, str], int] = defaultdict(int)
        self._next_day_fc: dict[str, float] = {}
        self._fc_abs_err = self._fc_naive_abs_err = self._fc_actual = 0.0
        self.daily: list[dict] = []
        self.bus.subscribe("allocation.created", self._on_allocation)

    # --- setup ------------------------------------------------------------
    def expected_daily_units(self, sku_id: str, day: int | None = None) -> float:
        dow = 1.0 if day is None else DOW_FACTORS[day % 7]
        return self.cfg.orders_per_day * dow * MEAN_LINES * MEAN_QTY * self.popularity[self.sku_ids.index(sku_id)]

    def _seed_stock(self) -> None:
        n_wh = len(self.warehouses)
        for wh in self.warehouses.values():
            locs = list(self.rng.permutation(sorted(wh.layout.locations)))
            for sku in self.sku_ids:
                per_wh = self.expected_daily_units(sku) / n_wh * self.cfg.initial_cover_days
                per_bin = max(5, math.ceil(per_wh / self.cfg.bins_per_sku))
                for _ in range(self.cfg.bins_per_sku):
                    wh.slot(sku, locs.pop(), per_bin)

    def _seed_history(self) -> None:
        """Pretend we have a few weeks of sales data before go-live."""
        for d in range(-self.cfg.history_days, 0):
            for sku in self.sku_ids:
                self.forecasts.record(sku, self.rng.poisson(self.expected_daily_units(sku, d)))
        self.forecasts.refit()

    # --- orders -------------------------------------------------------------
    def submit_order(self, lines: list[tuple[str, int]], destination: Coord) -> Order:
        for sku, qty in lines:
            if sku not in self.catalog:
                raise KeyError(sku)
            if qty <= 0:
                raise ValueError("qty must be positive")
        self._order_seq += 1
        order = Order(f"ORD-{self._order_seq:06d}", [OrderLine(s, q) for s, q in lines], destination,
                      created_tick=self.tick)
        self.orders[order.order_id] = order
        for s, q in lines:
            self._today_demand[s] += q
        self.bus.publish("order.created", order_id=order.order_id, lines=len(lines))
        self.fulfillment.route(order)
        return order

    def _random_order(self) -> Order:
        n_lines = int(self.rng.integers(1, 4))
        merged: dict[str, int] = defaultdict(int)
        for sku in self.rng.choice(self.sku_ids, size=n_lines, p=self.popularity):
            merged[str(sku)] += int(self.rng.integers(1, 4))
        dest = (int(self.rng.integers(0, 101)), int(self.rng.integers(0, 101)))
        return self.submit_order(list(merged.items()), dest)

    def _on_allocation(self, ev: Event) -> None:
        self._today_alloc[(ev.data["warehouse_id"], ev.data["sku_id"])] += ev.data["qty"]

    # --- time ---------------------------------------------------------------
    def step(self) -> None:
        cfg = self.cfg
        self.bus.tick = self.tick
        if self.tick % cfg.ticks_per_day == 0:
            self._start_of_day()
        lam = cfg.orders_per_day * DOW_FACTORS[self.day % 7] / cfg.ticks_per_day
        for _ in range(self.rng.poisson(lam)):
            self._random_order()
        self.fulfillment.step()
        for wh in self.warehouses.values():
            wh.step()
        if self.tick % cfg.scan_every_ticks == 0:
            for wh in self.warehouses.values():
                if wh.online:
                    wh.scan_shelves()
        self.tick += 1
        if self.tick % cfg.ticks_per_day == 0:
            self._end_of_day()

    def run_ticks(self, n: int) -> None:
        for _ in range(n):
            self.step()

    def run_days(self, n: int, on_day=None) -> None:
        for _ in range(n):
            self.run_ticks(self.cfg.ticks_per_day)
            if on_day:
                on_day(self.daily[-1])

    def _start_of_day(self) -> None:
        if self.cfg.outage:
            wid, start, end = self.cfg.outage
            if self.day == start:
                self.warehouses[wid].set_online(False, "simulated power failure")
            elif self.day == end:
                self.warehouses[wid].set_online(True, "power restored")
        self.replenishment.receive_due(self.day)

    def _end_of_day(self) -> None:
        # forecast accuracy: yesterday's one-day-ahead forecast vs today's actual
        for sku in self.sku_ids:
            actual = self._today_demand.get(sku, 0)
            if sku in self._next_day_fc:
                hist = self.forecasts.histories[sku]
                self._fc_abs_err += abs(actual - self._next_day_fc[sku])
                self._fc_naive_abs_err += abs(actual - hist[-7])
                self._fc_actual += actual
            self.forecasts.record(sku, actual)
        self.forecasts.refit()
        self._next_day_fc = {s: float(self.forecasts.forecast(s, 1)[0]) for s in self.sku_ids}

        self.replenishment.record_day(dict(self._today_alloc))
        self.replenishment.plan(self.day + 1)
        self._shrinkage()

        snap = self.metrics()
        snap["day"] = self.day
        self.daily.append(snap)
        self.bus.publish("day.closed", day=self.day)
        self._today_demand.clear()
        self._today_alloc.clear()
        self.day += 1

    def _shrinkage(self) -> None:
        """Damage/theft/misplacement: units vanish from shelves without the system knowing."""
        for wh in self.warehouses.values():
            for _ in range(self.rng.poisson(self.cfg.shrinkage_events_per_day)):
                stocked = [l for l in sorted(wh.inventory.bin_sku) if wh.physical[l] > 0]
                if not stocked:
                    break
                loc = stocked[int(self.rng.integers(len(stocked)))]
                lost = min(wh.physical[loc], int(self.rng.integers(1, 6)))
                wh.physical[loc] -= lost
                self.bus.publish("sim.shrinkage", warehouse_id=wh.warehouse_id, location_id=loc, lost=lost)

    # --- reporting ----------------------------------------------------------
    def metrics(self) -> dict:
        orders = list(self.orders.values())
        shipped = [o for o in orders if o.status == OrderStatus.SHIPPED]
        cycle = [o.shipped_tick - o.created_tick for o in shipped if o.shipped_tick is not None]
        robots = [r for w in self.warehouses.values() for r in w.fleet.robots]
        c = self.bus.counts
        return {
            "tick": self.tick,
            "day": self.day,
            "orders": len(orders),
            "shipped": len(shipped),
            "open_backorders": len(self.fulfillment.backorders),
            "in_progress": sum(o.status == OrderStatus.ALLOCATED for o in orders),
            "avg_cycle_ticks": round(float(np.mean(cycle)), 1) if cycle else None,
            "p95_cycle_ticks": round(float(np.percentile(cycle, 95)), 1) if cycle else None,
            "split_shipments": sum(len(o.warehouses()) > 1 for o in shipped),
            "reroutes": c["order.rerouted"],
            "short_picks": c["pick.short"],
            "vision_fixes": c["vision.discrepancy_fixed"],
            "vision_low_confidence": c["vision.low_confidence"],
            "shrinkage_events": c["sim.shrinkage"],
            "purchase_orders": c["po.created"],
            "robot_utilization": round(sum(r.busy_ticks for r in robots) / max(1, self.tick * len(robots)), 3),
            "robot_distance": sum(r.distance for r in robots),
            "forecast_wape": round(self._fc_abs_err / self._fc_actual, 3) if self._fc_actual else None,
            "naive_wape": round(self._fc_naive_abs_err / self._fc_actual, 3) if self._fc_actual else None,
            "warehouses_online": [w for w, wh in self.warehouses.items() if wh.online],
        }
