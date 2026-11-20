"""One fulfillment center: system inventory + physical shelves + robots + cameras."""

from __future__ import annotations

import zlib
from collections import defaultdict

import numpy as np

from .events import EventBus
from .inventory import Inventory
from .layout import Layout
from .models import Coord
from .robots import Fleet, PickJob, PickTask
from .vision import Detector, ShelfCamera, SimulatedDetector, StockReconciler


class Warehouse:
    def __init__(
        self,
        warehouse_id: str,
        position: Coord,
        bus: EventBus,
        *,
        layout: Layout | None = None,
        n_robots: int = 3,
        detector: Detector | None = None,
    ) -> None:
        self.warehouse_id = warehouse_id
        self.position = position
        self.bus = bus
        self.online = True
        self.layout = layout or Layout.grid()
        self.inventory = Inventory(warehouse_id, bus)
        # Ground truth of what is physically on each shelf. In production this is
        # unknowable; here it lets us simulate shrinkage the system must discover.
        self.physical: dict[str, int] = defaultdict(int)
        self.fleet = Fleet(self.layout, n_robots, self._on_pick, self._on_job_done, prefix=f"{warehouse_id}-R")
        detector = detector or SimulatedDetector(np.random.default_rng(zlib.crc32(warehouse_id.encode())))
        by_aisle: dict[str, list[str]] = defaultdict(list)
        for loc in sorted(self.layout.locations):
            by_aisle[self.layout.aisle_of(loc)].append(loc)
        self.cameras = [ShelfCamera(f"{warehouse_id}-CAM-{a}", locs, detector) for a, locs in by_aisle.items()]
        self.reconciler = StockReconciler(self.inventory, bus)
        self._job_seq = 0  # plain int (not itertools.count) so the simulation can be pickled

    # --- stock ----------------------------------------------------------
    def slot(self, sku_id: str, location_id: str, qty: int) -> None:
        if location_id not in self.layout.locations:
            raise KeyError(location_id)
        self.inventory.receive(sku_id, location_id, qty)
        self.physical[location_id] += qty

    def receive(self, sku_id: str, qty: int) -> None:
        """Put away inbound stock into the emptiest bin for that SKU."""
        locs = self.inventory.locations_for(sku_id)
        if not locs:
            raise KeyError(f"{sku_id} is not slotted in {self.warehouse_id}")
        loc = min(locs, key=lambda l: self.inventory.on_hand[l])
        self.slot(sku_id, loc, qty)

    # --- picking ----------------------------------------------------------
    @property
    def backlog(self) -> int:
        return self.fleet.backlog

    def dispatch(self, order_id: str, tasks: list[PickTask]) -> PickJob:
        self._job_seq += 1
        job = PickJob(f"{self.warehouse_id}-J{self._job_seq:05d}", order_id, self.warehouse_id, tasks,
                      created_tick=self.bus.tick)
        self.fleet.submit(job)
        self.bus.publish("job.queued", warehouse_id=self.warehouse_id, job_id=job.job_id, order_id=order_id)
        return job

    def cancel_job(self, job: PickJob) -> None:
        """Undo a queued (not started) job: free reservations, void allocations."""
        for task in job.tasks:
            self.inventory.release(task.location_id, task.qty)
            if task.allocation:
                task.allocation.cancelled = True
        self.bus.publish("job.cancelled", warehouse_id=self.warehouse_id, job_id=job.job_id, order_id=job.order_id)

    def _on_pick(self, task: PickTask) -> int:
        actual = min(self.physical[task.location_id], task.qty)
        self.physical[task.location_id] -= actual
        self.inventory.commit_pick(task.location_id, task.qty, actual)
        if task.allocation:
            task.allocation.picked += actual
        if actual < task.qty:
            short = task.qty - actual
            if task.allocation:
                task.allocation.qty -= short
            # The robot just saw the real count; correct the system immediately.
            self.inventory.adjust(task.location_id, self.physical[task.location_id], reason="short_pick")
            self.bus.publish("pick.short", warehouse_id=self.warehouse_id, order_id=task.order_id,
                             sku_id=task.sku_id, location_id=task.location_id, short=short)
        return actual

    def _on_job_done(self, job: PickJob) -> None:
        for task in job.tasks:
            if task.allocation:
                task.allocation.done = True
        self.bus.publish("job.done", warehouse_id=self.warehouse_id, job_id=job.job_id, order_id=job.order_id,
                         robot_id=job.robot_id)

    # --- vision -----------------------------------------------------------
    def scan_shelves(self) -> int:
        detections = [d for cam in self.cameras for d in cam.scan(self.physical, self.inventory.bin_sku)]
        return self.reconciler.process(detections)

    # --- control ----------------------------------------------------------
    def set_online(self, online: bool, reason: str = "") -> None:
        if online == self.online:
            return
        self.online = online
        kind = "warehouse.restored" if online else "warehouse.outage"
        self.bus.publish(kind, warehouse_id=self.warehouse_id, reason=reason)

    def step(self) -> None:
        # During an outage, robots already mid-tour finish; no new jobs start.
        self.fleet.step(accept_new=self.online)

    def summary(self) -> dict:
        return {
            "warehouse_id": self.warehouse_id,
            "position": list(self.position),
            "online": self.online,
            "backlog": self.backlog,
            "queued_jobs": len(self.fleet.queue),
            "robots": [
                {"robot_id": r.robot_id, "state": r.state.value, "pos": list(r.pos), "battery": round(r.battery, 1),
                 "job": r.job.job_id if r.job else None, "jobs_completed": r.jobs_completed, "distance": r.distance}
                for r in self.fleet.robots
            ],
        }
