"""AugmentDatasetWriter streaming API tests (requires PyAV)."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("av")

from augment.output_writer import AugmentDatasetWriter  # noqa: E402


def _frames(n: int = 5, h: int = 32, w: int = 32) -> np.ndarray:
    rng = np.random.default_rng(0)
    return rng.integers(0, 255, size=(n, h, w, 3), dtype=np.uint8)


def test_streaming_writer_v3(tmp_path: Path):
    out = tmp_path / "aug_v3"
    writer = AugmentDatasetWriter(out, target_format="lerobot_v3", fps=10.0, job_id="job1")
    for ep in range(2):
        ctx = writer.begin_episode(source_episode_index=ep, length=5, tasks=["t"])
        writer.add_camera_video(ctx, "cam_a", _frames())
        writer.add_camera_video(ctx, "cam_b", _frames())
        writer.commit_episode(ctx, state=np.ones((5, 3)), action=np.zeros((5, 2)), sidecar={})
    result = writer.finalize()
    assert result["totalEpisodes"] == 2
    assert result["totalFrames"] == 10
    # per-episode video shards
    assert (out / "videos" / "cam_a" / "chunk-000" / "file-000.mp4").is_file()
    assert (out / "videos" / "cam_a" / "chunk-000" / "file-001.mp4").is_file()
    stats = json.loads((out / "meta" / "stats.json").read_text(encoding="utf-8"))
    assert stats["observation.state"]["count"] == [10]
    assert stats["action"]["mean"] == [0.0, 0.0]
    report = json.loads((out / "meta" / "augmentation_report.json").read_text(encoding="utf-8"))
    assert report["fidelity"] == "partial"
    assert report["targetFormat"] == "lerobot_v3"
    assert report["knownLosses"]
    # staging removed after finalize
    assert not list(tmp_path.glob(".aug_v3.augment-building*"))


def test_streaming_writer_rejects_inconsistent_cameras(tmp_path: Path):
    writer = AugmentDatasetWriter(tmp_path / "aug", target_format="lerobot_v3", fps=10.0)
    ctx = writer.begin_episode(source_episode_index=0, length=5, tasks=[])
    writer.add_camera_video(ctx, "cam_a", _frames())
    writer.commit_episode(ctx, state=None, action=np.zeros((5, 2)), sidecar={})
    ctx2 = writer.begin_episode(source_episode_index=1, length=5, tasks=[])
    writer.add_camera_video(ctx2, "cam_other", _frames())
    with pytest.raises(RuntimeError, match="相机集合"):
        writer.commit_episode(ctx2, state=None, action=np.zeros((5, 2)), sidecar={})
    writer.cleanup()


def test_streaming_writer_rejects_frame_mismatch(tmp_path: Path):
    writer = AugmentDatasetWriter(tmp_path / "aug2", target_format="lerobot_v21", fps=10.0)
    ctx = writer.begin_episode(source_episode_index=0, length=5, tasks=[])
    with pytest.raises(RuntimeError, match="frames"):
        writer.add_camera_video(ctx, "cam_a", _frames(n=3))
    writer.cleanup()


def test_v21_paths_roll_over_at_chunk_boundary(tmp_path: Path):
    writer = AugmentDatasetWriter(tmp_path / "aug3", target_format="lerobot_v21", fps=10.0)
    assert "chunk-001" in str(writer._video_path(1000, "cam_a"))
    writer.cleanup()
