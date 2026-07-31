"""Streaming per-feature statistics for LeRobot-style stats.json files.

min/max/mean/std/count are exact; quantiles are estimated from a bounded
random sample so memory stays constant on arbitrarily large datasets.
"""

from __future__ import annotations

from typing import Any

import numpy as np


class FeatureStats:
    def __init__(self, sample_cap: int = 100_000, seed: int = 0) -> None:
        self.sample_cap = int(sample_cap)
        self.count = 0
        self._sum: np.ndarray | None = None
        self._sumsq: np.ndarray | None = None
        self._min: np.ndarray | None = None
        self._max: np.ndarray | None = None
        self._samples: list[np.ndarray] = []
        self._sampled_rows = 0
        self._rng = np.random.default_rng(seed)

    def update(self, values: np.ndarray) -> None:
        values = np.asarray(values, dtype=np.float64)
        if values.ndim == 1:
            values = values[:, None]
        values = values.reshape(values.shape[0], -1)
        if values.shape[0] == 0:
            return
        if self._sum is None:
            dim = values.shape[1]
            self._sum = np.zeros(dim)
            self._sumsq = np.zeros(dim)
            self._min = np.full(dim, np.inf)
            self._max = np.full(dim, -np.inf)
        self.count += values.shape[0]
        self._sum += values.sum(axis=0)
        self._sumsq += (values * values).sum(axis=0)
        self._min = np.minimum(self._min, values.min(axis=0))
        self._max = np.maximum(self._max, values.max(axis=0))

        # Bounded quantile sample: accept incoming rows with probability
        # cap/seen, then trim the pool back to the cap when it overgrows.
        accept_p = min(1.0, self.sample_cap / max(self.count, 1))
        if accept_p >= 1.0:
            picked = values
        else:
            mask = self._rng.random(values.shape[0]) < accept_p
            picked = values[mask]
        if picked.shape[0]:
            self._samples.append(picked)
            self._sampled_rows += picked.shape[0]
        if self._sampled_rows > int(self.sample_cap * 1.5):
            pool = np.concatenate(self._samples, axis=0)
            keep = self._rng.choice(pool.shape[0], size=self.sample_cap, replace=False)
            self._samples = [pool[keep]]
            self._sampled_rows = self.sample_cap

    def result(self) -> dict[str, Any] | None:
        if self.count == 0 or self._sum is None:
            return None
        mean = self._sum / self.count
        variance = np.maximum(self._sumsq / self.count - mean * mean, 0.0)
        out: dict[str, Any] = {
            "min": self._min.tolist(),
            "max": self._max.tolist(),
            "mean": mean.tolist(),
            "std": np.sqrt(variance).tolist(),
            "count": [self.count],
        }
        if self._samples:
            pool = np.concatenate(self._samples, axis=0)
            for name, q in (("q01", 0.01), ("q10", 0.10), ("q50", 0.50), ("q90", 0.90), ("q99", 0.99)):
                out[name] = np.quantile(pool, q, axis=0).tolist()
        return out


class StatsCollector:
    def __init__(self, sample_cap: int = 100_000) -> None:
        self._features: dict[str, FeatureStats] = {}
        self._sample_cap = sample_cap

    def update(self, name: str, values: np.ndarray) -> None:
        feature = self._features.get(name)
        if feature is None:
            feature = self._features[name] = FeatureStats(sample_cap=self._sample_cap)
        feature.update(values)

    def to_stats_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for name, feature in self._features.items():
            stats = feature.result()
            if stats is not None:
                result[name] = stats
        return result
