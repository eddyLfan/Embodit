"""DatasetAdapter / Writer interfaces."""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .view import DatasetView


class DatasetAdapter(ABC):
    format_id: str

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser().resolve()

    @classmethod
    @abstractmethod
    def detect(cls, path: Path) -> bool:
        raise NotImplementedError

    @abstractmethod
    def inspect(self) -> DatasetView:
        raise NotImplementedError

    def get_timeseries(self, episode_index: int, keys: list[str] | None = None) -> dict[str, np.ndarray]:
        """Return state/action arrays for one episode. Keys default to common names."""
        raise NotImplementedError(f"{self.format_id} does not expose timeseries yet")

    def iter_frames(
        self, episode_index: int, camera_key: str
    ) -> Iterator[tuple[float, bytes]]:
        """Yield (timestamp_s, jpeg_or_png_bytes) for frame-based cameras."""
        raise NotImplementedError(f"{self.format_id} does not expose frame iteration")

    def get_frames(self, episode_index: int, camera_key: str):
        """Yield decoded uint8 RGB frames for frame-based cameras (optional capability)."""
        raise NotImplementedError(f"{self.format_id} does not expose decoded frames")

    def export_subset(
        self,
        output: Path,
        episode_indices: list[int],
        media_mode: str = "hardlink",
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{self.format_id} subset export not implemented")


class DatasetWriter(ABC):
    format_id: str

    @abstractmethod
    def write_from_episodes(
        self,
        output: Path,
        episodes: Any,
        meta: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a dataset from normalized episode payloads.

        ``episodes`` is any iterable (often a generator — consume it once,
        streaming). Each episode dict may contain:
          episode_index, length, fps, tasks, state, action,
          images{cam: iterable of uint8 RGB frames}, video_paths{cam: path}

        The result dict must include ``output``, ``totalEpisodes`` and
        ``totalFrames``.
        """
        raise NotImplementedError
