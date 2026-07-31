"""Built-in mask-based recoloring and solid-background effects."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import cv2
import numpy as np


def feather_mask(mask: np.ndarray, radius: int = 3) -> np.ndarray:
    binary = mask.astype(np.uint8)
    if radius <= 0:
        return binary.astype(np.float32)
    inside = cv2.distanceTransform(binary, cv2.DIST_L2, 3)
    outside = cv2.distanceTransform(1 - binary, cv2.DIST_L2, 3)
    return np.clip(0.5 + (inside - outside) / (2.0 * radius), 0.0, 1.0).astype(np.float32)


def _lightness_range(frames: np.ndarray, masks: np.ndarray) -> tuple[float, float]:
    values: list[np.ndarray] = []
    ids = np.linspace(0, len(frames) - 1, min(len(frames), 40), dtype=np.int64)
    for index in ids:
        lab = cv2.cvtColor(frames[index], cv2.COLOR_RGB2LAB)
        if masks[index].any():
            values.append(lab[..., 0][masks[index]][::8])
    if not values:
        return 0.0, 255.0
    sample = np.concatenate(values).astype(np.float32)
    low, high = np.percentile(sample, [3, 97])
    return float(low), float(max(high, low + 12.0))


def recolor_frames(
    frames: np.ndarray,
    masks: np.ndarray,
    target_rgb: Sequence[int],
) -> tuple[np.ndarray, dict[str, Any]]:
    if frames.shape[:3] != masks.shape:
        raise ValueError("mask shape must match video frames")
    out = frames.copy()
    target = np.asarray(target_rgb, dtype=np.uint8).reshape(1, 1, 3)
    target_lab = cv2.cvtColor(target, cv2.COLOR_RGB2LAB)[0, 0].astype(np.float32)
    low, high = _lightness_range(frames, masks)
    changed_inside: list[float] = []
    changed_outside: list[float] = []
    for index, frame in enumerate(frames):
        mask = masks[index].astype(bool)
        if not mask.any():
            continue
        lab = cv2.cvtColor(frame, cv2.COLOR_RGB2LAB).astype(np.float32)
        lightness = lab[..., 0]
        detail = lightness - cv2.GaussianBlur(lightness, (0, 0), 2.0)
        normalized = np.clip((lightness - low) / (high - low), 0.0, 1.0)
        mapped = np.clip(target_lab[0] * (0.56 + 0.38 * normalized) + 1.35 * detail, 18.0, 238.0)
        colored = lab.copy()
        colored[..., 0] = mapped
        colored[..., 1] = 0.92 * target_lab[1] + 0.08 * lab[..., 1]
        colored[..., 2] = 0.92 * target_lab[2] + 0.08 * lab[..., 2]
        colored_rgb = cv2.cvtColor(
            np.clip(colored, 0, 255).astype(np.uint8), cv2.COLOR_LAB2RGB
        ).astype(np.float32)
        alpha = feather_mask(mask, radius=3)
        bright_cut = float(np.percentile(lightness[mask], 96))
        alpha[mask & (lightness >= max(bright_cut, low + 0.72 * (high - low)))] *= 0.28
        alpha_2d = alpha
        alpha = alpha[..., None]
        out[index] = np.clip(frame * (1.0 - alpha) + colored_rgb * alpha, 0, 255).astype(np.uint8)
        diff = np.abs(out[index].astype(np.int16) - frame.astype(np.int16)).mean(axis=2)
        changed_inside.append(float(diff[mask].mean()))
        core_outside = alpha_2d <= 0.001
        if core_outside.any():
            changed_outside.append(float(diff[core_outside].mean()))
    qa = {
        "episode_lightness_p03": low,
        "episode_lightness_p97": high,
        "inside_mean_abs_change": float(np.mean(changed_inside)) if changed_inside else 0.0,
        "outside_mean_abs_change": float(np.mean(changed_outside)) if changed_outside else 0.0,
    }
    if qa["inside_mean_abs_change"] < 8.0:
        raise RuntimeError("object recolor is too weak")
    if qa["outside_mean_abs_change"] > 1.0:
        raise RuntimeError(f"object recolor leaked outside mask: {qa['outside_mean_abs_change']:.3f}")
    return out, qa


def replace_background_frames(
    frames: np.ndarray,
    keep_masks: np.ndarray,
    background_rgb: Sequence[int],
) -> tuple[np.ndarray, dict[str, float]]:
    if frames.shape[:3] != keep_masks.shape:
        raise ValueError("mask shape must match video frames")
    out = frames.copy()
    color = np.asarray(background_rgb, dtype=np.float32).reshape(1, 1, 3)
    keep_changes: list[float] = []
    outside_errors: list[float] = []
    for index, frame in enumerate(frames):
        alpha = feather_mask(keep_masks[index], radius=3)[..., None]
        out[index] = np.clip(frame * alpha + color * (1.0 - alpha), 0, 255).astype(np.uint8)
        diff = np.abs(out[index].astype(np.int16) - frame.astype(np.int16)).mean(axis=2)
        core_keep = alpha[..., 0] >= 0.999
        if core_keep.any():
            keep_changes.append(float(diff[core_keep].mean()))
        core_outside = alpha[..., 0] <= 0.001
        if core_outside.any():
            outside_errors.append(
                float(np.abs(out[index][core_outside].astype(np.int16) - color.reshape(3)).mean())
            )
    qa = {
        "keep_mean_abs_change": float(np.mean(keep_changes)) if keep_changes else 0.0,
        "outside_color_mean_abs_error": float(np.mean(outside_errors)) if outside_errors else 0.0,
    }
    if qa["keep_mean_abs_change"] > 1.5:
        raise RuntimeError(f"background replacement altered keep region: {qa['keep_mean_abs_change']:.3f}")
    return out, qa
