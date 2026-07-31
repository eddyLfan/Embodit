"""Schema for the normalized episode payload exchanged between adapters and writers.

Historically this was a bare-dict convention; :class:`EpisodePayload` makes the
contract explicit and validates it at the producer boundary, while still
serializing to the same dict shape that writers consume.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable

import numpy as np


@dataclass
class EpisodePayload:
    """One episode's normalized data during cross-format conversion.

    - ``video_paths``: camera key -> absolute MP4 path on disk.
    - ``images``: camera key -> iterable of decoded uint8 RGB frames
      (usually a lazy generator so memory stays bounded).
    - ``state`` / ``action``: (T, D) float arrays, optional.
    """

    episode_index: int
    length: int = 0
    tasks: list[str] = field(default_factory=list)
    video_paths: dict[str, str] = field(default_factory=dict)
    images: dict[str, Iterable[np.ndarray]] = field(default_factory=dict)
    state: np.ndarray | None = None
    action: np.ndarray | None = None
    fps: float | None = None
    extras: dict[str, Any] = field(default_factory=dict)

    def validate(self) -> "EpisodePayload":
        if int(self.episode_index) < 0:
            raise ValueError(f"episode_index 非法：{self.episode_index}")
        length = int(self.length or 0)
        for name in ("state", "action"):
            value = getattr(self, name)
            if value is None:
                continue
            arr = np.asarray(value)
            if arr.ndim == 0:
                raise ValueError(f"episode {self.episode_index}: {name} 不是序列数据")
            if length and arr.shape[0] != length:
                # Tolerate off-by-frame sources but reject gross mismatch.
                if abs(int(arr.shape[0]) - length) > max(2, length // 10):
                    raise ValueError(
                        f"episode {self.episode_index}: {name} 帧数 {arr.shape[0]} 与 length {length} 严重不一致"
                    )
        for cam, path in self.video_paths.items():
            if not str(path):
                raise ValueError(f"episode {self.episode_index}: 相机 {cam} 的视频路径为空")
        return self

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "episode_index": int(self.episode_index),
            "length": int(self.length or 0),
            "tasks": list(self.tasks or []),
            "video_paths": dict(self.video_paths or {}),
            "images": dict(self.images or {}),
        }
        if self.state is not None:
            payload["state"] = self.state
        if self.action is not None:
            payload["action"] = self.action
        if self.fps is not None:
            payload["fps"] = float(self.fps)
        payload.update(self.extras or {})
        return payload
