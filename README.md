# Warehouse Inventory & Fulfillment

A three-warehouse fulfillment network where **robots pick items**, **shelf cameras
check stock**, **ML forecasts demand**, and **agents route and reroute orders**.
Everything runs in a deterministic discrete-time simulation with an HTTP API on top.

**Live demo: https://nitin172503.github.io/warehouse-fulfillment/** (runs entirely in your browser, nothing to install)

[![Deploy to Render](https://render.com/images/deploy-to-render-button.svg)](https://render.com/deploy?repo=https://github.com/Nitin172503/warehouse-fulfillment)

## Quick start

```bash
uv sync                                  # Python 3.11+, numpy, fastapi
uv run warehouse-sim --days 14           # run the network, print daily metrics + agent decisions
uv run pytest                            # 18 tests
uv run uvicorn warehouse.api:app --reload  # web app at http://127.0.0.1:8000
```

## Web app

Open **http://127.0.0.1:8000** after starting the server (`/docs` is the raw API explorer for developers).

| Page | What you can do |
|---|---|
| **Overview** | Live floor maps of all three sites (robots, routes, bin stock, uncaught count errors), KPIs, orders-per-day chart, "needs attention" list. Simulate an outage or trigger a shelf scan per site. |
| **Orders** | Search and filter every order; click one for its progress, allocations and reroutes. **New order** places your own. |
| **Inventory** | Network stock per site, demand for the next 7 days, days of cover and low-stock status. |
| **Forecasting** | Per-product demand history and 14-day forecast with a 95% range, model error vs a naive guess, inbound purchase orders. |
| **Activity** | Every agent decision with its reason, and every exception on the floor. |

On the GitHub Pages demo each visitor gets their own simulation running in their browser.
On a server deployment (Render, Docker) everyone shares one simulation: if two people press Play it runs twice as fast,
and Reset restarts it for everyone. The clock pauses when the tab is in the background and stops at day 120.

The sidebar runs the simulation clock: Play/Pause (or press Space), speed 1×–60×, and reset.

## How it fits together

```
 orders ──► FulfillmentAgent ──reserve──► Inventory (system of record) ◄──adjust── StockReconciler ◄── ShelfCamera
               │   ▲   ▲                       │                                                     (Detector)
          dispatch │   │ pick.short / outage   │ commit_pick                                              │
               ▼   │   │                       ▼                                                          │
            Fleet (robots, A*) ──picks from──► physical shelves ◄── shrinkage (sim) ─── observed by ──────┘
                                                                        
 daily demand ──► ForecastService (ridge) ──► ReplenishmentAgent ──► purchase orders ──► receive into bins
```

All components talk through an `EventBus`, so the event log is a full audit trail
and every agent decision is recorded with a plain-language reason.

| Module | Responsibility |
|---|---|
| `models.py` | SKU, Order, OrderLine, Allocation, order status |
| `inventory.py` | Per-warehouse stock by bin: reserve → pick → commit, adjustments |
| `layout.py` | Grid floor plan: shelves, aisles, pick faces, dock |
| `robots.py` | A* path planning, nearest-neighbour pick tours, battery/charging, job queue |
| `vision.py` | `Detector` protocol, simulated detector, camera-per-aisle, reconciler |
| `forecasting.py` | Ridge regression (trend, day-of-week, lags), backtest, reorder points |
| `agents.py` | `FulfillmentAgent` (routing/rerouting), `ReplenishmentAgent` (POs), decision log |
| `network.py` | `Warehouse`: wires inventory + physical shelves + fleet + cameras |
| `simulation.py` | Order generation, shrinkage, scheduled outage, daily cycle, metrics |
| `api.py` / `cli.py` | FastAPI service / terminal runner |
| `static/dashboard.html` | The web app (single file, no build step) |
| `static/engine.js` | JavaScript port of the simulation for the serverless GitHub Pages build |

## Key behaviours

**Routing.** Cheapest single warehouse that can ship the whole order wins
(cost = distance × shipping rate + robot backlog × penalty). If none can, it
splits greedily to maximise units covered per unit of cost; anything left over
is backordered and retried as stock arrives.

**Rerouting.** The agent moves orders when:
- a warehouse goes offline (queued jobs are cancelled, reservations released, orders re-planned);
- a robot **short-picks** because stock the system believed in isn't there (the bin count is corrected and the shortfall goes elsewhere);
- a robot queue exceeds `max_queue` and another site can take the whole job (load balancing);
- a backordered order becomes fillable.

**Vision.** Each aisle has a camera. The reconciler ignores low-confidence
frames (flagged for manual count) and only overwrites the system count after the
same discrepancy is seen on consecutive scans, so one noisy frame never corrupts
inventory. Swap `SimulatedDetector` for a real model by implementing `detect()`.

**Forecasting & replenishment.** Network demand per SKU is forecast with ridge
regression and scored daily against a seasonal-naive baseline (`forecast_wape`
vs `naive_wape`). Reorder point = lead-time demand + z·σ·√lead time, split per
warehouse by its recent share of allocations.

## API

| Method | Path | |
|---|---|---|
| GET | `/warehouses`, `/warehouses/{id}/inventory` | network state, robots, stock by bin |
| POST | `/warehouses/{id}/outage` · `/restore` · `/scan` | trigger outage / restore / camera sweep |
| POST/GET | `/orders`, `/orders/{id}`, `/orders?status=` | place and track orders |
| GET | `/forecast/{sku}?horizon=14` | forecast, σ, 7-day backtest |
| GET | `/decisions`, `/events`, `/metrics`, `/metrics/daily` | explainability and KPIs |
| POST | `/simulation/step`, `/simulation/reset` | advance or restart the clock |

## Known simplifications / next steps

- Robots don't avoid each other (no multi-agent path finding / reservation table).
- One robot handles one order per trip; no batch or zone picking.
- Unlimited bin capacity; no put-away planning or slotting optimisation.
- In-memory state; a real deployment would persist to Postgres and stream events via Kafka/NATS.
- Shipping cost is straight-line distance; no carrier cut-offs or SLAs.

## Deploying

**GitHub Pages (free, no server).** Every push to `main` runs the tests, builds a static copy of the
web app (`scripts/build_site.sh`) and publishes it via `.github/workflows/pages.yml`. In that build,
`src/warehouse/static/engine.js` — a JavaScript port of the simulation — runs in the visitor's browser
and answers the same routes as the FastAPI app, so `dashboard.html` is identical in both modes.
Keep `engine.js` in step with the Python modules when you change simulation behaviour;
`node tests/engine_check.cjs` smoke-tests it.

**Render / Docker (Python server).** The repo also includes a `Dockerfile` and a Render blueprint
(`render.yaml`). Click **Deploy to Render** above. On the free plan the service sleeps after ~15 idle
minutes and takes 30–60 s to wake.
