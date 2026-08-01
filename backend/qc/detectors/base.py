"""Detector protocol and per-episode context."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Protocol

import numpy as np

from qc.schema import DetectorOutcome


@dataclass
class EpisodeContext:
    adapter: Any
    view: Any
    episode: Any
    signals: dict[str, np.ndarray]
    config: dict[str, Any]
    cancelled: Callable[[], bool] | None = None

    @property
    def fps(self) -> float:
        return max(float(self.view.fps or 0), 1e-6)

    @property
    def duration(self) -> float:
        return max(float(self.episode.duration or self.episode.length / self.fps), 0.0)

    def check_cancelled(self) -> None:
        if self.cancelled and self.cancelled():
            raise ScanCancelled("QC 扫描已取消")

    def feature_names(self, key: str, dimensions: int) -> list[str]:
        raw = self.view.features.get(key, {}) if isinstance(self.view.features, dict) else {}
        names = list(raw.get("names") or []) if isinstance(raw, dict) else []
        if len(names) != dimensions:
            names = [f"{key}.d{index}" for index in range(dimensions)]
        return [str(item) for item in names]


class ScanCancelled(Exception):
    pass


class Detector(Protocol):
    detector_id: str
    version: str
    coverage_weight: float
    config_key: str

    def run(self, context: EpisodeContext) -> DetectorOutcome:
        ...


def as_matrix(value: np.ndarray) -> np.ndarray:
    array = np.asarray(value)
    if array.ndim == 1:
        return array[:, None]
    if array.ndim < 1:
        return array.reshape(0, 0)
    return array.reshape(array.shape[0], -1)
