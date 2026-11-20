"""System-of-record inventory for one warehouse.

Bins are single-SKU, so stock is keyed by location. `on_hand` is what the system
*believes* is on the shelf; the physical truth can drift (shrinkage, mis-picks)
and is corrected by short picks and by the shelf-camera reconciler.
"""

from __future__ import annotations

from collections import defaultdict

from .events import EventBus


class InsufficientStock(Exception):
    pass


class Inventory:
    def __init__(self, warehouse_id: str, bus: EventBus | None = None) -> None:
        self.warehouse_id = warehouse_id
        self.bus = bus
        self.bin_sku: dict[str, str] = {}
        self.on_hand: dict[str, int] = defaultdict(int)
        self.reserved: dict[str, int] = defaultdict(int)

    # --- slotting -------------------------------------------------------
    def assign(self, location_id: str, sku_id: str) -> None:
        current = self.bin_sku.get(location_id)
        if current is not None and current != sku_id:
            raise ValueError(f"{location_id} already holds {current}")
        self.bin_sku[location_id] = sku_id

    def locations_for(self, sku_id: str) -> list[str]:
        return [loc for loc, s in self.bin_sku.items() if s == sku_id]

    def skus(self) -> list[str]:
        return sorted(set(self.bin_sku.values()))

    # --- queries --------------------------------------------------------
    def available_at(self, location_id: str) -> int:
        return max(0, self.on_hand[location_id] - self.reserved[location_id])

    def available(self, sku_id: str) -> int:
        return sum(self.available_at(loc) for loc in self.locations_for(sku_id))

    def on_hand_total(self, sku_id: str) -> int:
        return sum(self.on_hand[loc] for loc in self.locations_for(sku_id))

    # --- mutations ------------------------------------------------------
    def receive(self, sku_id: str, location_id: str, qty: int) -> None:
        self.assign(location_id, sku_id)
        self.on_hand[location_id] += qty

    def reserve(self, sku_id: str, qty: int) -> list[tuple[str, int]]:
        """Reserve `qty` units, drawing from the fullest bins first.
        Returns the (location, qty) picks the robot must make."""
        if qty <= 0:
            return []
        if self.available(sku_id) < qty:
            raise InsufficientStock(f"{self.warehouse_id}: {sku_id} has {self.available(sku_id)}, need {qty}")
        picks: list[tuple[str, int]] = []
        remaining = qty
        for loc in sorted(self.locations_for(sku_id), key=self.available_at, reverse=True):
            take = min(remaining, self.available_at(loc))
            if take:
                self.reserved[loc] += take
                picks.append((loc, take))
                remaining -= take
            if not remaining:
                break
        return picks

    def release(self, location_id: str, qty: int) -> None:
        self.reserved[location_id] = max(0, self.reserved[location_id] - qty)

    def commit_pick(self, location_id: str, reserved_qty: int, picked_qty: int) -> None:
        self.release(location_id, reserved_qty)
        self.on_hand[location_id] = max(0, self.on_hand[location_id] - picked_qty)

    def adjust(self, location_id: str, counted: int, reason: str) -> int:
        delta = counted - self.on_hand[location_id]
        if delta:
            self.on_hand[location_id] = counted
            if self.bus:
                self.bus.publish(
                    "inventory.adjusted",
                    warehouse_id=self.warehouse_id,
                    location_id=location_id,
                    sku_id=self.bin_sku.get(location_id),
                    delta=delta,
                    reason=reason,
                )
        return delta

    def snapshot(self) -> list[dict]:
        return [
            {
                "sku_id": sku,
                "on_hand": self.on_hand_total(sku),
                "reserved": sum(self.reserved[l] for l in self.locations_for(sku)),
                "available": self.available(sku),
                "locations": {l: self.on_hand[l] for l in self.locations_for(sku)},
            }
            for sku in self.skus()
        ]
