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
    assert deep["detectors"]["cameraShake"]["sampleFps"] == 4.0
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
