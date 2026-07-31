"""Public augmentation API backed by Embodit's built-in algorithms."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import numpy as np

from augment import brightness
from augment.effects import recolor_frames, replace_background_frames
from augment.paths import SAM3_CHECKPOINT
from augment.sam3_adapter import Sam3Segmenter, write_frames_to_jpg_dir


def parse_prompts(raw: str | list[str] | None) -> list[str]:
    if raw is None:
        return []
    if isinstance(raw, list):
        parts = raw
    else:
        text = str(raw).replace("\n", ",").replace(";", ",")
        parts = text.split(",")
    return [part.strip() for part in parts if part and part.strip()]


def apply_brightness_videos(
    videos: dict[str, np.ndarray],
    *,
    mode: str = "auto",
    gain: float | None = None,
    gamma: float | None = None,
) -> tuple[dict[str, np.ndarray], dict]:
    """Apply brightness. mode=auto estimates per-camera curves; manual uses shared gain/gamma."""
    mode = (mode or "auto").lower()
    if mode == "manual":
        g = float(1.0 if gain is None else gain)
        gm = float(1.0 if gamma is None else gamma)
        g = float(np.clip(g, 0.4, 2.0))
        gm = float(np.clip(gm, 0.4, 2.0))
        out: dict[str, np.ndarray] = {}
        params: dict[str, Any] = {}
        for key, frames in videos.items():
            out[key] = brightness.apply_luma_curve(frames, g, gm)
            params[key] = {
                "gain": g,
                "gamma": gm,
                "result": brightness.result_metrics(out[key]),
            }
        params["_qa"] = {"mode": "manual", "shared_gain": g, "shared_gamma": gm}
        return out, {"mode": "manual", "gain": g, "gamma": gm, "cameras": params}

    out, meta = brightness.apply_brightness(videos)
    # Summarize estimated params for UI.
    gains = [v.get("gain") for k, v in meta.items() if k != "_qa" and isinstance(v, dict)]
    gammas = [v.get("gamma") for k, v in meta.items() if k != "_qa" and isinstance(v, dict)]
    return out, {
        "mode": "auto",
        "gain": float(np.median(gains)) if gains else None,
        "gamma": float(np.median(gammas)) if gammas else None,
        "cameras": meta,
    }


_SEGMENTER_CACHE: dict[str, Any] = {}


def get_segmenter(checkpoint: Path | None = None) -> Any:
    """Return a process-wide Sam3Segmenter (loading weights once per worker).

    The track cache is enabled so repeated previews / retries of the same
    episode reuse previous segmentation results.
    """
    ckpt = Path(checkpoint or os.environ.get("AUGMENT_SAM3_CHECKPOINT") or SAM3_CHECKPOINT)
    if not ckpt.is_file():
        raise FileNotFoundError(f"SAM3 权重不存在：{ckpt}")
    key = str(ckpt)
    segmenter = _SEGMENTER_CACHE.get(key)
    if segmenter is None:
        # ``device_id`` is fixed at 0: the worker maps the requested GPU via
        # CUDA_VISIBLE_DEVICES, so device 0 is always the selected card.
        segmenter = Sam3Segmenter(checkpoint=ckpt, device_id=0, cache_enabled=True)
        _SEGMENTER_CACHE[key] = segmenter
    return segmenter


def segment_by_prompts(frames: np.ndarray, prompts: list[str], segmenter: Any) -> tuple[np.ndarray, dict]:
    """Union masks from all prompts, writing the JPG frame dir only once."""
    import shutil
    import tempfile

    if not prompts:
        raise ValueError("颜色增强需要至少一个 SAM3 查询词")
    union = np.zeros(frames.shape[:3], dtype=bool)
    per_prompt: dict[str, Any] = {}
    frame_dir = Path(tempfile.mkdtemp(prefix="sam3_frames_"))
    try:
        write_frames_to_jpg_dir(frames, frame_dir)
        for prompt in prompts:
            masks = segmenter.segment_single_prompt_video(frames, prompt, frame_dir=frame_dir)
            ratio = float(masks.mean()) if masks.size else 0.0
            per_prompt[prompt] = {"mean_ratio": ratio, "any": bool(masks.any())}
            union |= masks.astype(bool)
    finally:
        shutil.rmtree(frame_dir, ignore_errors=True)
    if not union.any():
        raise RuntimeError("SAM3 未分割到任何区域，请更换查询词后重试：" + ", ".join(prompts))
    return union, {"prompts": prompts, "mean_ratio": float(union.mean()), "per_prompt": per_prompt}


def apply_color_video(
    frames: np.ndarray,
    *,
    prompts: list[str],
    apply_mode: str,
    color_rgb: list[int],
    segmenter: Any,
) -> tuple[np.ndarray, np.ndarray, dict]:
    """Color-augment a single camera. Returns (augmented, masks, meta)."""
    masks, seg_meta = segment_by_prompts(frames, prompts, segmenter)
    if apply_mode == "background_replace":
        aug, qa = replace_background_frames(frames, masks, color_rgb)
    else:
        aug, qa = recolor_frames(frames, masks, color_rgb)
    return aug, masks, {**seg_meta, "qa": qa}


def apply_color_videos(
    videos: dict[str, np.ndarray],
    *,
    prompts: list[str],
    apply_mode: str,
    color_rgb: list[int],
    gpu_id: int = 0,
    checkpoint: Path | None = None,
    segmenter: Any | None = None,
    camera_policy: str = "strict",
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray], dict]:
    """Return (augmented_videos, masks_by_camera, meta)."""
    if segmenter is None:
        segmenter = get_segmenter(checkpoint)
    out: dict[str, np.ndarray] = {}
    masks_out: dict[str, np.ndarray] = {}
    cameras_meta: dict[str, Any] = {}
    failures: list[str] = []

    for camera, frames in videos.items():
        try:
            aug, masks, cam_meta = apply_color_video(
                frames,
                prompts=prompts,
                apply_mode=apply_mode,
                color_rgb=color_rgb,
                segmenter=segmenter,
            )
            out[camera] = aug
            masks_out[camera] = masks
            cameras_meta[camera] = cam_meta
        except Exception as exc:  # noqa: BLE001
            failures.append(f"{camera}: {exc}")

    if not out:
        raise RuntimeError("所有相机颜色增强均失败：" + " | ".join(failures))
    if failures and camera_policy != "partial":
        raise RuntimeError("部分相机颜色增强失败（strict 策略）：" + " | ".join(failures))
    meta = {
        "applyMode": apply_mode,
        "colorRgb": list(color_rgb),
        "prompts": prompts,
        "cameras": cameras_meta,
        "failures": failures,
        "gpuId": gpu_id,
    }
    return out, masks_out, meta
