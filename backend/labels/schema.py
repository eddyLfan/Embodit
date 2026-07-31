"""Label record validation."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

PRESET_TAGS = [
    "adaptation_frame",
    "intervention",
    "collision",
    "keep_candidate",
    "task_phase:grasp",
    "task_phase:place",
    "task_phase:move",
]

VALID_TARGETS = {"episode", "interval", "frame"}


def validate_label(raw: dict[str, Any]) -> dict[str, Any]:
    target = str(raw.get("target") or "episode")
    if target not in VALID_TARGETS:
        raise ValueError(f"target 必须是 {sorted(VALID_TARGETS)}")
    episode_index = int(raw["episode_index"])
    quality = raw.get("quality_score")
    if quality is not None:
        quality = int(quality)
        if quality < 1 or quality > 5:
            raise ValueError("quality_score 必须在 1–5")
    label = {
        "version": int(raw.get("version") or 1),
        "target": target,
        "episode_index": episode_index,
        "start_s": float(raw["start_s"]) if raw.get("start_s") is not None else None,
        "end_s": float(raw["end_s"]) if raw.get("end_s") is not None else None,
        "frame_index": int(raw["frame_index"]) if raw.get("frame_index") is not None else None,
        "tags": list(raw.get("tags") or []),
        "quality_score": quality,
        "success": raw.get("success"),
        "note": str(raw.get("note") or ""),
        "updated_at": str(raw.get("updated_at") or datetime.now(timezone.utc).isoformat()),
        "updated_by": str(raw.get("updated_by") or "user"),
    }
    if target == "interval":
        if label["start_s"] is None or label["end_s"] is None:
            raise ValueError("interval 标签需要 start_s 与 end_s")
    if target == "frame" and label["frame_index"] is None and label["start_s"] is None:
        raise ValueError("frame 标签需要 frame_index 或 start_s")
    return label
