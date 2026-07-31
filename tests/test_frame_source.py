"""FrameSource episode-window tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from augment.video_io import encode_video_mp4
from augment.pipeline import _resize_frame
from datasets.frames import Mp4FrameSource, TopicFrameSource, episode_frame_source
from datasets.view import CameraRef, DatasetView, EpisodeView


def test_mp4_frame_source_slices_shared_shard(tmp_path: Path):
    frames = np.stack(
        [np.full((32, 32, 3), index * 20, dtype=np.uint8) for index in range(10)],
        axis=0,
    )
    path = tmp_path / "shared.mp4"
    encode_video_mp4(frames, path, fps=5.0)

    source = Mp4FrameSource(
        path,
        from_timestamp=0.4,
        fps=5.0,
        expected_frames=3,
    )
    sliced = source.load_rgb()
    assert sliced.shape == (3, 32, 32, 3)
    means = sliced.mean(axis=(1, 2, 3))
    assert np.allclose(means, [40, 60, 80], atol=5)


def test_mcap_topic_uses_direct_stream_instead_of_materialized_mp4(tmp_path: Path):
    class Adapter:
        def iter_topic_frames(self, episode, topic):
            assert episode.episode_index == 7
            assert topic == "/camera/compressed"
            yield np.full((8, 8, 3), 42, dtype=np.uint8)

        def materialize_topic_video(self, *_args):
            raise AssertionError("direct MCAP stream should avoid MP4 materialization")

    episode = EpisodeView(
        episode_index=7,
        length=1,
        duration=0.1,
        cameras={
            "camera": CameraRef(key="camera", kind="topic", topic="/camera/compressed")
        },
    )
    view = DatasetView(
        format_id="mcap",
        path=str(tmp_path),
        name="sample",
        fps=10.0,
        robot_type=None,
        features={},
        episodes=[episode],
    )
    source = episode_frame_source(Adapter(), view, episode, "camera")
    assert isinstance(source, TopicFrameSource)
    assert source.load_rgb().shape == (1, 8, 8, 3)


def test_preview_resize_preserves_aspect_ratio_and_small_frames():
    large = np.zeros((1300, 1600, 3), dtype=np.uint8)
    assert _resize_frame(large, 640).shape == (520, 640, 3)
    small = np.zeros((120, 160, 3), dtype=np.uint8)
    assert _resize_frame(small, 640) is small
