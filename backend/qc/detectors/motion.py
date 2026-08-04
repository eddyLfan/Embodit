"""Generic, representation-aware-enough motion quality checks."""

from __future__ import annotations

import numpy as np

from qc.intervals import mask_to_intervals
from qc.schema import DetectorOutcome, Finding

from .base import EpisodeContext, as_matrix


def robust_zscore(values: np.ndarray) -> np.ndarray:
    if values.size == 0:
        return np.zeros_like(values, dtype=np.float64)
    median = np.median(values, axis=0, keepdims=True)
    mad = np.median(np.abs(values - median), axis=0, keepdims=True)
    scale = 1.4826 * mad
    # A single jump must not inflate its own denominator enough to evade detection.
    fallback = np.quantile(np.abs(values - median), 0.75, axis=0, keepdims=True)
    scale = np.where(scale > 1e-9, scale, np.maximum(fallback, 1e-6))
    return (values - median) / scale


class MotionDetector:
    detector_id = "motion"
    version = "3"
    coverage_weight = 1.5
    config_key = "motion"

    def run(self, context: EpisodeContext) -> DetectorOutcome:
        config = context.config["detectors"][self.config_key]
        findings: list[Finding] = []
        evaluated = 0
        arrays: list[tuple[str, np.ndarray, list[str]]] = []
        for key in ("action", "observation.state"):
            raw = context.signals.get(key)
            if raw is None:
                continue
            values = as_matrix(raw).astype(np.float64, copy=False)
            if len(values) < 5 or not np.all(np.isfinite(values)):
                continue
            names = context.feature_names(key, values.shape[1])
            patterns = [str(item).lower() for item in config.get("excludeNamePatterns") or []]
            keep = np.asarray(
                [not any(pattern in name.lower() for pattern in patterns) for name in names],
                dtype=bool,
            )
            if np.any(keep):
                arrays.append((key, values[:, keep], [name for name, on in zip(names, keep) if on]))
                evaluated += 1
        if not arrays:
            return DetectorOutcome(
                detector_id=self.detector_id,
                version=self.version,
                status="skipped",
                skip_reason="没有可用的有限连续 action/state 信号",
                coverage_weight=self.coverage_weight,
            )

        context_seconds = float(context.config["intervals"].get("contextSeconds", 0.1))
        preferred_jitter_keys = {
            str(item) for item in config.get("jitterSignalKeys") or ["action"]
        }
        jitter_arrays = [item for item in arrays if item[0] in preferred_jitter_keys]
        # Datasets without action still receive useful motion QC from the first
        # available continuous signal, but measured state is not mixed into
        # command-jitter detection when action is present.
        if not jitter_arrays:
            jitter_arrays = arrays[:1]

        for key, values, names in jitter_arrays:
            context.check_cancelled()
            velocity = np.diff(values, axis=0) * context.fps
            acceleration = np.diff(velocity, axis=0) * context.fps
            jerk = np.diff(acceleration, axis=0) * context.fps
            acceleration_z = np.abs(robust_zscore(acceleration))
            jerk_z = np.abs(robust_zscore(jerk))
            acceleration_limit = float(config["accelerationRobustZ"])
            jerk_limit = float(config["jerkRobustZ"])
            # Robust z-score alone is unstable on almost-constant or quantized
            # dimensions: a tiny denominator can turn numerical noise into a
            # huge score. Require a meaningful signal span and a derivative
            # large relative to that span as an absolute significance gate.
            low = np.quantile(values, 0.01, axis=0)
            high = np.quantile(values, 0.99, axis=0)
            signal_range = np.maximum(0.0, high - low)
            active_dimensions = signal_range >= float(config["minimumDimensionRange"])
            safe_range = np.maximum(signal_range, np.finfo(np.float64).eps)
            acceleration_ratio = np.abs(acceleration) / (
                safe_range[None, :] * context.fps * context.fps
            )
            jerk_ratio = np.abs(jerk) / (
                safe_range[None, :] * context.fps * context.fps * context.fps
            )
            acceleration_hits = (
                (acceleration_z > acceleration_limit)
                & (acceleration_ratio > float(config["minimumAccelerationRangeRatio"]))
                & active_dimensions[None, :]
            )
            jerk_hits = (
                (jerk_z > jerk_limit)
                & (jerk_ratio > float(config["minimumJerkRangeRatio"]))
                & active_dimensions[None, :]
            )

            # A genuine short jitter creates both high acceleration and high
            # jerk in the same dimension. Align derivative indices and allow a
            # small frame tolerance instead of unioning every dimension's
            # independent outlier into one dense episode-wide mask.
            aligned_acceleration = np.zeros((len(values), values.shape[1]), dtype=bool)
            aligned_jerk = np.zeros_like(aligned_acceleration)
            aligned_acceleration[2 : 2 + len(acceleration_hits)] = acceleration_hits
            aligned_jerk[3 : 3 + len(jerk_hits)] = jerk_hits
            nearby_jerk = aligned_jerk.copy()
            alignment = max(0, int(config.get("derivativeAlignmentFrames", 1)))
            for distance in range(1, alignment + 1):
                nearby_jerk[distance:] |= aligned_jerk[:-distance]
                nearby_jerk[:-distance] |= aligned_jerk[distance:]
            joint_hits = aligned_acceleration & nearby_jerk
            reversal_counts = np.zeros_like(joint_hits, dtype=np.int32)
            excess_travel_ratios = np.zeros_like(joint_hits, dtype=np.float64)
            window_frames = max(
                2,
                int(round(float(config["jitterWindowSeconds"]) * context.fps)),
            )
            radius = max(1, window_frames // 2)
            for frame_index, dimension in np.argwhere(joint_hits):
                left = max(0, int(frame_index) - radius)
                right = min(len(values), int(frame_index) + radius + 1)
                local = values[left:right, int(dimension)]
                if len(local) < 3:
                    continue
                steps = np.diff(local)
                local_span = float(np.ptp(local))
                significance = max(
                    np.finfo(np.float64).eps,
                    float(config["minimumDimensionRange"]) * 0.05,
                    local_span * 0.01,
                )
                directions = np.sign(steps[np.abs(steps) >= significance])
                reversals = int(np.count_nonzero(directions[1:] != directions[:-1]))
                travel = float(np.sum(np.abs(steps)))
                net = float(abs(local[-1] - local[0]))
                excess_ratio = max(0.0, travel - net) / max(
                    local_span,
                    np.finfo(np.float64).eps,
                )
                reversal_counts[frame_index, dimension] = reversals
                excess_travel_ratios[frame_index, dimension] = excess_ratio

            # Large derivatives also occur at a legitimate one-way setpoint
            # step. Jitter must contain a local direction reversal and excess
            # path length (backtracking) rather than just one abrupt move.
            joint_hits &= reversal_counts >= int(config["minimumJitterReversals"])
            joint_hits &= excess_travel_ratios >= float(
                config["minimumJitterExcessTravelRatio"]
            )
            padded = np.any(joint_hits, axis=1)
            intervals = mask_to_intervals(
                padded,
                context.fps,
                minimum_seconds=float(config["minimumAnomalySeconds"]),
                merge_gap_seconds=float(config["mergeGapSeconds"]),
                context_seconds=context_seconds,
                duration=context.duration,
            )
            for start, end in intervals:
                left = max(0, int(start * context.fps) - 3)
                right = min(len(values), int(np.ceil(end * context.fps)) + 3)
                local_accel = acceleration_z[max(0, left - 2) : max(0, right - 2)]
                local_jerk = jerk_z[max(0, left - 3) : max(0, right - 3)]
                local_accel_ratio = acceleration_ratio[max(0, left - 2) : max(0, right - 2)]
                local_jerk_ratio = jerk_ratio[max(0, left - 3) : max(0, right - 3)]
                dims = set(np.flatnonzero(np.any(joint_hits[left:right], axis=0)).tolist())
                selected_dims = sorted(dims)
                selected_accel = local_accel[:, selected_dims] if selected_dims else local_accel[:0]
                selected_jerk = local_jerk[:, selected_dims] if selected_dims else local_jerk[:0]
                selected_accel_ratio = (
                    local_accel_ratio[:, selected_dims] if selected_dims else local_accel_ratio[:0]
                )
                selected_jerk_ratio = (
                    local_jerk_ratio[:, selected_dims] if selected_dims else local_jerk_ratio[:0]
                )
                local_reversals = reversal_counts[left:right]
                local_excess_travel = excess_travel_ratios[left:right]
                selected_reversals = (
                    local_reversals[:, selected_dims] if selected_dims else local_reversals[:0]
                )
                selected_excess_travel = (
                    local_excess_travel[:, selected_dims]
                    if selected_dims
                    else local_excess_travel[:0]
                )
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="motion/jitter",
                        category="motion",
                        severity="error",
                        confidence=0.9,
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=start,
                        end_s=end,
                        signal_key=key,
                        dimension_indices=selected_dims,
                        metrics={
                            "accelerationRobustZMax": (
                                float(np.max(selected_accel)) if selected_accel.size else 0.0
                            ),
                            "jerkRobustZMax": (
                                float(np.max(selected_jerk)) if selected_jerk.size else 0.0
                            ),
                            "accelerationRangeRatioMax": (
                                float(np.max(selected_accel_ratio))
                                if selected_accel_ratio.size
                                else 0.0
                            ),
                            "jerkRangeRatioMax": (
                                float(np.max(selected_jerk_ratio))
                                if selected_jerk_ratio.size
                                else 0.0
                            ),
                            "directionReversalsMax": (
                                int(np.max(selected_reversals))
                                if selected_reversals.size
                                else 0
                            ),
                            "excessTravelRatioMax": (
                                float(np.max(selected_excess_travel))
                                if selected_excess_travel.size
                                else 0.0
                            ),
                            "dimensionNames": [
                                names[index] for index in selected_dims if index < len(names)
                            ],
                            "ignoredLowRangeDimensions": int(np.count_nonzero(~active_dimensions)),
                        },
                        threshold={
                            "accelerationRobustZ": acceleration_limit,
                            "jerkRobustZ": jerk_limit,
                            "minimumDimensionRange": float(config["minimumDimensionRange"]),
                            "minimumAccelerationRangeRatio": float(
                                config["minimumAccelerationRangeRatio"]
                            ),
                            "minimumJerkRangeRatio": float(
                                config["minimumJerkRangeRatio"]
                            ),
                            "jitterWindowSeconds": float(config["jitterWindowSeconds"]),
                            "minimumJitterReversals": int(
                                config["minimumJitterReversals"]
                            ),
                            "minimumJitterExcessTravelRatio": float(
                                config["minimumJitterExcessTravelRatio"]
                            ),
                        },
                        explanation=(
                            f"{key} 同一维度出现高加速度、高 jerk 和短窗反向回摆"
                        ),
                        suggested_decision="review",
                    )
                )

        aligned = [values for _key, values, _names in arrays]
        minimum_length = min(len(item) for item in aligned)
        if minimum_length >= 2:
            deltas = [np.max(np.abs(np.diff(item[:minimum_length], axis=0)), axis=1) for item in aligned]
            motion = np.maximum.reduce(deltas)
            stationary = motion <= float(config["stationaryDelta"])
            intervals = mask_to_intervals(
                stationary,
                context.fps,
                minimum_seconds=float(config["stationaryMinimumSeconds"]),
                merge_gap_seconds=0.2,
                context_seconds=0.0,
                duration=context.duration,
            )
            for start, end in intervals:
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="motion/stationary",
                        category="motion",
                        severity="warning",
                        confidence=0.9,
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=start,
                        end_s=end,
                        metrics={"durationSeconds": end - start},
                        threshold={
                            "delta": float(config["stationaryDelta"]),
                            "minimumSeconds": float(config["stationaryMinimumSeconds"]),
                        },
                        explanation="连续长时间几乎没有机器人运动",
                        suggested_decision="review",
                    )
                )
            maximum_range = max(float(np.max(np.ptp(item, axis=0))) for item in aligned)
            if maximum_range < float(config["nearZeroMotionRange"]):
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="motion/near_zero_episode",
                        category="motion",
                        severity="error",
                        confidence=0.95,
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=0.0,
                        end_s=context.duration,
                        metrics={"motionRange": maximum_range},
                        threshold={"minimumRange": float(config["nearZeroMotionRange"])},
                        explanation="整条 episode 几乎没有有效运动",
                        suggested_decision="review",
                    )
                )
        return DetectorOutcome(
            detector_id=self.detector_id,
            version=self.version,
            findings=findings,
            coverage_weight=self.coverage_weight,
        )
