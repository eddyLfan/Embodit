"""Write preview contact images + short comparison videos."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from augment.video_io import encode_video_mp4


def _save_jpeg(path: Path, rgb: np.ndarray) -> None:
    from PIL import Image

    path.parent.mkdir(parents=True, exist_ok=True)
    Image.fromarray(np.ascontiguousarray(rgb)).save(path, format="JPEG", quality=90)


def _overlay_mask(frame: np.ndarray, mask: np.ndarray, color=(0, 220, 120), alpha: float = 0.45) -> np.ndarray:
    return _overlay_mask_video(frame[None], mask[None], color=color, alpha=alpha)[0]


def _overlay_mask_video(
    frames: np.ndarray,
    masks: np.ndarray,
    color=(0, 220, 120),
    alpha: float = 0.45,
) -> np.ndarray:
    """Vectorized tint over masked pixels for the whole clip at once."""
    out = frames.copy()
    m = masks.astype(bool)
    if m.any():
        tint = np.asarray(color, dtype=np.float32)
        blended = frames[m].astype(np.float32) * (1.0 - alpha) + tint * alpha
        out[m] = np.clip(blended, 0, 255).astype(frames.dtype)
    return out


def _side_by_side(left: np.ndarray, right: np.ndarray) -> np.ndarray:
    n = min(len(left), len(right))
    return np.concatenate([left[:n], right[:n]], axis=2)


def write_preview(
    preview_dir: Path,
    *,
    source_videos: dict[str, np.ndarray],
    augmented_videos: dict[str, np.ndarray],
    masks: dict[str, np.ndarray] | None,
    meta: dict[str, Any],
    frame_index: int | None = None,
    fps: float = 30.0,
) -> dict[str, Any]:
    preview_dir = Path(preview_dir)
    preview_dir.mkdir(parents=True, exist_ok=True)
    cameras = sorted(augmented_videos.keys())
    assets: list[dict[str, Any]] = []
    fps = float(fps or meta.get("fps") or 30.0)

    for camera in cameras:
        src = source_videos[camera]
        aug = augmented_videos[camera]
        idx = frame_index if frame_index is not None else max(0, len(src) // 2)
        idx = min(idx, len(src) - 1, len(aug) - 1)
        cam_dir = preview_dir / _safe_name(camera)
        cam_dir.mkdir(parents=True, exist_ok=True)

        original_jpg = cam_dir / "original.jpg"
        result_jpg = cam_dir / "result.jpg"
        original_mp4 = cam_dir / "original.mp4"
        result_mp4 = cam_dir / "result.mp4"
        compare_mp4 = cam_dir / "compare.mp4"
        _save_jpeg(original_jpg, src[idx])
        _save_jpeg(result_jpg, aug[idx])
        encode_video_mp4(src, original_mp4, fps=fps)
        encode_video_mp4(aug, result_mp4, fps=fps)
        encode_video_mp4(_side_by_side(src, aug), compare_mp4, fps=fps)

        safe = _safe_name(camera)
        entry: dict[str, Any] = {
            "camera": camera,
            "frameIndex": idx,
            "original": original_jpg.name,
            "result": result_jpg.name,
            "originalVideo": original_mp4.name,
            "resultVideo": result_mp4.name,
            "compareVideo": compare_mp4.name,
            "relative": {
                "original": f"{safe}/original.jpg",
                "result": f"{safe}/result.jpg",
                "originalVideo": f"{safe}/original.mp4",
                "resultVideo": f"{safe}/result.mp4",
                "compareVideo": f"{safe}/compare.mp4",
            },
        }
        if masks and camera in masks:
            mask_frames = masks[camera]
            overlay = _overlay_mask_video(src, mask_frames)
            overlay_jpg = cam_dir / "mask.jpg"
            overlay_mp4 = cam_dir / "mask.mp4"
            _save_jpeg(overlay_jpg, overlay[idx])
            encode_video_mp4(overlay, overlay_mp4, fps=fps)
            entry["mask"] = overlay_jpg.name
            entry["maskVideo"] = overlay_mp4.name
            entry["relative"]["mask"] = f"{safe}/mask.jpg"
            entry["relative"]["maskVideo"] = f"{safe}/mask.mp4"
        assets.append(entry)

    payload = {"meta": meta, "cameras": assets, "fps": fps}
    (preview_dir / "meta.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return payload


def _safe_name(camera: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in camera)
