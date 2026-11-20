"""Warehouse floor as a grid: shelf cells are obstacles, robots drive the aisles.

Generated layout (n_aisles=2, aisle_length=3):

    x: 0 1 2 3 4 5 6
    y0 D . . . . . .     D = dock, row 0 and last row are cross-aisles
    y1 . S S . S S .     S = shelf (a bin location)
    y2 . S S . S S .     each shelf is picked from the aisle cell beside it
    y3 . S S . S S .
    y4 . . . . . . .
"""

from __future__ import annotations

from dataclasses import dataclass

from .models import Coord


@dataclass
class Layout:
    width: int
    height: int
    blocked: set[Coord]
    locations: dict[str, Coord]   # location_id -> shelf cell
    pick_faces: dict[str, Coord]  # location_id -> aisle cell a robot stands on to pick
    dock: Coord = (0, 0)

    @classmethod
    def grid(cls, n_aisles: int = 4, aisle_length: int = 8) -> "Layout":
        width, height = 3 * n_aisles + 1, aisle_length + 2
        blocked: set[Coord] = set()
        locations: dict[str, Coord] = {}
        faces: dict[str, Coord] = {}
        for x in range(width):
            side = x % 3
            if side == 0:
                continue
            for y in range(1, aisle_length + 1):
                loc_id = f"{chr(65 + x // 3)}{side}-{y:02d}"
                blocked.add((x, y))
                locations[loc_id] = (x, y)
                faces[loc_id] = (x - 1, y) if side == 1 else (x + 1, y)
        return cls(width, height, blocked, locations, faces)

    def passable(self, c: Coord) -> bool:
        x, y = c
        return 0 <= x < self.width and 0 <= y < self.height and c not in self.blocked

    def neighbors(self, c: Coord) -> list[Coord]:
        x, y = c
        cand = ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
        return [n for n in cand if self.passable(n)]

    def aisle_of(self, location_id: str) -> str:
        return location_id[0]
