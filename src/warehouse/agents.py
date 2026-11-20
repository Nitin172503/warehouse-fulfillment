"""Autonomous agents that make network-level decisions.

FulfillmentAgent  – routes each order to the cheapest warehouse(s) that can fill it,
                    and *reroutes* when the world changes: warehouse outages, short
                    picks (stock that turned out not to exist), overloaded robot
                    queues, and backorders that become fillable.
ReplenishmentAgent – turns demand forecasts into purchase orders per warehouse.

Every decision is written to a DecisionLog with a human-readable reason, so the
system's behaviour is explainable after the fact.
"""

from __future__ import annotations

import math
from collections import defaultdict, deque
from dataclasses import asdict, dataclass, field
from functools import partial

from .events import Event, EventBus
from .forecasting import ForecastService
from .models import SKU, Allocation, Coord, Order, OrderStatus
from .network import Warehouse
from .robots import PickTask


@dataclass
class Decision:
    tick: int
    agent: str
    action: str
    reason: str
    order_id: str | None = None
    details: dict = field(default_factory=dict)


class DecisionLog:
    def __init__(self, bus: EventBus, maxlen: int = 5000) -> None:
        self.bus = bus
        self.entries: deque[Decision] = deque(maxlen=maxlen)

    def record(self, agent: str, action: str, reason: str, order_id: str | None = None, **details) -> Decision:
        d = Decision(self.bus.tick, agent, action, reason, order_id, details)
        self.entries.append(d)
        self.bus.publish("agent.decision", **asdict(d))
        return d

    def recent(self, limit: int = 50) -> list[Decision]:
        return list(self.entries)[-limit:]


Plan = dict[str, dict[str, int]]  # warehouse_id -> {sku: qty}


class FulfillmentAgent:
    name = "fulfillment"

    def __init__(
        self,
        warehouses: dict[str, Warehouse],
        orders: dict[str, Order],
        bus: EventBus,
        log: DecisionLog,
        *,
        ship_cost_per_km: float = 0.05,
        backlog_penalty: float = 0.5,
        split_penalty: float = 2.0,
        max_queue: int = 8,
        max_reroutes: int = 3,
        backorder_retry_every: int = 30,
    ) -> None:
        self.warehouses = warehouses
        self.orders = orders
        self.bus = bus
        self.log = log
        self.ship_cost_per_km = ship_cost_per_km
        self.backlog_penalty = backlog_penalty
        self.split_penalty = split_penalty
        self.max_queue = max_queue
        self.max_reroutes = max_reroutes
        self.backorder_retry_every = backorder_retry_every
        self.backorders: set[str] = set()
        self._reroutes: deque[tuple[str, frozenset[str], str]] = deque()
        bus.subscribe("pick.short", self._on_short_pick)
        bus.subscribe("warehouse.outage", self._on_outage)
        bus.subscribe("job.done", self._on_job_done)

    # --- perception -------------------------------------------------------
    def cost(self, wh: Warehouse, dest: Coord) -> float:
        return math.dist(wh.position, dest) * self.ship_cost_per_km + wh.backlog * self.backlog_penalty

    # --- planning ---------------------------------------------------------
    def plan(self, need: dict[str, int], dest: Coord, exclude: frozenset[str] = frozenset()) -> tuple[Plan, dict[str, int]]:
        """Cheapest single warehouse that can ship everything; otherwise a greedy
        split that maximises units covered per unit of cost. Returns (plan, leftover)."""
        pool = [w for w in self.warehouses.values() if w.online and w.warehouse_id not in exclude]
        costs = {w.warehouse_id: self.cost(w, dest) for w in pool}

        full = [w for w in pool if all(w.inventory.available(s) >= q for s, q in need.items())]
        if full:
            best = min(full, key=lambda w: costs[w.warehouse_id])
            return {best.warehouse_id: dict(need)}, {}

        remaining, plan = dict(need), {}
        while remaining and pool:
            def coverage(w: Warehouse) -> int:
                return sum(min(q, w.inventory.available(s)) for s, q in remaining.items())

            scored = [(coverage(w) / (costs[w.warehouse_id] + self.split_penalty), w) for w in pool if coverage(w) > 0]
            if not scored:
                break
            best = max(scored, key=lambda x: x[0])[1]
            take = {s: min(q, best.inventory.available(s)) for s, q in remaining.items()}
            take = {s: q for s, q in take.items() if q > 0}
            plan[best.warehouse_id] = take
            for s, q in take.items():
                remaining[s] -= q
                if remaining[s] == 0:
                    del remaining[s]
            pool.remove(best)
        return plan, remaining

    # --- acting -----------------------------------------------------------
    def route(self, order: Order, exclude: frozenset[str] = frozenset(), reason: str = "new order") -> Plan:
        need = order.unallocated()
        if not need:
            return {}
        plan, leftover = self.plan(need, order.destination, exclude)
        for wh_id, lines in plan.items():
            self._commit(order, self.warehouses[wh_id], lines)

        was_backordered = order.order_id in self.backorders
        if leftover:
            order.status = OrderStatus.BACKORDERED
            self.backorders.add(order.order_id)
        else:
            self.backorders.discard(order.order_id)
            order.status = OrderStatus.ALLOCATED

        if plan or (leftover and not was_backordered):
            action = "route" if reason == "new order" else "reroute"
            if action == "reroute" and plan:
                order.reroute_count += 1
                self.bus.publish("order.rerouted", order_id=order.order_id, to=list(plan), reason=reason)
            self.log.record(self.name, action, reason, order.order_id, plan=plan, backordered=leftover)
        return plan

    def _commit(self, order: Order, wh: Warehouse, lines: dict[str, int]) -> None:
        tasks: list[PickTask] = []
        for sku, qty in lines.items():
            alloc = Allocation(wh.warehouse_id, sku, qty)
            order.allocations.append(alloc)
            tasks += [PickTask(order.order_id, sku, loc, q, allocation=alloc) for loc, q in wh.inventory.reserve(sku, qty)]
            self.bus.publish("allocation.created", order_id=order.order_id, warehouse_id=wh.warehouse_id,
                             sku_id=sku, qty=qty)
        wh.dispatch(order.order_id, tasks)

    def request_reroute(self, order_id: str, reason: str, exclude: frozenset[str] = frozenset()) -> None:
        self._reroutes.append((order_id, exclude, reason))

    # --- event handlers ---------------------------------------------------
    def _on_short_pick(self, ev: Event) -> None:
        d = ev.data
        self.request_reroute(d["order_id"], f"short pick: {d['sku_id']} at {d['warehouse_id']}/{d['location_id']} "
                                            f"missing {d['short']} units")

    def _on_outage(self, ev: Event) -> None:
        wh = self.warehouses[ev.data["warehouse_id"]]
        jobs = wh.fleet.drain_queue()
        for job in jobs:
            wh.cancel_job(job)
            self.request_reroute(job.order_id, f"{wh.warehouse_id} outage", frozenset({wh.warehouse_id}))
        self.log.record(self.name, "evacuate", f"{wh.warehouse_id} went offline ({ev.data.get('reason') or 'unknown'})",
                        cancelled_jobs=len(jobs))

    def _on_job_done(self, ev: Event) -> None:
        order = self.orders.get(ev.data["order_id"])
        if order:
            self._check_complete(order)

    def _check_complete(self, order: Order) -> None:
        if order.status != OrderStatus.SHIPPED and order.is_complete():
            order.status = OrderStatus.SHIPPED
            order.shipped_tick = self.bus.tick
            self.backorders.discard(order.order_id)
            self.bus.publish("order.shipped", order_id=order.order_id, warehouses=order.warehouses(),
                             cycle_ticks=order.shipped_tick - order.created_tick)

    # --- main loop ----------------------------------------------------------
    def step(self) -> None:
        while self._reroutes:
            order_id, exclude, reason = self._reroutes.popleft()
            order = self.orders.get(order_id)
            if order and order.status not in (OrderStatus.SHIPPED, OrderStatus.CANCELLED):
                self.route(order, exclude, reason)
        self._rebalance()
        if self.bus.tick % self.backorder_retry_every == 0:
            for order_id in sorted(self.backorders):
                self.route(self.orders[order_id], reason="backorder retry: stock became available")

    def _rebalance(self) -> None:
        """Move not-yet-started jobs off warehouses whose robot queue is too long,
        but only when another warehouse can take the whole job."""
        if sum(w.online for w in self.warehouses.values()) < 2:
            return
        for wh in self.warehouses.values():
            excess = len(wh.fleet.queue) - self.max_queue
            if not wh.online or excess <= 0:
                continue
            for job in reversed(list(wh.fleet.queue)[-excess:]):
                order = self.orders[job.order_id]
                if order.reroute_count >= self.max_reroutes:
                    continue
                exclude = frozenset({wh.warehouse_id})
                _, leftover = self.plan(job.demand(), order.destination, exclude)
                if leftover:
                    continue
                wh.fleet.remove(job)
                wh.cancel_job(job)
                self.route(order, exclude, f"load balancing: {wh.warehouse_id} has {len(wh.fleet.queue) + 1} queued jobs")


@dataclass
class PurchaseOrder:
    po_id: str
    warehouse_id: str
    sku_id: str
    qty: int
    ordered_day: int
    arrival_day: int


class ReplenishmentAgent:
    """Daily: for each warehouse × SKU, if (available + on order) < reorder point,
    order up to reorder point + `cover_days` of forecast demand.

    Network demand is forecast per SKU; each warehouse's share is estimated from
    how much of that SKU the routing agent sent it over the last two weeks."""

    name = "replenishment"

    def __init__(
        self,
        warehouses: dict[str, Warehouse],
        catalog: dict[str, SKU],
        forecasts: ForecastService,
        bus: EventBus,
        log: DecisionLog,
        *,
        z: float = 1.65,
        cover_days: int = 7,
        share_window_days: int = 14,
    ) -> None:
        self.warehouses = warehouses
        self.catalog = catalog
        self.forecasts = forecasts
        self.bus = bus
        self.log = log
        self.z = z
        self.cover_days = cover_days
        self.on_order: dict[tuple[str, str], int] = defaultdict(int)
        self.inbound: list[PurchaseOrder] = []
        self._alloc_hist: dict[tuple[str, str], deque[int]] = defaultdict(partial(deque, maxlen=share_window_days))
        self._po_seq = 0

    def record_day(self, allocated: dict[tuple[str, str], int]) -> None:
        for wh in self.warehouses:
            for sku in self.catalog:
                self._alloc_hist[(wh, sku)].append(allocated.get((wh, sku), 0))

    def share(self, wh_id: str, sku_id: str) -> float:
        online = [w for w in self.warehouses.values() if w.online]
        total = sum(sum(self._alloc_hist[(w.warehouse_id, sku_id)]) for w in online)
        if not total:
            return 1.0 / max(1, len(online))
        return sum(self._alloc_hist[(wh_id, sku_id)]) / total

    def plan(self, day: int) -> list[PurchaseOrder]:
        created = []
        for wh in self.warehouses.values():
            if not wh.online:
                continue
            for sku in wh.inventory.skus():
                lead = self.catalog[sku].lead_time_days
                share = self.share(wh.warehouse_id, sku)
                rop = self.forecasts.reorder_point(sku, lead, self.z, share)
                position = wh.inventory.available(sku) + self.on_order[(wh.warehouse_id, sku)]
                if position >= rop:
                    continue
                cover = float(self.forecasts.forecast(sku, self.cover_days).sum()) * share
                qty = math.ceil(rop + cover - position)
                if qty <= 0:
                    continue
                self._po_seq += 1
                po = PurchaseOrder(f"PO-{self._po_seq:05d}", wh.warehouse_id, sku, qty, day, day + lead)
                self.inbound.append(po)
                self.on_order[(wh.warehouse_id, sku)] += qty
                created.append(po)
                self.bus.publish("po.created", **asdict(po))
                self.log.record(self.name, "purchase", f"position {position} < reorder point {rop:.1f} "
                                f"(lead {lead}d, share {share:.0%})", po_id=po.po_id, warehouse_id=wh.warehouse_id,
                                sku_id=sku, qty=qty)
        return created

    def receive_due(self, day: int) -> list[PurchaseOrder]:
        due = [po for po in self.inbound if po.arrival_day <= day]
        self.inbound = [po for po in self.inbound if po.arrival_day > day]
        for po in due:
            self.warehouses[po.warehouse_id].receive(po.sku_id, po.qty)
            self.on_order[(po.warehouse_id, po.sku_id)] -= po.qty
            self.bus.publish("po.received", po_id=po.po_id, warehouse_id=po.warehouse_id, sku_id=po.sku_id, qty=po.qty)
        return due
