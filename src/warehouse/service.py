"""Framework-free application service: every operation the web app needs.

It runs in two places, from the same code:
* on the server, wrapped by FastAPI (`warehouse.api`), one Service per visitor session;
* in the browser, through Pyodide (the static GitHub Pages build), via `dispatch_json`.

Keep this module free of FastAPI/pydantic imports so it loads in Pyodide.
"""

from __future__ import annotations

import json
import pickle
import re
import zlib
from dataclasses import asdict, dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlsplit

from .forecasting import backtest
from .models import Order, OrderStatus
from .simulation import WAREHOUSE_SITES, SimConfig, Simulation

MAX_DAYS = 120          # keeps memory bounded when left running
MAX_STEP_TICKS = 2_400  # 5 simulated days per request
SNAPSHOT_VERSION = 1
NOISY_EVENTS = {"allocation.created", "job.queued", "agent.decision", "order.created", "job.done",
                "vision.low_confidence", "day.closed"}


class HttpError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


@dataclass(frozen=True)
class Scenario:
    """The knobs a visitor can set and share in a link."""

    seed: int = 7
    outage: str | None = "WH-CENTRAL"  # warehouse that fails, or None
    outage_start: int = 4               # day it goes down
    outage_days: int = 2                # how long it stays down
    orders_per_day: float = 60.0
    robots: int = 3                     # per warehouse

    @classmethod
    def parse(cls, data: dict | None) -> "Scenario":
        data = data or {}
        base = cls()

        def num(key: str, lo, hi, cast):
            raw = data.get(key, getattr(base, key))
            try:
                value = cast(raw)
            except (TypeError, ValueError):
                raise HttpError(422, f"{key} must be a number") from None
            return max(lo, min(hi, value))

        outage = data.get("outage", base.outage)
        if outage is True:
            outage = base.outage
        elif outage in (None, False, "", "none", "None"):
            outage = None
        else:
            outage = str(outage).upper()
            if not outage.startswith("WH-"):
                outage = "WH-" + outage
            if outage not in WAREHOUSE_SITES:
                raise HttpError(422, f"unknown warehouse {outage}")
        return cls(
            seed=num("seed", 0, 2**31 - 1, int),
            outage=outage,
            outage_start=num("outage_start", 0, MAX_DAYS, int),
            outage_days=num("outage_days", 1, 30, int),
            orders_per_day=num("orders_per_day", 5.0, 300.0, float),
            robots=num("robots", 1, 8, int),
        )

    def config(self) -> SimConfig:
        outage = (self.outage, self.outage_start, self.outage_start + self.outage_days) if self.outage else None
        return SimConfig(seed=self.seed, orders_per_day=self.orders_per_day, robots_per_warehouse=self.robots, outage=outage)


def order_out(o: Order) -> dict:
    return {
        "order_id": o.order_id,
        "status": o.status.value,
        "destination": list(o.destination),
        "lines": [asdict(l) for l in o.lines],
        "allocations": [asdict(a) for a in o.allocations],
        "unallocated": o.unallocated(),
        "warehouses": o.warehouses(),
        "created_tick": o.created_tick,
        "shipped_tick": o.shipped_tick,
        "reroute_count": o.reroute_count,
    }


class Service:
    def __init__(self, scenario: Scenario | None = None, sim: Simulation | None = None) -> None:
        self.scenario = scenario or Scenario()
        self.sim = sim or Simulation(self.scenario.config())

    # --- persistence (browser keeps progress across refreshes) -------------
    def snapshot(self) -> bytes:
        return zlib.compress(pickle.dumps((SNAPSHOT_VERSION, self.scenario, self.sim), protocol=pickle.HIGHEST_PROTOCOL), 6)

    @classmethod
    def restore(cls, data: bytes) -> "Service":
        version, scenario, sim = pickle.loads(zlib.decompress(bytes(data)))
        if version != SNAPSHOT_VERSION:
            raise ValueError("snapshot from an older version")
        return cls(scenario, sim)

    # --- helpers ------------------------------------------------------------
    def _wh(self, wid: str):
        if wid not in self.sim.warehouses:
            raise HttpError(404, f"unknown warehouse {wid}")
        return self.sim.warehouses[wid]

    # --- operations ---------------------------------------------------------
    def health(self) -> dict:
        return {"ok": True, "tick": self.sim.tick, "day": self.sim.day}

    def catalog(self) -> list:
        return [asdict(s) for s in self.sim.catalog.values()]

    def warehouses(self) -> list:
        return [w.summary() for w in self.sim.warehouses.values()]

    def inventory(self, wid: str) -> list:
        return self._wh(wid).inventory.snapshot()

    def outage(self, wid: str) -> dict:
        self._wh(wid).set_online(False, "manual")
        self.sim.fulfillment.step()
        return self._wh(wid).summary()

    def restore_site(self, wid: str) -> dict:
        self._wh(wid).set_online(True, "manual")
        return self._wh(wid).summary()

    def scan(self, wid: str) -> dict:
        return {"adjustments": self._wh(wid).scan_shelves()}

    def create_order(self, lines: list[tuple[str, int]], destination) -> dict:
        if not lines:
            raise HttpError(422, "an order needs at least one line")
        try:
            dest = (int(destination[0]), int(destination[1]))
            order = self.sim.submit_order([(str(s), int(q)) for s, q in lines], dest)
        except KeyError as e:
            raise HttpError(422, f"unknown sku {e}") from None
        except (ValueError, TypeError, IndexError) as e:
            raise HttpError(422, str(e) or "invalid order") from None
        return order_out(order)

    def list_orders(self, status: str | None = None, limit: int = 50) -> list:
        if status is not None and status not in {s.value for s in OrderStatus}:
            raise HttpError(422, f"unknown status {status}")
        orders = [o for o in self.sim.orders.values() if status is None or o.status.value == status]
        return [order_out(o) for o in reversed(orders[-max(1, limit):])]

    def get_order(self, oid: str) -> dict:
        if oid not in self.sim.orders:
            raise HttpError(404, f"unknown order {oid}")
        return order_out(self.sim.orders[oid])

    def forecast(self, sku_id: str, horizon: int = 14, history: int = 14) -> dict:
        if sku_id not in self.sim.catalog:
            raise HttpError(404, f"unknown sku {sku_id}")
        fs = self.sim.forecasts
        hist = fs.histories[sku_id]
        return {
            "sku_id": sku_id,
            "history_tail": hist[-max(1, history):],
            "forecast": [round(float(x), 2) for x in fs.forecast(sku_id, max(1, min(horizon, 90)))],
            "sigma": round(fs.sigma(sku_id), 2),
            "backtest_7d": backtest(hist, 7),
        }

    def decisions(self, limit: int = 50, agent: str | None = None) -> list:
        return [asdict(d) for d in self.sim.decisions.recent(5000) if agent is None or d.agent == agent][-limit:]

    def events(self, limit: int = 100, kind: str | None = None) -> list:
        return [asdict(e) for e in self.sim.bus.recent(limit, kind)]

    def metrics(self) -> dict:
        return self.sim.metrics()

    def metrics_daily(self) -> list:
        return self.sim.daily

    def inventory_overview(self) -> list:
        sim, rows = self.sim, []
        for sku in sim.catalog.values():
            fc7 = float(sim.forecasts.forecast(sku.sku_id, 7).sum())
            per_wh = {
                w.warehouse_id: {
                    "on_hand": w.inventory.on_hand_total(sku.sku_id),
                    "available": w.inventory.available(sku.sku_id),
                    "on_order": sim.replenishment.on_order[(w.warehouse_id, sku.sku_id)],
                    "online": w.online,
                }
                for w in sim.warehouses.values()
            }
            available = sum(v["available"] for v in per_wh.values() if v["online"])
            daily = fc7 / 7
            rows.append({
                "sku_id": sku.sku_id, "name": sku.name, "lead_time_days": sku.lead_time_days,
                "warehouses": per_wh, "available": available,
                "on_order": sum(v["on_order"] for v in per_wh.values()),
                "forecast_7d": round(fc7, 1),
                "days_of_cover": round(available / daily, 1) if daily else None,
            })
        return rows

    def purchase_orders(self) -> list:
        return [asdict(p) for p in sorted(self.sim.replenishment.inbound, key=lambda p: p.arrival_day)]

    def step(self, ticks: int = 60) -> dict:
        sim = self.sim
        if sim.day >= MAX_DAYS:
            raise HttpError(409, f"The simulation reached day {MAX_DAYS}. Reset to start over.")
        sim.run_ticks(max(1, min(int(ticks), MAX_STEP_TICKS, MAX_DAYS * sim.cfg.ticks_per_day - sim.tick)))
        return sim.metrics()

    def reset(self, scenario: dict | None = None) -> dict:
        self.scenario = Scenario.parse({**asdict(self.scenario), **(scenario or {})}) if scenario is not None else self.scenario
        self.sim = Simulation(self.scenario.config())
        return {**self.sim.metrics(), "scenario": asdict(self.scenario)}

    def dashboard_state(self) -> dict:
        sim, whs = self.sim, []
        for w in sim.warehouses.values():
            inv = w.inventory
            whs.append({
                "warehouse_id": w.warehouse_id,
                "online": w.online,
                "backlog": w.backlog,
                "queued_jobs": len(w.fleet.queue),
                "width": w.layout.width,
                "height": w.layout.height,
                "dock": list(w.layout.dock),
                "bins": [
                    {"loc": loc, "x": x, "y": y, "sku": inv.bin_sku.get(loc),
                     "on_hand": inv.on_hand[loc] if loc in inv.bin_sku else 0,
                     "reserved": inv.reserved[loc] if loc in inv.bin_sku else 0,
                     "physical": w.physical[loc] if loc in inv.bin_sku else 0}
                    for loc, (x, y) in w.layout.locations.items()
                ],
                "robots": [
                    {"id": r.robot_id, "pos": list(r.pos), "state": r.state.value, "battery": round(r.battery),
                     "order": r.job.order_id if r.job else None,
                     "route": [list(c) for leg in r.legs for c in leg.path],
                     "stops": [list(w.layout.pick_faces[leg.task.location_id]) for leg in r.legs if leg.task]}
                    for r in w.fleet.robots
                ],
            })
        return {
            "tick": sim.tick,
            "day": sim.day,
            "max_days": MAX_DAYS,
            "ticks_per_day": sim.cfg.ticks_per_day,
            "scenario": asdict(self.scenario),
            "metrics": sim.metrics(),
            "daily": sim.daily,
            "warehouses": whs,
            "skus": [{"sku_id": s.sku_id, "name": s.name} for s in sim.catalog.values()],
            "decisions": [asdict(d) for d in sim.decisions.recent(40)][::-1],
            "events": [asdict(e) for e in sim.bus.log if e.kind not in NOISY_EVENTS][-40:][::-1],
        }

    # --- path router (used by the browser build) ------------------------------
    def dispatch(self, method: str, path: str, query: dict[str, str] | None = None, body: dict | None = None) -> Any:
        q, b = query or {}, body or {}

        def qi(key: str, default: int) -> int:
            try:
                return int(q.get(key, default))
            except ValueError:
                raise HttpError(422, f"{key} must be an integer") from None

        routes: list[tuple[str, str, Callable[..., Any]]] = [
            ("GET", r"/health", lambda: self.health()),
            ("GET", r"/catalog", lambda: self.catalog()),
            ("GET", r"/warehouses", lambda: self.warehouses()),
            ("GET", r"/warehouses/([^/]+)/inventory", lambda w: self.inventory(w)),
            ("POST", r"/warehouses/([^/]+)/outage", lambda w: self.outage(w)),
            ("POST", r"/warehouses/([^/]+)/restore", lambda w: self.restore_site(w)),
            ("POST", r"/warehouses/([^/]+)/scan", lambda w: self.scan(w)),
            ("POST", r"/orders", lambda: self.create_order(
                [(l.get("sku_id"), l.get("qty")) for l in b.get("lines", [])], b.get("destination") or [0, 0])),
            ("GET", r"/orders", lambda: self.list_orders(q.get("status"), qi("limit", 50))),
            ("GET", r"/orders/([^/]+)", lambda oid: self.get_order(oid)),
            ("GET", r"/forecast/([^/]+)", lambda s: self.forecast(s, qi("horizon", 14), qi("history", 14))),
            ("GET", r"/decisions", lambda: self.decisions(qi("limit", 50), q.get("agent"))),
            ("GET", r"/events", lambda: self.events(qi("limit", 100), q.get("kind"))),
            ("GET", r"/metrics", lambda: self.metrics()),
            ("GET", r"/metrics/daily", lambda: self.metrics_daily()),
            ("GET", r"/inventory/overview", lambda: self.inventory_overview()),
            ("GET", r"/purchase-orders", lambda: self.purchase_orders()),
            ("POST", r"/simulation/step", lambda: self.step(b.get("ticks", 60))),
            ("POST", r"/simulation/reset", lambda: self.reset(b.get("scenario", b) if b else None)),
            ("GET", r"/dashboard/state", lambda: self.dashboard_state()),
        ]
        for m, pattern, handler in routes:
            if m == method.upper():
                match = re.fullmatch(pattern, path)
                if match:
                    return handler(*match.groups())
        raise HttpError(404, f"no route {method} {path}")

    def dispatch_json(self, method: str, url: str, body_json: str | None = None) -> str:
        """JSON in, JSON out — the whole browser↔Python bridge is this one call."""
        parts = urlsplit(url)
        try:
            body = json.loads(body_json) if body_json else None
            data = self.dispatch(method, parts.path, dict(parse_qsl(parts.query)), body)
            return json.dumps({"ok": True, "data": data})
        except HttpError as e:
            return json.dumps({"ok": False, "status": e.status, "detail": e.detail})
        except json.JSONDecodeError:
            return json.dumps({"ok": False, "status": 400, "detail": "invalid JSON body"})
