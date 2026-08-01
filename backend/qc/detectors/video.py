"""Full-decode video integrity plus visual quality and static-camera shake."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import re

import cv2
import numpy as np

from datasets.frames import episode_frame_source
from qc.intervals import mask_to_intervals
from qc.schema import DetectorOutcome, Finding

from .base import EpisodeContext


class VideoDetector:
    detector_id = "video"
    version = "2"
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
                else:
                    freeze_deltas.append(float(np.mean(cv2.absdiff(freeze_gray, previous_freeze))))
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
        freeze_mask = np.asarray(freeze_deltas) <= float(integrity["freezePixelDelta"])
        freeze_intervals = mask_to_intervals(
            freeze_mask,
            analysis_fps,
            minimum_seconds=float(integrity["freezeMinimumSeconds"]),
            merge_gap_seconds=0.1,
            context_seconds=context_seconds,
            duration=duration,
        )
        frozen_seconds = sum(end - start for start, end in freeze_intervals)
        for start, end in freeze_intervals:
            hard = duration > 0 and frozen_seconds / duration >= 0.5
            findings.append(
                Finding(
                    episode_index=context.episode.episode_index,
                    issue_code="integrity/video_frozen" if hard else "visual/frozen",
                    category="integrity" if hard else "visual",
                    severity="fatal" if hard else "error",
                    confidence=0.9,
                    detector_id=self.detector_id,
                    detector_version=self.version,
                    start_s=start,
                    end_s=end,
                    camera_key=camera_key,
                    metrics={"meanPixelDeltaThreshold": float(integrity["freezePixelDelta"])},
                    explanation=f"相机 {camera_key} 画面连续冻结",
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
            findings.extend(self._camera_shake(context, camera_key, sampled_grays, duration, shake, shake_sample_fps))
        return findings

    def _is_static_camera(self, key: str, config: dict) -> bool:
        configured = {str(item) for item in config.get("staticCameraKeys") or []}
        if configured:
            return key in configured
        lowered = key.lower()
        return any(token in lowered for token in ("base", "head", "main", "front", "overhead")) and not any(
            token in lowered for token in ("wrist", "hand", "eef")
        )

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
        vectors: list[np.ndarray] = []
        uniformities: list[float] = []
        for previous, current in zip(frames, frames[1:]):
            flow = cv2.calcOpticalFlowFarneback(previous, current, None, 0.5, 3, 15, 3, 5, 1.2, 0)
            vector = np.median(flow.reshape(-1, 2), axis=0)
            residual = np.linalg.norm(flow - vector, axis=2)
            tolerance = max(0.75, float(np.median(residual)) * 2.5)
            vectors.append(vector)
            uniformities.append(float(np.mean(residual <= tolerance)))
        changes = np.linalg.norm(np.diff(np.stack(vectors), axis=0), axis=1)
        uniforms = np.asarray(uniformities[1:])
        mask = (changes > float(config["jitterThreshold"])) & (
            uniforms >= float(config["uniformMotionRatio"])
        )
        sample_fps = max(float(sample_fps), 1e-6)
        intervals = mask_to_intervals(
            np.r_[False, False, mask],
            sample_fps,
            minimum_seconds=1.0 / sample_fps,
            merge_gap_seconds=1.0 / sample_fps,
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
                metrics={"globalMotionChangeMax": float(np.max(changes)) if changes.size else 0.0},
                threshold={
                    "jitter": float(config["jitterThreshold"]),
                    "uniformMotionRatio": float(config["uniformMotionRatio"]),
                },
                explanation=f"静态相机 {camera_key} 出现全局抖动",
                suggested_decision="review",
            )
            for start, end in intervals
        ]

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
