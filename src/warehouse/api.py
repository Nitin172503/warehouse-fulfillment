"""HTTP API over a live simulation. Run: `uv run uvicorn warehouse.api:app --reload`."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, RedirectResponse
from pydantic import BaseModel, Field

from .forecasting import backtest
from .models import Order, OrderStatus
from .simulation import SimConfig, Simulation

STATIC = Path(__file__).parent / "static"
MAX_DAYS = 120  # keeps memory bounded when hosted and left running


class LineIn(BaseModel):
    sku_id: str
    qty: int = Field(gt=0)


class OrderIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1)
    destination: tuple[int, int]


class StepIn(BaseModel):
    ticks: int = Field(default=60, ge=1, le=48_000)


class ResetIn(BaseModel):
    seed: int = 7
    outage: bool = True


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


def create_app(sim: Simulation | None = None) -> FastAPI:
    app = FastAPI(title="Warehouse Fulfillment", version="0.1.0")
    state = {"sim": sim or Simulation()}

    def S() -> Simulation:
        return state["sim"]

    def wh_or_404(wid: str):
        if wid not in S().warehouses:
            raise HTTPException(404, f"unknown warehouse {wid}")
        return S().warehouses[wid]

    @app.get("/health")
    def health():
        return {"ok": True, "tick": S().tick, "day": S().day}

    @app.get("/catalog")
    def catalog():
        return [asdict(s) for s in S().catalog.values()]

    @app.get("/warehouses")
    def warehouses():
        return [w.summary() for w in S().warehouses.values()]

    @app.get("/warehouses/{wid}/inventory")
    def inventory(wid: str):
        return wh_or_404(wid).inventory.snapshot()

    @app.post("/warehouses/{wid}/outage")
    def outage(wid: str):
        wh_or_404(wid).set_online(False, "manual via API")
        S().fulfillment.step()
        return wh_or_404(wid).summary()

    @app.post("/warehouses/{wid}/restore")
    def restore(wid: str):
        wh_or_404(wid).set_online(True, "manual via API")
        return wh_or_404(wid).summary()

    @app.post("/warehouses/{wid}/scan")
    def scan(wid: str):
        return {"adjustments": wh_or_404(wid).scan_shelves()}

    @app.post("/orders", status_code=201)
    def create_order(body: OrderIn):
        try:
            o = S().submit_order([(l.sku_id, l.qty) for l in body.lines], body.destination)
        except KeyError as e:
            raise HTTPException(422, f"unknown sku {e}")
        return order_out(o)

    @app.get("/orders")
    def list_orders(status: OrderStatus | None = None, limit: int = 50):
        orders = [o for o in S().orders.values() if status is None or o.status == status]
        return [order_out(o) for o in reversed(orders[-limit:])]

    @app.get("/orders/{oid}")
    def get_order(oid: str):
        if oid not in S().orders:
            raise HTTPException(404, f"unknown order {oid}")
        return order_out(S().orders[oid])

    @app.get("/forecast/{sku_id}")
    def forecast(sku_id: str, horizon: int = 14, history: int = 14):
        if sku_id not in S().catalog:
            raise HTTPException(404, f"unknown sku {sku_id}")
        fs = S().forecasts
        hist = fs.histories[sku_id]
        return {
            "sku_id": sku_id,
            "history_tail": hist[-history:],
            "forecast": [round(float(x), 2) for x in fs.forecast(sku_id, horizon)],
            "sigma": round(fs.sigma(sku_id), 2),
            "backtest_7d": backtest(hist, 7),
        }

    @app.get("/decisions")
    def decisions(limit: int = 50, agent: Literal["fulfillment", "replenishment"] | None = None):
        return [asdict(d) for d in S().decisions.recent(5000) if agent is None or d.agent == agent][-limit:]

    @app.get("/events")
    def events(limit: int = 100, kind: str | None = None):
        return [asdict(e) for e in S().bus.recent(limit, kind)]

    @app.get("/metrics")
    def metrics():
        return S().metrics()

    @app.get("/metrics/daily")
    def metrics_daily():
        return S().daily

    @app.get("/inventory/overview")
    def inventory_overview():
        """Per-SKU stock across the network with forecast cover, for the inventory page."""
        sim = S()
        rows = []
        for sku in sim.catalog.values():
            fc7 = float(sim.forecasts.forecast(sku.sku_id, 7).sum())
            per_wh = {}
            for w in sim.warehouses.values():
                inv = w.inventory
                per_wh[w.warehouse_id] = {
                    "on_hand": inv.on_hand_total(sku.sku_id),
                    "available": inv.available(sku.sku_id),
                    "on_order": sim.replenishment.on_order[(w.warehouse_id, sku.sku_id)],
                    "online": w.online,
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

    @app.get("/purchase-orders")
    def purchase_orders():
        return [asdict(p) for p in sorted(S().replenishment.inbound, key=lambda p: p.arrival_day)]

    @app.get("/", include_in_schema=False)
    def root():
        return RedirectResponse("/dashboard")

    @app.get("/dashboard", include_in_schema=False)
    def dashboard():
        return FileResponse(STATIC / "dashboard.html")

    @app.get("/dashboard/state")
    def dashboard_state():
        """Everything the live dashboard draws, in one call."""
        sim = S()
        whs = []
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
        noisy = {"allocation.created", "job.queued", "agent.decision", "order.created", "job.done",
                 "vision.low_confidence", "day.closed"}
        return {
            "tick": sim.tick,
            "day": sim.day,
            "ticks_per_day": sim.cfg.ticks_per_day,
            "metrics": sim.metrics(),
            "daily": sim.daily,
            "warehouses": whs,
            "skus": [{"sku_id": s.sku_id, "name": s.name} for s in sim.catalog.values()],
            "decisions": [asdict(d) for d in sim.decisions.recent(40)][::-1],
            "events": [asdict(e) for e in sim.bus.log if e.kind not in noisy][-40:][::-1],
        }

    @app.post("/simulation/step")
    def step(body: StepIn):
        sim = S()
        if sim.day >= MAX_DAYS:
            raise HTTPException(409, f"The simulation reached day {MAX_DAYS}. Reset to start over.")
        sim.run_ticks(min(body.ticks, (MAX_DAYS * sim.cfg.ticks_per_day) - sim.tick))
        return S().metrics()

    @app.post("/simulation/reset")
    def reset(body: ResetIn):
        cfg = SimConfig(seed=body.seed)
        if not body.outage:
            cfg.outage = None
        state["sim"] = Simulation(cfg)
        return S().metrics()

    return app


app = create_app()
