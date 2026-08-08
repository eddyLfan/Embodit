"""Helpers for turning frame-level anomaly masks into reviewable time spans."""

from __future__ import annotations

import math
from collections.abc import Iterable

import numpy as np


def _finite(value: float, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"{name} 必须是有限数值") from error
    if not math.isfinite(number):
        raise ValueError(f"{name} 必须是有限数值")
    return number


def _nonnegative(value: float, name: str) -> float:
    number = _finite(value, name)
    if number < 0:
        raise ValueError(f"{name} 不能为负数")
    return number


def mask_to_intervals(
    mask: Iterable[bool],
    fps: float,
    *,
    minimum_seconds: float = 0.0,
    merge_gap_seconds: float = 0.0,
    context_seconds: float = 0.0,
    duration: float | None = None,
) -> list[tuple[float, float]]:
    rate = _finite(fps, "fps")
    if rate <= 0:
        raise ValueError("fps 必须大于 0")
    minimum = _nonnegative(minimum_seconds, "minimum_seconds")
    merge_gap = _nonnegative(merge_gap_seconds, "merge_gap_seconds")
    context = _nonnegative(context_seconds, "context_seconds")
    maximum = _nonnegative(duration, "duration") if duration is not None else None
    values = np.asarray(list(mask), dtype=bool)
    if values.size == 0 or not np.any(values):
        return []
    indices = np.flatnonzero(values)
    runs: list[tuple[int, int]] = []
    start = previous = int(indices[0])
    for raw in indices[1:]:
        current = int(raw)
        if current > previous + 1:
            runs.append((start, previous + 1))
            start = current
        previous = current
    runs.append((start, previous + 1))

    # A run must actually reach the configured minimum duration. Conversely,
    # a merge gap must not exceed its configured maximum duration.
    minimum_frames = max(1, int(math.ceil(minimum * rate)))
    runs = [(a, b) for a, b in runs if b - a >= minimum_frames]
    if not runs:
        return []
    merge_frames = int(math.floor(merge_gap * rate))
    merged = [runs[0]]
    for start, end in runs[1:]:
        old_start, old_end = merged[-1]
        if start - old_end <= merge_frames:
            merged[-1] = (old_start, end)
        else:
            merged.append((start, end))
    maximum = maximum if maximum is not None else values.size / rate
    intervals: list[tuple[float, float]] = []
    for start, end in merged:
        clipped_start = max(0.0, min(maximum, start / rate - context))
        clipped_end = max(0.0, min(maximum, end / rate + context))
        if clipped_end > clipped_start:
            intervals.append((clipped_start, clipped_end))
    return intervals


def union_duration(intervals: Iterable[tuple[float, float]], duration: float) -> float:
    maximum = _nonnegative(duration, "duration")
    normalized: list[tuple[float, float]] = []
    for raw_start, raw_end in intervals:
        start = max(0.0, min(maximum, _finite(raw_start, "interval start")))
        end = max(0.0, min(maximum, _finite(raw_end, "interval end")))
        if end > start:
            normalized.append((start, end))
    normalized.sort()
    if not normalized:
        return 0.0
    total = 0.0
    start, end = normalized[0]
    for current_start, current_end in normalized[1:]:
        if current_start <= end:
            end = max(end, current_end)
        else:
            total += end - start
            start, end = current_start, current_end
    return total + end - start
