"""Helpers for turning frame-level anomaly masks into reviewable time spans."""

from __future__ import annotations

from collections.abc import Iterable

import numpy as np


def mask_to_intervals(
    mask: Iterable[bool],
    fps: float,
    *,
    minimum_seconds: float = 0.0,
    merge_gap_seconds: float = 0.0,
    context_seconds: float = 0.0,
    duration: float | None = None,
) -> list[tuple[float, float]]:
    values = np.asarray(list(mask), dtype=bool)
    if values.size == 0 or not np.any(values):
        return []
    rate = max(float(fps), 1e-6)
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

    minimum_frames = max(1, int(round(max(0.0, minimum_seconds) * rate)))
    runs = [(a, b) for a, b in runs if b - a >= minimum_frames]
    if not runs:
        return []
    merge_frames = max(0, int(round(max(0.0, merge_gap_seconds) * rate)))
    merged = [runs[0]]
    for start, end in runs[1:]:
        old_start, old_end = merged[-1]
        if start - old_end <= merge_frames:
            merged[-1] = (old_start, end)
        else:
            merged.append((start, end))
    context = max(0.0, float(context_seconds))
    maximum = float(duration) if duration is not None else values.size / rate
    return [
        (max(0.0, start / rate - context), min(maximum, end / rate + context))
        for start, end in merged
    ]


def union_duration(intervals: Iterable[tuple[float, float]], duration: float) -> float:
    normalized = sorted(
        (max(0.0, float(a)), min(float(duration), float(b)))
        for a, b in intervals
        if b > a
    )
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
