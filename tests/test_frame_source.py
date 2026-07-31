"""FrameSource episode-window tests."""

from __future__ import annotations

from pathlib import Path

import numpy as np

from augment.video_io import encode_video_mp4
from datasets.frames import Mp4FrameSource


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
