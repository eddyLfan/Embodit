"""Unified in-memory view model (not an on-disk format)."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


FORMAT_LEROBOT_V21 = "lerobot_v21"
FORMAT_LEROBOT_V3 = "lerobot_v3"
FORMAT_HDF5 = "hdf5"
FORMAT_MCAP = "mcap"

SUPPORTED_FORMATS = (
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_HDF5,
    FORMAT_MCAP,
)

FORMAT_LABELS = {
    FORMAT_LEROBOT_V21: "LeRobot v2.1",
    FORMAT_LEROBOT_V3: "LeRobot v3",
    FORMAT_HDF5: "HDF5",
    FORMAT_MCAP: "MCAP",
}


@dataclass
class CameraRef:
    key: str
    kind: str  # video | frames | topic
    path: str | None = None
    from_timestamp: float | None = None
    to_timestamp: float | None = None
    topic: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class EpisodeView:
    episode_index: int
    length: int
    duration: float
    tasks: list[str] = field(default_factory=list)
    has_intervention: bool = False
    cameras: dict[str, CameraRef] = field(default_factory=dict)
    extras: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "episodeIndex": self.episode_index,
            "length": self.length,
            "duration": self.duration,
            "tasks": self.tasks,
            "hasIntervention": self.has_intervention,
            "videos": {
                key: {
                    "path": cam.path,
                    "fromTimestamp": cam.from_timestamp,
                    "toTimestamp": cam.to_timestamp,
                    "kind": cam.kind,
                    "topic": cam.topic,
                }
                for key, cam in self.cameras.items()
            },
            "extras": self.extras,
        }


@dataclass
class DatasetView:
    format_id: str
    path: str
    name: str
    fps: float
    robot_type: str | None
    features: dict[str, Any]
    episodes: list[EpisodeView]
    total_frames: int = 0
    total_tasks: int = 0
    extras: dict[str, Any] = field(default_factory=dict)

    def to_inspect_dict(self) -> dict[str, Any]:
        video_keys = sorted({key for ep in self.episodes for key in ep.cameras})
        return {
            "path": self.path,
            "name": self.name,
            "format": self.format_id,
            "formatLabel": FORMAT_LABELS.get(self.format_id, self.format_id),
            "codebaseVersion": self.extras.get("codebase_version"),
            "robotType": self.robot_type,
            "totalEpisodes": len(self.episodes),
            "totalFrames": self.total_frames or sum(ep.length for ep in self.episodes),
            "totalTasks": self.total_tasks,
            "fps": self.fps,
            "videoKeys": video_keys,
            "features": self.features,
            "episodes": [ep.to_dict() for ep in self.episodes],
            "extras": {k: v for k, v in self.extras.items() if k != "codebase_version"},
        }
