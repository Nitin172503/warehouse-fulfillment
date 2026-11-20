"""Run the simulation from the terminal: `uv run warehouse-sim --days 14`."""

from __future__ import annotations

import argparse

from .simulation import SimConfig, Simulation


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="Simulate a warehouse fulfillment network")
    p.add_argument("--days", type=int, default=14)
    p.add_argument("--seed", type=int, default=7)
    p.add_argument("--orders-per-day", type=float, default=60)
    p.add_argument("--no-outage", action="store_true", help="disable the scheduled WH-CENTRAL outage")
    p.add_argument("--decisions", type=int, default=12, help="how many recent agent decisions to print")
    args = p.parse_args(argv)

    cfg = SimConfig(seed=args.seed, orders_per_day=args.orders_per_day)
    if args.no_outage:
        cfg.outage = None
    sim = Simulation(cfg)

    hdr = f"{'day':>3} {'orders':>6} {'shipped':>7} {'backord':>7} {'reroute':>7} {'short':>5} {'visfix':>6} {'POs':>4} {'util':>5}  online"
    print(hdr)
    print("-" * len(hdr))

    def on_day(m: dict) -> None:
        print(f"{m['day']:>3} {m['orders']:>6} {m['shipped']:>7} {m['open_backorders']:>7} {m['reroutes']:>7} "
              f"{m['short_picks']:>5} {m['vision_fixes']:>6} {m['purchase_orders']:>4} {m['robot_utilization']:>5.0%}  "
              f"{','.join(w.removeprefix('WH-') for w in m['warehouses_online'])}")

    sim.run_days(args.days, on_day)

    m = sim.metrics()
    print("\nSummary")
    for k in ("orders", "shipped", "in_progress", "open_backorders", "avg_cycle_ticks", "p95_cycle_ticks",
              "split_shipments", "reroutes", "short_picks", "shrinkage_events", "vision_fixes",
              "purchase_orders", "robot_utilization", "forecast_wape", "naive_wape"):
        print(f"  {k:<20} {m[k]}")

    print(f"\nLast {args.decisions} agent decisions")
    for d in sim.decisions.recent(args.decisions):
        who = d.order_id or d.details.get("po_id", "")
        print(f"  t={d.tick:<6} {d.agent:<13} {d.action:<9} {who:<11} {d.reason}")


if __name__ == "__main__":
    main()
