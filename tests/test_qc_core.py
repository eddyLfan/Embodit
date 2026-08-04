"""Core automatic-QC schema, scoring, detector and report tests."""

from __future__ import annotations
import threading
import time

from pathlib import Path
from types import SimpleNamespace

import numpy as np
from datasets.frames import FrameSource

from qc.config import merge_config
from qc.detectors.base import EpisodeContext
from qc.detectors.gripper import GripperDetector
from qc.detectors.motion import MotionDetector
from qc.detectors.signal_integrity import SignalIntegrityDetector
from qc.detectors.video import VideoDetector
from qc.intervals import mask_to_intervals, union_duration
from qc.pipeline import run_scan
from qc.scoring import score_episode
from qc.store import (
    episode_detail,
    initialize_report,
    query_episodes,
    review_finding,
    selected_episode_indices,
    summary,
    upsert_detector_run,
    write_episode,
)


def test_intervals_merge_and_score_hard_invalid() -> None:
    intervals = mask_to_intervals(
        [False, True, True, False, True, True, False],
        2.0,
        minimum_seconds=0.5,
        merge_gap_seconds=0.5,
        duration=3.5,
    )
    assert intervals == [(0.5, 3.0)]
    assert union_duration([(0.5, 1.5), (1.0, 2.0)], 4.0) == 1.5
    result = score_episode(
        10.0,
        [{"severity": "fatal", "confidence": 1.0, "hard_invalid": True, "start_s": None, "end_s": None}],
        [{"status": "completed", "coverageWeight": 3.0}],
        merge_config(),
    )
    assert result == {
        "integrityStatus": "invalid",
        "usableRatio": 0.0,
        "qualityScore": 0.0,
        "coverage": 100.0,
        "autoDecision": "quarantine",
    }



def test_scan_profiles_preserve_integrity_and_allow_explicit_overrides() -> None:
    fast = merge_config({"profile": "fast"})
    assert fast["detectors"]["videoIntegrity"]["enabled"] is True
    assert fast["detectors"]["visualQuality"]["enabled"] is False
    assert fast["detectors"]["cameraShake"]["enabled"] is False
    assert fast["runtime"]["episodeWorkers"] == 4
    deep = merge_config({"profile": "deep"})
    assert deep["detectors"]["visualQuality"]["resizeWidth"] == 480
    assert deep["detectors"]["cameraShake"]["sampleFps"] == 8.0
    overridden = merge_config({"profile": "fast", "detectors": {"visualQuality": {"enabled": True}}})
    assert overridden["detectors"]["visualQuality"]["enabled"] is True
    capped = merge_config({"runtime": {"episodeWorkers": 100, "cameraWorkers": 0}})
    assert capped["runtime"] == {"sleepBetweenEpisodes": 0.0, "episodeWorkers": 16, "cameraWorkers": 1}


def test_video_quality_metrics_are_sampled_and_fast_skips_them(monkeypatch) -> None:
    class Source(FrameSource):
        def iter_rgb(self):
            for index in range(60):
                yield np.full((64, 64, 3), (index * 7) % 255, dtype=np.uint8)

    import qc.detectors.video as video_module

    original_laplacian = video_module.cv2.Laplacian
    calls = {"count": 0}

    def counted_laplacian(*args, **kwargs):
        calls["count"] += 1
        return original_laplacian(*args, **kwargs)

    monkeypatch.setattr(video_module.cv2, "Laplacian", counted_laplacian)
    standard = _context({}, length=60, fps=20.0)
    standard.config = merge_config({"profile": "standard"})
    VideoDetector()._scan_camera(standard, "wrist", Source())
    standard_calls = calls["count"]
    assert 0 < standard_calls <= 20

    fast = _context({}, length=60, fps=20.0)
    fast.config = merge_config({"profile": "fast"})
    VideoDetector()._scan_camera(fast, "wrist", Source())
    assert calls["count"] == standard_calls


def _scan_video_frames(frames: list[np.ndarray], signals: dict[str, np.ndarray]):
    class Source(FrameSource):
        def iter_rgb(self):
            yield from frames

    context = _context(signals, length=len(frames), fps=20.0)
    context.config = merge_config(
        {
            "detectors": {
                "visualQuality": {"enabled": False},
                "cameraShake": {"enabled": False},
            }
        }
    )
    return VideoDetector()._scan_camera(context, "wrist", Source())


def test_video_freeze_requires_near_duplicate_pixels_and_cross_modal_motion() -> None:
    y, x = np.indices((64, 96))
    base = np.stack(((x * 3) % 200, (y * 5) % 200, ((x + y) * 2) % 200), axis=-1).astype(
        np.uint8
    )
    frames = []
    for index in range(100):
        shift = 4 if 20 <= index < 80 else index // 4
        frames.append(np.roll(base, shift, axis=1))
    action = np.zeros((100, 2), dtype=np.float64)
    action[:, 0] = np.linspace(0.0, 1.0, len(action))

    findings = _scan_video_frames(frames, {"action": action})
    frozen = [item for item in findings if item.issue_code == "integrity/video_frozen"]
    assert frozen
    assert frozen[0].metrics["changedPixelRatioMax"] == 0.0
    assert frozen[0].metrics["signalMotionRangeRatio"] > 0.02


def test_video_freeze_does_not_mislabel_a_naturally_stationary_episode() -> None:
    rng = np.random.default_rng(7)
    frame = rng.integers(0, 255, size=(64, 96, 3), dtype=np.uint8)
    frames = [frame.copy() for _ in range(100)]
    action = np.zeros((100, 2), dtype=np.float64)

    findings = _scan_video_frames(frames, {"action": action})
    assert not any("frozen" in item.issue_code for item in findings)

    unverified = _scan_video_frames(frames, {})
    suspected = [item for item in unverified if item.issue_code == "visual/frozen"]
    assert suspected and not suspected[0].hard_invalid
    assert suspected[0].confidence == 0.7


def test_video_freeze_rejects_low_amplitude_sensor_noise() -> None:
    rng = np.random.default_rng(11)
    base = rng.integers(20, 230, size=(64, 96, 3), dtype=np.uint8)
    frames = []
    for _index in range(100):
        noisy = base.copy()
        changed = rng.random(base.shape[:2]) < 0.2
        noisy[changed] = np.minimum(noisy[changed].astype(np.int16) + 1, 255).astype(np.uint8)
        frames.append(noisy)
    action = np.zeros((100, 2), dtype=np.float64)
    action[:, 0] = np.linspace(0.0, 1.0, len(action))

    findings = _scan_video_frames(frames, {"action": action})
    assert not any("frozen" in item.issue_code for item in findings)


def test_camera_shake_uses_robust_global_motion_and_ignores_smooth_pan() -> None:
    rng = np.random.default_rng(19)
    base = rng.integers(0, 255, size=(96, 128), dtype=np.uint8)
    detector = VideoDetector()
    context = _context({}, length=10, fps=10.0)
    config = context.config["detectors"]["cameraShake"]

    jitter_frames = [np.roll(base, shift, axis=1) for shift in (0, 5, -5, 5, -5, 5, -5, 0)]
    jitter = detector._camera_shake(
        context,
        "front",
        jitter_frames,
        len(jitter_frames) / 10.0,
        config,
        10.0,
    )
    assert any(item.issue_code == "visual/camera_shake" for item in jitter)

    smooth_frames = [np.roll(base, shift, axis=1) for shift in range(0, 16, 2)]
    smooth = detector._camera_shake(
        context,
        "front",
        smooth_frames,
        len(smooth_frames) / 10.0,
        config,
        10.0,
    )
    assert not smooth

    foreground_frames = []
    for left in (2, 12, 22, 32, 42, 52, 62, 72):
        frame = base.copy()
        frame[30:60, left : left + 24] = 255
        foreground_frames.append(frame)
    foreground = detector._camera_shake(
        context,
        "front",
        foreground_frames,
        len(foreground_frames) / 10.0,
        config,
        10.0,
    )
    assert not foreground



def _context(signals: dict[str, np.ndarray], *, length: int = 100, fps: float = 20.0):
    view = SimpleNamespace(
        fps=fps,
        features={
            "action": {"names": ["joint.x", "gripper.pos"]},
            "observation.state": {"names": ["joint.x", "gripper.pos"]},
        },
    )
    episode = SimpleNamespace(episode_index=3, length=length, duration=length / fps, cameras={})
    return EpisodeContext(
        adapter=None,
        view=view,
        episode=episode,
        signals=signals,
        config=merge_config(),
    )


def test_signal_integrity_marks_nonfinite_hard_invalid() -> None:
    action = np.zeros((100, 2), dtype=np.float64)
    action[10, 0] = np.nan
    outcome = SignalIntegrityDetector().run(_context({"action": action}))
    assert any(item.issue_code == "integrity/non_finite_signal" for item in outcome.findings)
    assert any(item.hard_invalid for item in outcome.findings)


def test_motion_and_regrasp_candidates_localize_intervals() -> None:
    action = np.zeros((100, 2), dtype=np.float64)
    action[:, 0] = np.linspace(0, 1, 100)
    action[50, 0] += 10
    action[20:, 1] = 1
    action[30:, 1] = 0
    action[40:, 1] = 1
    context = _context({"action": action})
    assert any(item.issue_code == "motion/jitter" for item in MotionDetector().run(context).findings)
    assert any(
        item.issue_code == "manipulation/regrasp_candidate"
        for item in GripperDetector().run(context).findings
    )


def test_motion_jitter_ignores_tiny_low_range_noise() -> None:
    action = np.zeros((120, 2), dtype=np.float64)
    action[40:43, 0] = [2e-7, -3e-7, 2e-7]
    outcome = MotionDetector().run(_context({"action": action}, length=len(action)))
    assert not any(item.issue_code == "motion/jitter" for item in outcome.findings)


def test_motion_jitter_does_not_mislabel_a_one_way_setpoint_step() -> None:
    action = np.zeros((120, 2), dtype=np.float64)
    action[60:, 0] = 1.0
    outcome = MotionDetector().run(_context({"action": action}, length=len(action)))
    assert not any(item.issue_code == "motion/jitter" for item in outcome.findings)


def test_motion_jitter_prefers_action_over_measured_state() -> None:
    action = np.zeros((120, 2), dtype=np.float64)
    action[:, 0] = np.linspace(0, 1, len(action))
    state = action.copy()
    state[60, 0] += 10
    outcome = MotionDetector().run(
        _context({"action": action, "observation.state": state}, length=len(action))
    )
    assert not any(item.issue_code == "motion/jitter" for item in outcome.findings)


def test_motion_jitter_falls_back_to_state_without_action() -> None:
    state = np.zeros((120, 2), dtype=np.float64)
    state[:, 0] = np.linspace(0, 1, len(state))
    state[60, 0] += 10
    outcome = MotionDetector().run(
        _context({"observation.state": state}, length=len(state))
    )
    assert any(item.issue_code == "motion/jitter" for item in outcome.findings)


def test_motion_jitter_config_normalizes_signal_key() -> None:
    config = merge_config({"detectors": {"motion": {"jitterSignalKeys": "observation.state"}}})
    assert config["detectors"]["motion"]["jitterSignalKeys"] == ["observation.state"]


def test_pipeline_parallelizes_analysis_and_serializes_report_writes(
    monkeypatch,
    tmp_path: Path,
) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    report = tmp_path / "parallel.qc.sqlite3"
    episodes = [
        SimpleNamespace(
            episode_index=index,
            length=10,
            duration=1.0,
            cameras={},
            extras={},
            tasks=[],
        )
        for index in range(8)
    ]
    view = SimpleNamespace(
        format_id="fake",
        path=str(dataset),
        name="fake",
        fps=10.0,
        robot_type=None,
        features={"action": {"names": ["joint"]}},
        episodes=episodes,
    )
    tracker = {"active": 0, "maximum": 0}
    lock = threading.Lock()

    class Adapter:
        def inspect(self):
            return view

        def get_timeseries(self, _episode_index):
            with lock:
                tracker["active"] += 1
                tracker["maximum"] = max(tracker["maximum"], tracker["active"])
            try:
                time.sleep(0.02)
                return {"action": np.zeros((10, 1), dtype=np.float64)}
            finally:
                with lock:
                    tracker["active"] -= 1

    monkeypatch.setattr("qc.pipeline.open_dataset", lambda _path: Adapter())
    monkeypatch.setattr("qc.pipeline.os.cpu_count", lambda: 8)
    result = run_scan(
        dataset,
        report,
        scan_id="parallel",
        use_cache=False,
        config={
            "requirements": {"minimumCameras": 0},
            "detectors": {
                "videoIntegrity": {"enabled": False},
                "motion": {"enabled": False},
                "gripper": {"enabled": False},
            },
            "runtime": {"episodeWorkers": 4, "cameraWorkers": 1},
        },
    )
    assert result["totalEpisodes"] == 8
    assert result["episodeWorkers"] == 4
    assert result["cameraWorkers"] == 0
    assert tracker["maximum"] >= 2
    assert summary(report)["totals"]["episodes"] == 8


def test_sqlite_report_query_review_and_selection(tmp_path: Path) -> None:
    report = tmp_path / "scan.qc.sqlite3"
    initialize_report(
        report,
        {
            "scanId": "scan1", "datasetPath": "/data/example",
            "datasetFormat": "lerobot_v3", "datasetId": "dataset1",
            "datasetFingerprint": "fingerprint1", "config": merge_config(),
            "configHash": "config1", "totalEpisodes": 2,
        },
    )
    upsert_detector_run(
        report,
        {
            "detectorId": "signal_integrity", "version": "1", "enabled": True,
            "status": "completed", "processedCount": 2, "failedCount": 0,
            "coverageWeight": 3.0, "config": {},
        },
    )
    finding = {
        "finding_id": "f1", "stable_signature": "s1", "episode_index": 0,
        "issue_code": "integrity/missing_action", "category": "integrity",
        "severity": "fatal", "confidence": 1.0, "detector_id": "signal_integrity",
        "detector_version": "1", "start_s": None, "end_s": None,
        "camera_key": None, "signal_key": "action", "dimension_indices": [],
        "metrics": {}, "threshold": {}, "explanation": "missing",
        "suggested_decision": "quarantine", "hard_invalid": True,
    }
    detectors = [{"detectorId": "signal_integrity", "version": "1", "status": "completed", "coverageWeight": 3.0}]
    write_episode(
        report,
        {
            "episodeIndex": 0, "duration": 5, "frameCount": 100,
            "integrityStatus": "invalid", "autoDecision": "quarantine",
            "usableRatio": 0, "qualityScore": 0, "coverage": 100,
        },
        [finding],
        detectors,
    )
    write_episode(
        report,
        {
            "episodeIndex": 1, "duration": 5, "frameCount": 100,
            "integrityStatus": "valid", "autoDecision": "pass",
            "usableRatio": 100, "qualityScore": 100, "coverage": 100,
        },
        [],
        detectors,
    )
    report_summary = summary(report)
    assert report_summary["totals"]["invalid"] == 1
    assert report_summary["detectors"][0]["coverage"] == 100.0
    assert report_summary["detectors"][0]["skippedCount"] == 0
    assert [row["episodeIndex"] for row in query_episodes(report, {"decision": "pass"})["episodes"]] == [1]
    selection = selected_episode_indices(report, {})
    assert selection["episodes"] == [1]
    assert selection["invalidEpisodes"] == [0]
    review_finding(report, "f1", {"reviewStatus": "confirmed", "note": "checked"}, "tester")
    detail = episode_detail(report, 0)
    assert detail["findings"][0]["reviewStatus"] == "confirmed"
    assert detail["detectors"][0]["coverageWeight"] == 3.0
    assert detail["detectors"][0]["skipReason"] is None

    review_finding(report, "f1", {"reviewStatus": "rejected", "note": "false positive"}, "tester")
    detail = episode_detail(report, 0)
    assert detail["findings"][0]["reviewStatus"] == "rejected"
    episode = next(row for row in query_episodes(report, {})["episodes"] if row["episodeIndex"] == 0)
    assert episode["findingCount"] == 0
    assert episode["issues"] == []
    assert summary(report)["issues"] == []
    assert query_episodes(report, {"issueCodes": ["integrity/missing_action"]})["total"] == 0

    review_finding(report, "f1", {"reviewStatus": "unreviewed"}, "tester")
    episode = next(row for row in query_episodes(report, {})["episodes"] if row["episodeIndex"] == 0)
    assert episode["findingCount"] == 1
