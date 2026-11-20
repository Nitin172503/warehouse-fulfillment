"""HTTP API. Run: `uv run uvicorn warehouse.api:app --reload`.

A thin wrapper over `warehouse.service.Service`: all logic lives there so the same
code also runs in the browser (static build). Each visitor gets their own simulation,
tracked by a session cookie; idle sessions are evicted.
"""

from __future__ import annotations

import secrets
import time
from collections import OrderedDict
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

from .models import OrderStatus
from .service import MAX_STEP_TICKS, HttpError, Service
from .simulation import Simulation

STATIC = Path(__file__).parent / "static"
COOKIE = "wh_session"


class SessionStore:
    """One Service per browser session, least-recently-used eviction."""

    def __init__(self, max_sessions: int = 200, idle_seconds: int = 1800, shared: Service | None = None) -> None:
        self.max_sessions = max_sessions
        self.idle_seconds = idle_seconds
        self.shared = shared  # tests / single-user mode: everyone gets this one
        self._sessions: OrderedDict[str, tuple[Service, float]] = OrderedDict()

    def get(self, sid: str | None) -> tuple[Service, str, bool]:
        if self.shared is not None:
            return self.shared, sid or "shared", False
        now = time.monotonic()
        for old, (_, seen) in list(self._sessions.items()):  # evict idle
            if now - seen > self.idle_seconds:
                del self._sessions[old]
        if sid and sid in self._sessions:
            service, _ = self._sessions.pop(sid)
            self._sessions[sid] = (service, now)
            return service, sid, False
        while len(self._sessions) >= self.max_sessions:
            self._sessions.popitem(last=False)
        sid = secrets.token_urlsafe(16)
        service = Service()
        self._sessions[sid] = (service, now)
        return service, sid, True

    def __len__(self) -> int:
        return len(self._sessions)


class LineIn(BaseModel):
    sku_id: str
    qty: int = Field(gt=0)


class OrderIn(BaseModel):
    lines: list[LineIn] = Field(min_length=1)
    destination: tuple[int, int]


class StepIn(BaseModel):
    ticks: int = Field(default=60, ge=1, le=MAX_STEP_TICKS)


class ScenarioIn(BaseModel):
    seed: int | None = None
    outage: str | bool | None = None
    outage_start: int | None = None
    outage_days: int | None = None
    orders_per_day: float | None = None
    robots: int | None = None


class ResetIn(BaseModel):
    scenario: ScenarioIn | None = None


def create_app(sim: Simulation | None = None, *, max_sessions: int = 200) -> FastAPI:
    """`sim` pins a single shared simulation (handy for tests); otherwise one per session."""
    app = FastAPI(title="Warehouse Fulfillment", version="0.2.0")
    store = SessionStore(max_sessions=max_sessions, shared=Service(sim=sim) if sim else None)
    app.state.sessions = store

    @app.middleware("http")
    async def session_middleware(request: Request, call_next):
        service, sid, new = store.get(request.cookies.get(COOKIE))
        request.state.service = service
        response = await call_next(request)
        if new:
            response.set_cookie(COOKIE, sid, max_age=7 * 24 * 3600, httponly=True, samesite="lax")
        return response

    @app.exception_handler(HttpError)
    async def http_error(_: Request, exc: HttpError):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status)

    def svc(request: Request) -> Service:
        return request.state.service

    # Handlers are async so a session's requests run one at a time on the event loop.
    @app.get("/health")
    async def health(s: Service = Depends(svc)):
        return s.health()

    @app.get("/catalog")
    async def catalog(s: Service = Depends(svc)):
        return s.catalog()

    @app.get("/warehouses")
    async def warehouses(s: Service = Depends(svc)):
        return s.warehouses()

    @app.get("/warehouses/{wid}/inventory")
    async def inventory(wid: str, s: Service = Depends(svc)):
        return s.inventory(wid)

    @app.post("/warehouses/{wid}/outage")
    async def outage(wid: str, s: Service = Depends(svc)):
        return s.outage(wid)

    @app.post("/warehouses/{wid}/restore")
    async def restore(wid: str, s: Service = Depends(svc)):
        return s.restore_site(wid)

    @app.post("/warehouses/{wid}/scan")
    async def scan(wid: str, s: Service = Depends(svc)):
        return s.scan(wid)

    @app.post("/orders", status_code=201)
    async def create_order(body: OrderIn, s: Service = Depends(svc)):
        return s.create_order([(l.sku_id, l.qty) for l in body.lines], body.destination)

    @app.get("/orders")
    async def list_orders(status: OrderStatus | None = None, limit: int = 50, s: Service = Depends(svc)):
        return s.list_orders(status.value if status else None, limit)

    @app.get("/orders/{oid}")
    async def get_order(oid: str, s: Service = Depends(svc)):
        return s.get_order(oid)

    @app.get("/forecast/{sku_id}")
    async def forecast(sku_id: str, horizon: int = 14, history: int = 14, s: Service = Depends(svc)):
        return s.forecast(sku_id, horizon, history)

    @app.get("/decisions")
    async def decisions(limit: int = 50, agent: Literal["fulfillment", "replenishment"] | None = None, s: Service = Depends(svc)):
        return s.decisions(limit, agent)

    @app.get("/events")
    async def events(limit: int = 100, kind: str | None = None, s: Service = Depends(svc)):
        return s.events(limit, kind)

    @app.get("/metrics")
    async def metrics(s: Service = Depends(svc)):
        return s.metrics()

    @app.get("/metrics/daily")
    async def metrics_daily(s: Service = Depends(svc)):
        return s.metrics_daily()

    @app.get("/inventory/overview")
    async def inventory_overview(s: Service = Depends(svc)):
        return s.inventory_overview()

    @app.get("/purchase-orders")
    async def purchase_orders(s: Service = Depends(svc)):
        return s.purchase_orders()

    @app.post("/simulation/step")
    async def step(body: StepIn, s: Service = Depends(svc)):
        return s.step(body.ticks)

    @app.post("/simulation/reset")
    async def reset(body: ResetIn, s: Service = Depends(svc)):
        # exclude_unset (not exclude_none): an explicit `"outage": null` means "no outage"
        scenario = body.scenario.model_dump(exclude_unset=True) if body.scenario else None
        return s.reset(scenario)

    @app.get("/dashboard/state")
    async def dashboard_state(s: Service = Depends(svc)):
        return s.dashboard_state()

    @app.get("/", include_in_schema=False)
    async def root(request: Request):
        query = f"?{request.url.query}" if request.url.query else ""
        return RedirectResponse(f"/dashboard{query}")

    @app.get("/dashboard", include_in_schema=False)
    async def dashboard():
        return FileResponse(STATIC / "dashboard.html")

    return app


app = create_app()
