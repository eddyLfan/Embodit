"""FrameSource episode-window tests."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import av
import numpy as np

from datasets.frames import Mp4FrameSource, TopicFrameSource, episode_frame_source
from datasets.view import CameraRef, DatasetView, EpisodeView


def encode_video_mp4(frames: np.ndarray, path: Path, fps: float) -> None:
    """Write a tiny test fixture without depending on a product feature module."""
    height, width = frames.shape[1:3]
    with av.open(str(path), mode="w") as container:
        stream = container.add_stream("libx264", rate=Fraction(str(fps)))
        stream.width = width
        stream.height = height
        stream.pix_fmt = "yuv420p"
        for pixels in frames:
            frame = av.VideoFrame.from_ndarray(pixels, format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)


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


def test_mp4_sampled_decode_counts_every_frame_but_materializes_stride(tmp_path: Path):
    frames = np.stack(
        [np.full((24, 24, 3), index * 30, dtype=np.uint8) for index in range(6)],
        axis=0,
    )
    path = tmp_path / "sampled.mp4"
    encode_video_mp4(frames, path, fps=6.0)
    events = list(Mp4FrameSource(path, fps=6.0, expected_frames=6).iter_rgb_samples(3))
    assert [index for index, _frame in events] == list(range(6))
    assert [index for index, frame in events if frame is not None] == [0, 3]


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
