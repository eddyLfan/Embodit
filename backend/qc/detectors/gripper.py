"""Gripper chatter and conservative re-grasp candidate detection."""

from __future__ import annotations

import numpy as np

from qc.schema import DetectorOutcome, Finding

from .base import EpisodeContext, as_matrix


class GripperDetector:
    detector_id = "gripper"
    version = "1"
    coverage_weight = 1.0
    config_key = "gripper"

    def run(self, context: EpisodeContext) -> DetectorOutcome:
        config = context.config["detectors"][self.config_key]
        patterns = [str(item).lower() for item in config.get("namePatterns") or []]
        selected: list[tuple[str, np.ndarray, list[str]]] = []
        for key in ("action", "observation.state"):
            raw = context.signals.get(key)
            if raw is None:
                continue
            values = as_matrix(raw).astype(np.float64, copy=False)
            names = context.feature_names(key, values.shape[1])
            mask = np.asarray(
                [any(pattern in name.lower() for pattern in patterns) for name in names],
                dtype=bool,
            )
            if np.any(mask) and len(values) >= 2 and np.all(np.isfinite(values[:, mask])):
                selected.append((key, values[:, mask], [name for name, on in zip(names, mask) if on]))
        if not selected:
            return DetectorOutcome(
                detector_id=self.detector_id,
                version=self.version,
                status="skipped",
                skip_reason="未识别到 gripper/finger/jaw 维度；可在配置中提供字段名称",
                coverage_weight=self.coverage_weight,
            )
        findings: list[Finding] = []
        threshold = float(config["transitionThreshold"])
        for key, values, names in selected:
            changes = np.abs(np.diff(values, axis=0))
            transition_rows = np.flatnonzero(np.any(changes >= threshold, axis=1)) + 1
            duration = max(context.duration, len(values) / context.fps, 1e-6)
            rate = float(len(transition_rows) / duration)
            if rate > float(config["maxTransitionsPerSecond"]):
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="manipulation/gripper_chatter",
                        category="manipulation",
                        severity="error",
                        confidence=0.9,
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=float(transition_rows[0] / context.fps),
                        end_s=min(context.duration, float((transition_rows[-1] + 1) / context.fps)),
                        signal_key=key,
                        metrics={
                            "transitionCount": int(len(transition_rows)),
                            "transitionsPerSecond": rate,
                            "dimensionNames": names,
                        },
                        threshold={
                            "delta": threshold,
                            "maxTransitionsPerSecond": float(config["maxTransitionsPerSecond"]),
                        },
                        explanation="夹爪在短时间内高频开合",
                        suggested_decision="review",
                    )
                )
            if config.get("allowMultipleGrasps", False) or len(transition_rows) < 3:
                continue
            window_frames = max(1, int(float(config["regraspWindowSeconds"]) * context.fps))
            for left in range(len(transition_rows) - 2):
                triple = transition_rows[left : left + 3]
                if int(triple[-1] - triple[0]) > window_frames:
                    continue
                # Require alternating direction on at least one gripper dimension.
                first = values[triple[0]] - values[max(0, triple[0] - 1)]
                second = values[triple[1]] - values[max(0, triple[1] - 1)]
                third = values[triple[2]] - values[max(0, triple[2] - 1)]
                alternating = np.any((np.sign(first) == np.sign(third)) & (np.sign(first) != np.sign(second)))
                if not alternating:
                    continue
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="manipulation/regrasp_candidate",
                        category="manipulation",
                        severity="warning",
                        confidence=0.65,
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=max(0.0, float((triple[0] - 1) / context.fps)),
                        end_s=min(context.duration, float((triple[-1] + 1) / context.fps)),
                        signal_key=key,
                        metrics={"transitionFrames": triple.astype(int).tolist(), "dimensionNames": names},
                        threshold={"windowSeconds": float(config["regraspWindowSeconds"])},
                        explanation="检测到闭合、重新张开并再次闭合的二次抓取候选",
                        suggested_decision="review",
                    )
                )
                break
        return DetectorOutcome(
            detector_id=self.detector_id,
            version=self.version,
            findings=findings,
            coverage_weight=self.coverage_weight,
        )
