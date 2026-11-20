"""Daily demand forecasting.

Model: ridge regression on hand-built features (trend, day-of-week, lag-1,
lag-7, 7-day mean), forecast recursively for multi-day horizons. Small, fast,
no heavy dependencies, and it beats a seasonal-naive baseline on weekly-seasonal
demand. Residual spread feeds safety stock and reorder points.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np


def _features(y: np.ndarray, t: int) -> np.ndarray:
    dow = np.zeros(6)
    if t % 7 < 6:
        dow[t % 7] = 1.0
    return np.concatenate(([1.0, t / 365.0], dow, [y[t - 1], y[t - 7], y[t - 7 : t].mean()]))


@dataclass
class FittedModel:
    weights: np.ndarray | None  # None => not enough history, use level fallback
    sigma: float
    level: float


class RidgeForecaster:
    def __init__(self, alpha: float = 1.0, min_history: int = 21) -> None:
        self.alpha = alpha
        self.min_history = min_history

    def fit(self, history: np.ndarray | list[float]) -> FittedModel:
        y = np.asarray(history, dtype=float)
        level = float(y[-7:].mean()) if len(y) else 0.0
        if len(y) < self.min_history:
            sigma = float(y[-14:].std()) if len(y) > 1 else math.sqrt(max(level, 1.0))
            return FittedModel(None, sigma, level)
        X = np.array([_features(y, t) for t in range(7, len(y))])
        target = y[7:]
        reg = self.alpha * np.eye(X.shape[1])
        reg[0, 0] = 0.0  # don't shrink the intercept
        w = np.linalg.solve(X.T @ X + reg, X.T @ target)
        resid = target - X @ w
        return FittedModel(w, float(resid.std(ddof=1)), level)

    def predict(self, model: FittedModel, history: np.ndarray | list[float], horizon: int) -> np.ndarray:
        if model.weights is None:
            return np.full(horizon, model.level)
        y = np.asarray(history, dtype=float)
        ext = np.concatenate([y, np.zeros(horizon)])
        for t in range(len(y), len(y) + horizon):
            ext[t] = max(0.0, float(_features(ext, t) @ model.weights))
        return ext[len(y):]


def wape(actual: np.ndarray, pred: np.ndarray) -> float:
    denom = float(np.abs(actual).sum())
    return float(np.abs(actual - pred).sum() / denom) if denom else 0.0


def backtest(history: list[float] | np.ndarray, horizon: int = 7, forecaster: RidgeForecaster | None = None) -> dict:
    """Hold out the last `horizon` days; compare model vs seasonal-naive (same day last week)."""
    f = forecaster or RidgeForecaster()
    y = np.asarray(history, dtype=float)
    train, test = y[:-horizon], y[-horizon:]
    pred = f.predict(f.fit(train), train, horizon)
    naive = np.array([train[len(train) - 7 + (h % 7)] for h in range(horizon)])
    return {"wape_model": wape(test, pred), "wape_seasonal_naive": wape(test, naive)}


class ForecastService:
    """Holds one demand history + fitted model per key (e.g. SKU)."""

    def __init__(self, forecaster: RidgeForecaster | None = None) -> None:
        self.forecaster = forecaster or RidgeForecaster()
        self.histories: dict[str, list[float]] = {}
        self.models: dict[str, FittedModel] = {}

    def record(self, key: str, value: float) -> None:
        self.histories.setdefault(key, []).append(float(value))

    def refit(self) -> None:
        for key, hist in self.histories.items():
            self.models[key] = self.forecaster.fit(hist)

    def forecast(self, key: str, horizon: int = 7) -> np.ndarray:
        hist = self.histories.get(key, [])
        model = self.models.get(key) or self.forecaster.fit(hist)
        return self.forecaster.predict(model, hist, horizon)

    def sigma(self, key: str) -> float:
        model = self.models.get(key) or self.forecaster.fit(self.histories.get(key, []))
        return model.sigma

    def reorder_point(self, key: str, lead_time_days: int, z: float = 1.65, share: float = 1.0) -> float:
        """Expected demand over the lead time + safety stock (z=1.65 ≈ 95% cycle service level)."""
        lead_demand = float(self.forecast(key, lead_time_days).sum())
        safety = z * self.sigma(key) * math.sqrt(lead_time_days)
        return share * (lead_demand + safety)
