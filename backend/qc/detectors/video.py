"""Full-decode video integrity plus visual quality and static-camera shake."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re

import cv2
import numpy as np

from datasets.frames import episode_frame_source
from qc.intervals import mask_to_intervals, union_duration
from qc.schema import DetectorOutcome, Finding

from .base import EpisodeContext, as_matrix


class VideoDetector:
    detector_id = "video"
    version = "3"
    coverage_weight = 3.0
    config_key = "videoIntegrity"

    def run(self, context: EpisodeContext) -> DetectorOutcome:
        requirements = context.config["requirements"]
        cameras = context.episode.cameras
        findings: list[Finding] = []
        minimum = int(requirements.get("minimumCameras", 1))
        if len(cameras) < minimum:
            findings.append(
                self._hard(
                    context,
                    "integrity/missing_required_camera",
                    f"只有 {len(cameras)} 路相机，少于要求的 {minimum} 路",
                    metrics={"actual": len(cameras), "minimum": minimum},
                )
            )
        explicit_required = {str(item) for item in requirements.get("requiredCameraKeys") or []}
        patterns = [re.compile(str(item), re.IGNORECASE) for item in requirements.get("requiredCameraPatterns") or []]
        for key in sorted(explicit_required):
            if key not in cameras:
                findings.append(
                    self._hard(
                        context,
                        "integrity/missing_required_camera",
                        f"缺少必需相机 {key}",
                        camera_key=key,
                    )
                )
        if patterns and not any(any(pattern.search(key) for pattern in patterns) for key in cameras):
            findings.append(
                self._hard(
                    context,
                    "integrity/missing_required_camera_pattern",
                    "没有相机匹配 requiredCameraPatterns",
                    metrics={"patterns": [item.pattern for item in patterns]},
                )
            )
        if not cameras:
            return DetectorOutcome(
                detector_id=self.detector_id,
                version=self.version,
                findings=findings,
                coverage_weight=self.coverage_weight,
            )

        def scan_camera(camera_key: str) -> list[Finding]:
            context.check_cancelled()
            source = episode_frame_source(context.adapter, context.view, context.episode, camera_key)
            if source is None:
                return [
                    self._hard(
                        context,
                        "integrity/video_source_unavailable",
                        f"相机 {camera_key} 无法建立帧源",
                        camera_key=camera_key,
                    )
                ]
            return self._scan_camera(context, camera_key, source)

        camera_keys = sorted(cameras)
        runtime = context.config.get("runtime", {})
        requested_workers = int(runtime.get("activeCameraWorkers", runtime.get("cameraWorkers", 1)))
        workers = min(len(camera_keys), max(1, requested_workers))
        if workers == 1:
            camera_results = map(scan_camera, camera_keys)
            for rows in camera_results:
                findings.extend(rows)
        else:
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="qc-camera") as executor:
                for rows in executor.map(scan_camera, camera_keys):
                    findings.extend(rows)
        return DetectorOutcome(
            detector_id=self.detector_id,
            version=self.version,
            findings=findings,
            coverage_weight=self.coverage_weight,
        )

    def _scan_camera(self, context: EpisodeContext, camera_key: str, source) -> list[Finding]:
        integrity = context.config["detectors"]["videoIntegrity"]
        visual = context.config["detectors"]["visualQuality"]
        shake = context.config["detectors"]["cameraShake"]
        visual_enabled = bool(visual.get("enabled", True))
        shake_enabled = bool(shake.get("enabled", True)) and self._is_static_camera(camera_key, shake)
        decoded = 0
        means: list[float] = []
        blur_values: list[float] = []
        freeze_deltas: list[float] = []
        freeze_changed_ratios: list[float] = []
        sampled_grays: list[np.ndarray] = []
        previous_freeze: np.ndarray | None = None
        expected = int(context.episode.length or 0)
        freeze_target_fps = max(float(integrity.get("freezeSampleFps", 5.0)), 0.1)
        visual_target_fps = max(float(visual.get("sampleFps", 4.0)), 0.1)
        shake_target_fps = max(float(shake.get("sampleFps", 2.0)), 0.1)
        target_fps = max(
            freeze_target_fps,
            visual_target_fps if visual_enabled else 0.0,
            shake_target_fps if shake_enabled else 0.0,
        )
        analysis_stride = max(1, int(round(context.fps / min(context.fps, target_fps))))
        analysis_fps = context.fps / analysis_stride
        visual_step = max(1, int(round(analysis_fps / visual_target_fps)))
        shake_step = max(1, int(round(analysis_fps / shake_target_fps)))
        visual_sample_fps = analysis_fps / visual_step
        shake_sample_fps = analysis_fps / shake_step

        def resize_gray(gray: np.ndarray, width: int) -> np.ndarray:
            if width > 0 and gray.shape[1] > width:
                height = max(1, round(gray.shape[0] * width / gray.shape[1]))
                return cv2.resize(gray, (width, height), interpolation=cv2.INTER_AREA)
            return gray

        decode_error: str | None = None
        sample_index = 0
        try:
            for frame_index, frame in source.iter_rgb_samples(analysis_stride):
                decoded = frame_index + 1
                if decoded % 32 == 0:
                    context.check_cancelled()
                if frame is None:
                    continue
                rgb = np.asarray(frame)
                if rgb.ndim != 3 or rgb.shape[2] < 3:
                    raise ValueError(f"非法 frame shape: {rgb.shape}")
                gray = cv2.cvtColor(rgb[..., :3], cv2.COLOR_RGB2GRAY)
                freeze_gray = resize_gray(gray, int(integrity.get("resizeWidth", 160)))
                if previous_freeze is None:
                    freeze_deltas.append(float("inf"))
                    freeze_changed_ratios.append(1.0)
                else:
                    difference = cv2.absdiff(freeze_gray, previous_freeze)
                    freeze_deltas.append(float(np.mean(difference)))
                    freeze_changed_ratios.append(
                        float(
                            np.mean(
                                difference
                                > float(integrity.get("freezeChangedPixelThreshold", 0.0))
                            )
                        )
                    )
                previous_freeze = freeze_gray

                if visual_enabled and sample_index % visual_step == 0:
                    visual_gray = resize_gray(gray, int(visual.get("resizeWidth", 320)))
                    means.append(float(np.mean(visual_gray)))
                    blur_values.append(float(cv2.Laplacian(visual_gray, cv2.CV_64F).var()))
                if shake_enabled and sample_index % shake_step == 0:
                    sampled_grays.append(resize_gray(gray, int(shake.get("resizeWidth", 320))))
                sample_index += 1
        except Exception as error:  # noqa: BLE001
            decode_error = f"{type(error).__name__}: {error}"

        findings: list[Finding] = []
        if decode_error is not None:
            findings.append(
                self._hard(
                    context,
                    "integrity/video_decode_error",
                    f"相机 {camera_key} 解码失败：{decode_error}",
                    camera_key=camera_key,
                    metrics={"decodedFrames": decoded, "error": decode_error},
                )
            )
        if decoded == 0:
            findings.append(
                self._hard(
                    context,
                    "integrity/empty_video",
                    f"相机 {camera_key} 没有可解码帧",
                    camera_key=camera_key,
                )
            )
            return findings
        if expected > 0:
            ratio = decoded / expected
            if ratio < float(integrity["minimumDecodedRatio"]):
                findings.append(
                    self._hard(
                        context,
                        "integrity/video_frame_count_mismatch",
                        f"相机 {camera_key} 只解码 {decoded}/{expected} 帧",
                        camera_key=camera_key,
                        metrics={"decodedFrames": decoded, "expectedFrames": expected, "ratio": ratio},
                    )
                )

        duration = context.duration or decoded / context.fps
        context_seconds = float(context.config["intervals"].get("contextSeconds", 0.1))
        delta_values = np.asarray(freeze_deltas[1:], dtype=np.float64)
        changed_ratios = np.asarray(freeze_changed_ratios[1:], dtype=np.float64)
        freeze_mask = (delta_values <= float(integrity["freezePixelDelta"])) & (
            changed_ratios <= float(integrity["freezeMaximumChangedRatio"])
        )
        # A delta at sampled frame i represents the span (i-1, i]. Dropping
        # the leading sentinel keeps the interval start aligned to that span.
        candidate_intervals = mask_to_intervals(
            freeze_mask,
            analysis_fps,
            minimum_seconds=float(integrity["freezeMinimumSeconds"]),
            merge_gap_seconds=0.1,
            context_seconds=0.0,
            duration=duration,
        )
        accepted_freezes: list[dict] = []
        background = delta_values[~freeze_mask]
        background = background[np.isfinite(background)]
        background_delta = float(np.median(background)) if background.size else 0.0
        minimum_motion = float(integrity["freezeMinimumMotionRangeRatio"])
        for start, end in candidate_intervals:
            motion_ratio, motion_available = self._signal_motion_evidence(context, start, end)
            # When proprioception is present, an exactly still scene while the
            # robot is also still is observationally ambiguous and must not be
            # called a camera freeze. Motion during unchanged video provides
            # the cross-modal evidence required for a precise finding.
            if motion_available and motion_ratio < minimum_motion:
                continue
            left = max(0, int(np.floor(start * analysis_fps)))
            right = min(len(delta_values), int(np.ceil(end * analysis_fps)))
            local_deltas = delta_values[left:right]
            local_changed = changed_ratios[left:right]
            after_index = min(len(delta_values) - 1, right) if len(delta_values) else 0
            post_delta = float(delta_values[after_index]) if len(delta_values) else 0.0
            accepted_freezes.append(
                {
                    "start": start,
                    "end": end,
                    "motionRatio": motion_ratio,
                    "motionAvailable": motion_available,
                    "meanDeltaMax": float(np.max(local_deltas)) if local_deltas.size else 0.0,
                    "meanDeltaMean": float(np.mean(local_deltas)) if local_deltas.size else 0.0,
                    "changedRatioMax": (
                        float(np.max(local_changed)) if local_changed.size else 0.0
                    ),
                    "postDelta": post_delta,
                }
            )
        frozen_seconds = union_duration(
            [(row["start"], row["end"]) for row in accepted_freezes],
            duration,
        )
        hard_ratio = float(integrity["freezeHardRatio"])
        for row in accepted_freezes:
            start = max(0.0, float(row["start"]) - context_seconds)
            end = min(duration, float(row["end"]) + context_seconds)
            hard = (
                duration > 0
                and frozen_seconds / duration >= hard_ratio
                and bool(row["motionAvailable"])
            )
            confidence = 0.95 if row["motionAvailable"] else 0.7
            findings.append(
                Finding(
                    episode_index=context.episode.episode_index,
                    issue_code="integrity/video_frozen" if hard else "visual/frozen",
                    category="integrity" if hard else "visual",
                    severity="fatal" if hard else "error",
                    confidence=confidence,
                    detector_id=self.detector_id,
                    detector_version=self.version,
                    start_s=start,
                    end_s=end,
                    camera_key=camera_key,
                    metrics={
                        "meanPixelDeltaMax": row["meanDeltaMax"],
                        "meanPixelDeltaMean": row["meanDeltaMean"],
                        "changedPixelRatioMax": row["changedRatioMax"],
                        "backgroundPixelDeltaMedian": background_delta,
                        "postFreezePixelDelta": row["postDelta"],
                        "signalMotionRangeRatio": row["motionRatio"],
                        "signalMotionEvidenceAvailable": row["motionAvailable"],
                        "frozenSeconds": frozen_seconds,
                        "frozenRatio": frozen_seconds / duration if duration else 0.0,
                    },
                    threshold={
                        "meanPixelDelta": float(integrity["freezePixelDelta"]),
                        "changedPixel": float(integrity["freezeChangedPixelThreshold"]),
                        "maximumChangedPixelRatio": float(
                            integrity["freezeMaximumChangedRatio"]
                        ),
                        "minimumSignalMotionRangeRatio": minimum_motion,
                    },
                    explanation=(
                        f"相机 {camera_key} 画面近重复"
                        + (
                            "，且机器人信号仍在运动"
                            if row["motionAvailable"]
                            else "（无运动信号可交叉验证）"
                        )
                    ),
                    suggested_decision="quarantine" if hard else "review",
                    hard_invalid=hard,
                )
            )

        if visual_enabled:
            issue_specs = (
                (
                    "visual/dark",
                    np.asarray(means) < float(visual["darkMeanThreshold"]),
                    {"meanThreshold": float(visual["darkMeanThreshold"])},
                    "画面持续过暗",
                ),
                (
                    "visual/overexposed",
                    np.asarray(means) > float(visual["brightMeanThreshold"]),
                    {"meanThreshold": float(visual["brightMeanThreshold"])},
                    "画面持续过曝",
                ),
                (
                    "visual/blur",
                    np.asarray(blur_values) < float(visual["blurVarianceThreshold"]),
                    {"laplacianVarianceThreshold": float(visual["blurVarianceThreshold"])},
                    "画面持续模糊",
                ),
            )
            for issue, mask, threshold, explanation in issue_specs:
                intervals = mask_to_intervals(
                    mask,
                    visual_sample_fps,
                    minimum_seconds=float(visual["minimumIssueSeconds"]),
                    merge_gap_seconds=float(visual["mergeGapSeconds"]),
                    context_seconds=context_seconds,
                    duration=duration,
                )
                for start, end in intervals:
                    findings.append(
                        Finding(
                            episode_index=context.episode.episode_index,
                            issue_code=issue,
                            category="visual",
                            severity="warning",
                            confidence=0.85,
                            detector_id=self.detector_id,
                            detector_version=self.version,
                            start_s=start,
                            end_s=end,
                            camera_key=camera_key,
                            threshold=threshold,
                            explanation=f"{camera_key}：{explanation}",
                            suggested_decision="review",
                        )
                    )
        if shake_enabled:
            findings.extend(
                self._camera_shake(
                    context,
                    camera_key,
                    sampled_grays,
                    duration,
                    shake,
                    shake_sample_fps,
                )
            )
        return findings

    def _is_static_camera(self, key: str, config: dict) -> bool:
        configured = {str(item) for item in config.get("staticCameraKeys") or []}
        if configured:
            return key in configured
        lowered = key.lower()
        return any(
            token in lowered for token in ("base", "head", "main", "front", "overhead")
        ) and not any(token in lowered for token in ("wrist", "hand", "eef"))

    def _signal_motion_evidence(
        self,
        context: EpisodeContext,
        start: float,
        end: float,
    ) -> tuple[float, bool]:
        """Return normalized in-interval signal range and whether it was observable."""

        evidence = 0.0
        available = False
        for key in ("action", "observation.state"):
            raw = context.signals.get(key)
            if raw is None:
                continue
            values = as_matrix(raw).astype(np.float64, copy=False)
            if len(values) < 2 or not np.all(np.isfinite(values)):
                continue
            available = True
            left = max(0, min(len(values) - 1, int(np.floor(start * context.fps))))
            right = max(
                left + 1,
                min(len(values), int(np.ceil(end * context.fps)) + 1),
            )
            global_low = np.quantile(values, 0.01, axis=0)
            global_high = np.quantile(values, 0.99, axis=0)
            global_range = global_high - global_low
            active = global_range > np.finfo(np.float64).eps
            if not np.any(active):
                continue
            local_range = np.ptp(values[left:right], axis=0)
            evidence = max(
                evidence,
                float(np.max(local_range[active] / global_range[active])),
            )
        return evidence, available

    def _camera_shake(
        self,
        context: EpisodeContext,
        camera_key: str,
        frames: list[np.ndarray],
        duration: float,
        config: dict,
        sample_fps: float,
    ) -> list[Finding]:
        if len(frames) < 3:
            return []
        motions = np.full((len(frames) - 1, 4), np.nan, dtype=np.float64)
        inlier_ratios = np.zeros(len(frames) - 1, dtype=np.float64)
        spatial_coverages = np.zeros(len(frames) - 1, dtype=np.float64)
        tracked_counts = np.zeros(len(frames) - 1, dtype=np.int32)
        for index, (previous, current) in enumerate(zip(frames, frames[1:])):
            estimate = self._estimate_global_motion(previous, current, config)
            if estimate is None:
                continue
            motion, metrics = estimate
            motions[index] = motion
            inlier_ratios[index] = metrics["inlierRatio"]
            spatial_coverages[index] = metrics["spatialCoverage"]
            tracked_counts[index] = metrics["trackedPoints"]
        reliable = np.all(np.isfinite(motions), axis=1)
        changes = np.zeros(max(0, len(motions) - 1), dtype=np.float64)
        reliable_changes = reliable[1:] & reliable[:-1]
        if changes.size:
            raw_changes = np.linalg.norm(np.diff(motions, axis=0), axis=1)
            changes[reliable_changes] = raw_changes[reliable_changes]
        mask = reliable_changes & (changes > float(config["jitterThreshold"]))
        sample_fps = max(float(sample_fps), 1e-6)
        intervals = mask_to_intervals(
            np.r_[False, False, mask],
            sample_fps,
            minimum_seconds=int(config["minimumShakeChanges"]) / sample_fps,
            merge_gap_seconds=float(config["mergeGapSeconds"]),
            context_seconds=float(context.config["intervals"].get("contextSeconds", 0.1)),
            duration=duration,
        )
        return [
            Finding(
                episode_index=context.episode.episode_index,
                issue_code="visual/camera_shake",
                category="visual",
                severity="error",
                confidence=0.8,
                detector_id=self.detector_id,
                detector_version=self.version,
                start_s=start,
                end_s=end,
                camera_key=camera_key,
                metrics={
                    "globalMotionChangeMax": float(np.max(changes)) if changes.size else 0.0,
                    "inlierRatioMedian": (
                        float(np.median(inlier_ratios[reliable])) if np.any(reliable) else 0.0
                    ),
                    "spatialCoverageMedian": (
                        float(np.median(spatial_coverages[reliable]))
                        if np.any(reliable)
                        else 0.0
                    ),
                    "trackedPointsMin": (
                        int(np.min(tracked_counts[reliable])) if np.any(reliable) else 0
                    ),
                },
                threshold={
                    "jitter": float(config["jitterThreshold"]),
                    "minimumInlierRatio": float(config["minimumInlierRatio"]),
                    "minimumSpatialCoverage": float(config["minimumSpatialCoverage"]),
                    "minimumShakeChanges": int(config["minimumShakeChanges"]),
                },
                explanation=(
                    f"静态相机 {camera_key} 出现经 RANSAC 验证的高频全局抖动"
                ),
                suggested_decision="review",
            )
            for start, end in intervals
        ]

    def _estimate_global_motion(
        self,
        previous: np.ndarray,
        current: np.ndarray,
        config: dict,
    ) -> tuple[np.ndarray, dict] | None:
        """Estimate robust global similarity motion from spatially spread features."""

        points = cv2.goodFeaturesToTrack(
            previous,
            maxCorners=300,
            qualityLevel=0.01,
            minDistance=6,
            blockSize=5,
        )
        minimum_points = int(config["minimumTrackedPoints"])
        if points is None or len(points) < minimum_points:
            return None
        tracked, status, errors = cv2.calcOpticalFlowPyrLK(
            previous,
            current,
            points,
            None,
            winSize=(21, 21),
            maxLevel=3,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 30, 0.01),
        )
        if tracked is None or status is None:
            return None
        valid = status.reshape(-1).astype(bool)
        valid &= np.all(np.isfinite(tracked.reshape(-1, 2)), axis=1)
        if errors is not None:
            valid &= np.isfinite(errors.reshape(-1))
        source = points.reshape(-1, 2)[valid]
        target = tracked.reshape(-1, 2)[valid]
        if len(source) < minimum_points:
            return None
        affine, inliers = cv2.estimateAffinePartial2D(
            source,
            target,
            method=cv2.RANSAC,
            ransacReprojThreshold=2.0,
            maxIters=2000,
            confidence=0.99,
            refineIters=10,
        )
        if affine is None or inliers is None:
            return None
        inlier_mask = inliers.reshape(-1).astype(bool)
        inlier_count = int(np.count_nonzero(inlier_mask))
        inlier_ratio = inlier_count / max(1, len(source))
        minimum_inlier_ratio = max(
            float(config["minimumInlierRatio"]),
            float(config.get("uniformMotionRatio", 0.0)),
        )
        if inlier_count < minimum_points or inlier_ratio < minimum_inlier_ratio:
            return None
        height, width = previous.shape[:2]
        inlier_points = source[inlier_mask]
        grid_x = np.clip((inlier_points[:, 0] * 4 / max(width, 1)).astype(int), 0, 3)
        grid_y = np.clip((inlier_points[:, 1] * 4 / max(height, 1)).astype(int), 0, 3)
        spatial_coverage = len(set(zip(grid_x.tolist(), grid_y.tolist()))) / 16.0
        if spatial_coverage < float(config["minimumSpatialCoverage"]):
            return None
        a, b, tx = affine[0]
        c, d, ty = affine[1]
        scale = max(float(np.sqrt(a * a + c * c)), np.finfo(np.float64).eps)
        radius = 0.5 * float(np.hypot(width, height))
        rotation_displacement = float(np.arctan2(c, a)) * radius
        scale_displacement = float(np.log(scale)) * radius
        motion = np.asarray(
            [tx, ty, rotation_displacement, scale_displacement],
            dtype=np.float64,
        )
        return motion, {
            "inlierRatio": inlier_ratio,
            "spatialCoverage": spatial_coverage,
            "trackedPoints": inlier_count,
        }

    def _hard(
        self,
        context: EpisodeContext,
        issue: str,
        explanation: str,
        *,
        camera_key: str | None = None,
        metrics: dict | None = None,
    ) -> Finding:
        return Finding(
            episode_index=context.episode.episode_index,
            issue_code=issue,
            category="integrity",
            severity="fatal",
            detector_id=self.detector_id,
            detector_version=self.version,
            camera_key=camera_key,
            metrics=metrics or {},
            explanation=explanation,
            suggested_decision="quarantine",
            hard_invalid=True,
        )
