"""Built-in, temporally stable brightness augmentation.

Adapted from the original ``data_strengthen`` implementation.  Keeping the
small algorithm in Embodit makes brightness augmentation work in a clean
checkout without an unpublished sibling repository.
"""

from __future__ import annotations

from typing import Any

import cv2
import numpy as np


def _sample_luma(frames: np.ndarray, max_frames: int = 32) -> np.ndarray:
    if len(frames) == 0:
        raise ValueError("brightness augmentation requires at least one frame")
    ids = np.linspace(0, len(frames) - 1, min(len(frames), max_frames), dtype=np.int64)
    sample = frames[ids, ::8, ::8]
    y = (
        0.299 * sample[..., 0].astype(np.float32)
        + 0.587 * sample[..., 1].astype(np.float32)
        + 0.114 * sample[..., 2].astype(np.float32)
    )
    valid = y[(y >= 18.0) & (y <= 248.0)]
    return valid if valid.size >= 100 else y.reshape(-1)


def _camera_stats(frames: np.ndarray) -> dict[str, float]:
    y = _sample_luma(frames)
    p10, p50, p90 = np.percentile(y, [10, 50, 90])
    return {"p10": float(p10), "p50": float(p50), "p90": float(p90)}


def _solve_curve(src50: float, src90: float, dst50: float, dst90: float) -> tuple[float, float]:
    if src90 - src50 < 8.0:
        return float(np.clip(dst50 / max(src50, 1.0), 0.72, 1.38)), 1.0
    x50 = np.clip(src50 / 255.0, 1e-3, 0.999)
    x90 = np.clip(src90 / 255.0, x50 + 1e-3, 0.999)
    y50 = np.clip(dst50 / 255.0, 1e-3, 0.999)
    y90 = np.clip(dst90 / 255.0, y50 + 1e-3, 0.999)
    gamma = float(np.clip(np.log(y50 / y90) / np.log(x50 / x90), 0.72, 1.35))
    gain = float(np.clip(y90 / (x90**gamma), 0.72, 1.38))
    return gain, gamma


def estimate_brightness_params(videos: dict[str, np.ndarray]) -> dict[str, dict[str, float]]:
    if not videos:
        raise ValueError("brightness augmentation requires at least one camera")
    stats = {key: _camera_stats(frames) for key, frames in videos.items()}
    target50 = float(np.clip(np.median([s["p50"] for s in stats.values()]), 105.0, 150.0))
    target90 = float(np.clip(np.median([s["p90"] for s in stats.values()]), 190.0, 225.0))
    target90 = max(target90, target50 + 35.0)
    params: dict[str, dict[str, float]] = {}
    for key, values in stats.items():
        gain, gamma = _solve_curve(values["p50"], values["p90"], target50, target90)
        params[key] = {
            **values,
            "target_p50": target50,
            "target_p90": target90,
            "gain": gain,
            "gamma": gamma,
        }
    return params


def apply_luma_curve(frames: np.ndarray, gain: float, gamma: float) -> np.ndarray:
    frames = np.asarray(frames)
    if frames.ndim != 4 or frames.shape[-1] != 3 or frames.dtype != np.uint8:
        raise ValueError("frames must have shape (T,H,W,3) and dtype uint8")
    x = np.arange(256, dtype=np.float32) / 255.0
    lut = np.clip(255.0 * gain * np.power(x, gamma), 0, 255).astype(np.uint8)
    out = np.empty_like(frames)
    for index, frame in enumerate(frames):
        ycc = cv2.cvtColor(frame, cv2.COLOR_RGB2YCrCb)
        ycc[..., 0] = cv2.LUT(ycc[..., 0], lut)
        out[index] = cv2.cvtColor(ycc, cv2.COLOR_YCrCb2RGB)
    return out


def result_metrics(frames: np.ndarray) -> dict[str, float]:
    y = _sample_luma(frames)
    return {
        "p10": float(np.percentile(y, 10)),
        "p50": float(np.percentile(y, 50)),
        "p90": float(np.percentile(y, 90)),
        "dark_clip_ratio": float((y <= 3).mean()),
        "bright_clip_ratio": float((y >= 252).mean()),
    }


def apply_brightness(
    videos: dict[str, np.ndarray],
    params: dict[str, dict[str, Any]] | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    resolved: dict[str, Any] = params or estimate_brightness_params(videos)
    out: dict[str, np.ndarray] = {}
    medians: list[float] = []
    warnings: list[str] = []
    for key, frames in videos.items():
        camera_params = resolved[key]
        out[key] = apply_luma_curve(frames, camera_params["gain"], camera_params["gamma"])
        camera_params["result"] = result_metrics(out[key])
        medians.append(camera_params["result"]["p50"])
        if (
            camera_params["result"]["dark_clip_ratio"] > 0.02
            or camera_params["result"]["bright_clip_ratio"] > 0.02
        ):
            warnings.append(f"{key}: excessive clipping after brightness adaptation")
    spread = float(max(medians) - min(medians))
    if spread > 32.0:
        # Head/wrist/left/right cameras can legitimately have very different
        # exposure. This is useful QA information, not an augmentation failure.
        warnings.append(f"cross-camera median brightness spread is high: {spread:.1f}")
    resolved["_qa"] = {
        "camera_p50_spread": spread,
        "status": "warning" if warnings else "ok",
        "warnings": warnings,
    }
    return out, resolved
