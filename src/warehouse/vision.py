"""Shelf cameras and stock reconciliation.

A `Detector` turns a camera frame of one bin into a count + confidence. Here the
detector is simulated (true count plus noise and occlusion); a real one would
wrap an object-detection model and implement the same `detect` signature.

The reconciler only overwrites the system count when the camera is confident
*and* has seen the same different value on consecutive scans, so one noisy frame
never corrupts inventory. Low-confidence frames are flagged for a manual count.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Protocol

import numpy as np

from .events import EventBus
from .inventory import Inventory


@dataclass(frozen=True)
class Detection:
    camera_id: str
    location_id: str
    sku_id: str
    count: int
    confidence: float


class Detector(Protocol):
    def detect(self, camera_id: str, location_id: str, sku_id: str, true_count: int) -> Detection: ...


class SimulatedDetector:
    def __init__(self, rng: np.random.Generator, noise_rate: float = 0.03, occlusion_rate: float = 0.05) -> None:
        self.rng = rng
        self.noise_rate = noise_rate
        self.occlusion_rate = occlusion_rate

    def detect(self, camera_id: str, location_id: str, sku_id: str, true_count: int) -> Detection:
        count, conf = true_count, float(self.rng.uniform(0.88, 0.99))
        if self.rng.random() < self.occlusion_rate:  # forklift / person in frame
            count = max(0, true_count + int(self.rng.integers(-5, 6)))
            conf = float(self.rng.uniform(0.3, 0.7))
        elif self.rng.random() < self.noise_rate:  # off-by-one miscount
            count = max(0, true_count + int(self.rng.choice([-1, 1])))
        return Detection(camera_id, location_id, sku_id, count, conf)


class ShelfCamera:
    def __init__(self, camera_id: str, location_ids: list[str], detector: Detector) -> None:
        self.camera_id = camera_id
        self.location_ids = location_ids
        self.detector = detector

    def scan(self, physical: Mapping[str, int], bin_sku: Mapping[str, str]) -> list[Detection]:
        return [
            self.detector.detect(self.camera_id, loc, bin_sku[loc], physical.get(loc, 0))
            for loc in self.location_ids
            if loc in bin_sku
        ]


class StockReconciler:
    def __init__(self, inventory: Inventory, bus: EventBus, min_confidence: float = 0.8, confirmations: int = 2) -> None:
        self.inventory = inventory
        self.bus = bus
        self.min_confidence = min_confidence
        self.confirmations = confirmations
        self.pending: dict[str, tuple[int, int]] = {}  # location -> (observed count, consecutive sightings)

    def process(self, detections: list[Detection]) -> int:
        """Returns the number of inventory adjustments made."""
        adjusted = 0
        wh = self.inventory.warehouse_id
        for det in detections:
            if det.confidence < self.min_confidence:
                self.bus.publish("vision.low_confidence", warehouse_id=wh, location_id=det.location_id,
                                 confidence=round(det.confidence, 2))
                continue
            if det.count == 0:
                self.bus.publish("vision.empty_bin", warehouse_id=wh, location_id=det.location_id, sku_id=det.sku_id)
            system = self.inventory.on_hand[det.location_id]
            if det.count == system:
                self.pending.pop(det.location_id, None)
                continue
            prev = self.pending.get(det.location_id)
            sightings = prev[1] + 1 if prev and prev[0] == det.count else 1
            if sightings >= self.confirmations:
                self.inventory.adjust(det.location_id, det.count, reason=f"vision:{det.camera_id}")
                self.bus.publish("vision.discrepancy_fixed", warehouse_id=wh, location_id=det.location_id,
                                 sku_id=det.sku_id, system=system, observed=det.count)
                self.pending.pop(det.location_id, None)
                adjusted += 1
            else:
                self.pending[det.location_id] = (det.count, sightings)
        return adjusted
