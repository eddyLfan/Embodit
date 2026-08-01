"""Hard validity checks for action/state arrays."""

from __future__ import annotations

import numpy as np

from qc.schema import DetectorOutcome, Finding

from .base import EpisodeContext, as_matrix


class SignalIntegrityDetector:
    detector_id = "signal_integrity"
    version = "1"
    coverage_weight = 3.0
    config_key = "signalIntegrity"

    def run(self, context: EpisodeContext) -> DetectorOutcome:
        requirements = context.config["requirements"]
        findings: list[Finding] = []
        action = context.signals.get("action")
        state = context.signals.get("observation.state")
        if action is None and requirements.get("action", True):
            findings.append(self._hard(context, "integrity/missing_action", "缺少必需 action 信号"))
        if state is None and requirements.get("state", False):
            findings.append(self._hard(context, "integrity/missing_state", "缺少必需 state 信号"))

        arrays: dict[str, np.ndarray] = {}
        for key, raw in (("action", action), ("observation.state", state)):
            if raw is None:
                continue
            try:
                values = as_matrix(raw)
            except Exception as error:  # noqa: BLE001
                findings.append(
                    self._hard(context, "integrity/invalid_signal_shape", f"{key} 形状无法解析：{error}", key)
                )
                continue
            arrays[key] = values
            if values.shape[0] == 0 or values.shape[1] == 0:
                findings.append(self._hard(context, "integrity/empty_signal", f"{key} 为空", key))
                continue
            try:
                finite = np.isfinite(values.astype(np.float64, copy=False))
            except (TypeError, ValueError):
                findings.append(
                    self._hard(context, "integrity/non_numeric_signal", f"{key} 不是数值信号", key)
                )
                continue
            if not np.all(finite):
                row_mask = ~np.all(finite, axis=1)
                bad_rows = np.flatnonzero(row_mask)
                start = float(bad_rows[0] / context.fps)
                end = float((bad_rows[-1] + 1) / context.fps)
                dims = np.flatnonzero(~np.all(finite, axis=0)).astype(int).tolist()
                findings.append(
                    Finding(
                        episode_index=context.episode.episode_index,
                        issue_code="integrity/non_finite_signal",
                        category="integrity",
                        severity="fatal",
                        detector_id=self.detector_id,
                        detector_version=self.version,
                        start_s=start,
                        end_s=min(context.duration, end),
                        signal_key=key,
                        dimension_indices=dims,
                        metrics={"nonFiniteValues": int(np.size(finite) - np.count_nonzero(finite))},
                        explanation=f"{key} 包含 NaN 或 Inf",
                        suggested_decision="quarantine",
                        hard_invalid=True,
                    )
                )

            expected = int(context.episode.length or 0)
            tolerance = max(
                int(requirements.get("lengthToleranceFrames", 2)),
                int(round(expected * float(requirements.get("lengthToleranceRatio", 0.1)))),
            )
            if expected > 0 and abs(values.shape[0] - expected) > tolerance:
                findings.append(
                    self._hard(
                        context,
                        "integrity/signal_length_mismatch",
                        f"{key} 长度 {values.shape[0]} 与 episode 帧数 {expected} 严重不一致",
                        key,
                        {"actual": int(values.shape[0]), "expected": expected, "tolerance": tolerance},
                    )
                )
        if len(arrays) >= 2:
            lengths = {key: int(value.shape[0]) for key, value in arrays.items()}
            if max(lengths.values()) - min(lengths.values()) > max(
                int(requirements.get("lengthToleranceFrames", 2)),
                int(round(max(lengths.values()) * float(requirements.get("lengthToleranceRatio", 0.1)))),
            ):
                findings.append(
                    self._hard(
                        context,
                        "integrity/action_state_length_mismatch",
                        "action 与 state 长度严重不一致",
                        metrics=lengths,
                    )
                )
        return DetectorOutcome(
            detector_id=self.detector_id,
            version=self.version,
            findings=findings,
            coverage_weight=self.coverage_weight,
        )

    def _hard(
        self,
        context: EpisodeContext,
        issue: str,
        explanation: str,
        signal_key: str | None = None,
        metrics: dict | None = None,
    ) -> Finding:
        return Finding(
            episode_index=context.episode.episode_index,
            issue_code=issue,
            category="integrity",
            severity="fatal",
            detector_id=self.detector_id,
            detector_version=self.version,
            signal_key=signal_key,
            metrics=metrics or {},
            explanation=explanation,
            suggested_decision="quarantine",
            hard_invalid=True,
        )
