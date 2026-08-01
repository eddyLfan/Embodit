"""Transparent episode scoring from normalized QC findings."""

from __future__ import annotations

from typing import Any

from .intervals import union_duration


def score_episode(
    duration: float,
    findings: list[dict[str, Any]],
    detector_statuses: list[dict[str, Any]],
    config: dict[str, Any],
) -> dict[str, Any]:
    duration = max(0.0, float(duration))
    hard_invalid = any(bool(item.get("hard_invalid")) for item in findings)
    error_intervals = [
        (float(item["start_s"]), float(item["end_s"]))
        for item in findings
        if item.get("severity") in {"error", "fatal"}
        and item.get("start_s") is not None
        and item.get("end_s") is not None
    ]
    bad_duration = union_duration(error_intervals, duration) if duration else 0.0
    usable_ratio = 0.0 if hard_invalid else (
        100.0 if duration <= 0 else 100.0 * (duration - bad_duration) / duration
    )

    weights = config["scoring"]["severityWeights"]
    weighted_intervals: list[tuple[float, float, float]] = []
    episode_penalty = 0.0
    for item in findings:
        weight = float(weights.get(str(item.get("severity")), 0.0)) * float(
            item.get("confidence", 1.0)
        )
        start = item.get("start_s")
        end = item.get("end_s")
        if duration > 0 and start is not None and end is not None and float(end) > float(start):
            weighted_intervals.append((max(0.0, float(start)), min(duration, float(end)), weight))
        elif item.get("severity") == "fatal":
            episode_penalty = max(episode_penalty, 100.0)
        elif item.get("severity") == "error":
            episode_penalty = max(episode_penalty, 25.0)
        elif item.get("severity") == "warning":
            episode_penalty = max(episode_penalty, 5.0)

    time_penalty = 0.0
    if duration > 0 and weighted_intervals:
        points = sorted({0.0, duration, *[x for row in weighted_intervals for x in row[:2]]})
        for left, right in zip(points, points[1:]):
            if right <= left:
                continue
            active = [w for start, end, w in weighted_intervals if start < right and end > left]
            if active:
                time_penalty += (right - left) * max(active) / duration * 100.0
    quality_score = 0.0 if hard_invalid else max(
        0.0, min(100.0, 100.0 - time_penalty - episode_penalty)
    )

    applicable = [row for row in detector_statuses if row.get("applicable", True)]
    denominator = sum(float(row.get("coverageWeight", 1.0)) for row in applicable)
    numerator = sum(
        float(row.get("coverageWeight", 1.0))
        for row in applicable
        if row.get("status") == "completed"
    )
    coverage = 100.0 if denominator <= 0 else 100.0 * numerator / denominator

    thresholds = config["scoring"]
    has_fatal = any(item.get("severity") == "fatal" for item in findings)
    has_error = any(item.get("severity") == "error" for item in findings)
    if hard_invalid or has_fatal:
        decision = "quarantine"
    elif (
        quality_score >= float(thresholds["passQualityScore"])
        and usable_ratio >= float(thresholds["passUsableRatio"])
        and coverage >= float(thresholds["minimumCoverage"])
        and not has_error
    ):
        decision = "pass"
    else:
        decision = "review"
    return {
        "integrityStatus": "invalid" if hard_invalid else "valid",
        "usableRatio": round(usable_ratio, 3),
        "qualityScore": round(quality_score, 3),
        "coverage": round(coverage, 3),
        "autoDecision": decision,
    }
