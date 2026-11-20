"""Core domain types shared by every subsystem."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum

Coord = tuple[int, int]


@dataclass(frozen=True)
class SKU:
    sku_id: str
    name: str
    lead_time_days: int = 3
    unit_cost: float = 10.0


class OrderStatus(str, Enum):
    PENDING = "pending"          # received, not yet routed
    ALLOCATED = "allocated"      # fully reserved, being picked
    BACKORDERED = "backordered"  # some quantity could not be reserved anywhere
    SHIPPED = "shipped"
    CANCELLED = "cancelled"


@dataclass
class OrderLine:
    sku_id: str
    qty: int


@dataclass
class Allocation:
    """A quantity of one SKU promised to an order by one warehouse.

    `qty` shrinks if a robot short-picks, so the shortfall immediately shows up
    as unallocated demand that the routing agent can send elsewhere.
    """

    warehouse_id: str
    sku_id: str
    qty: int
    picked: int = 0
    done: bool = False
    cancelled: bool = False


@dataclass
class Order:
    order_id: str
    lines: list[OrderLine]
    destination: Coord
    created_tick: int = 0
    status: OrderStatus = OrderStatus.PENDING
    allocations: list[Allocation] = field(default_factory=list)
    shipped_tick: int | None = None
    reroute_count: int = 0

    def demand(self) -> dict[str, int]:
        out: dict[str, int] = defaultdict(int)
        for line in self.lines:
            out[line.sku_id] += line.qty
        return dict(out)

    def unallocated(self) -> dict[str, int]:
        covered: dict[str, int] = defaultdict(int)
        for a in self.allocations:
            if not a.cancelled:
                covered[a.sku_id] += a.qty
        return {s: q - covered[s] for s, q in self.demand().items() if q - covered[s] > 0}

    def is_complete(self) -> bool:
        return (
            bool(self.allocations)
            and not self.unallocated()
            and all(a.done or a.cancelled for a in self.allocations)
        )

    def warehouses(self) -> list[str]:
        return sorted({a.warehouse_id for a in self.allocations if not a.cancelled})
