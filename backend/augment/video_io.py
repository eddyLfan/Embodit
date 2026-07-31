"""Decode / encode RGB video frames for augmentation."""

from __future__ import annotations

from fractions import Fraction
from pathlib import Path

import numpy as np


def decode_video_rgb(path: Path, max_frames: int | None = None) -> np.ndarray:
    """Decode an MP4 into (T,H,W,3) uint8 RGB."""
    try:
        import av
    except ImportError as error:  # noqa: BLE001
        raise RuntimeError("需要安装 av（PyAV）才能解码视频") from error

    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)

    container = av.open(str(path))
    stream = container.streams.video[0]
    stream.thread_type = "AUTO"

    # Pre-allocate when the container reports a frame count to avoid the
    # list-append + stack double-buffering.
    expected = int(stream.frames or 0)
    if max_frames is not None and expected:
        expected = min(expected, max_frames)
    buffer: np.ndarray | None = None
    frames: list[np.ndarray] = []
    count = 0
    try:
        for frame in container.decode(stream):
            rgb = frame.to_ndarray(format="rgb24")
            if buffer is None and expected:
                buffer = np.empty((expected, *rgb.shape), dtype=np.uint8)
            if buffer is not None:
                if count < len(buffer):
                    buffer[count] = rgb
                else:
                    # Container under-reported; fall back to list for the tail.
                    frames.append(rgb)
            else:
                frames.append(rgb)
            count += 1
            if max_frames is not None and count >= max_frames:
                break
    finally:
        container.close()
    if count == 0:
        raise RuntimeError(f"未能解码任何帧：{path}")
    if buffer is not None:
        head = buffer[: min(count, len(buffer))]
        if frames:
            return np.concatenate([head, np.stack(frames, axis=0)], axis=0)
        return head
    return np.stack(frames, axis=0)


def encode_video_mp4(frames: np.ndarray, out_path: Path, fps: float = 30.0) -> Path:
    try:
        import av
    except ImportError as error:  # noqa: BLE001
        raise RuntimeError("需要安装 av（PyAV）才能编码视频") from error

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    _, height, width, _ = frames.shape
    # Keep fractional frame rates (29.97 etc.) instead of rounding to int.
    rate = Fraction(max(float(fps or 30.0), 1e-3)).limit_denominator(1001)
    container = av.open(str(out_path), mode="w")
    stream = container.add_stream("libx264", rate=rate)
    stream.width = int(width)
    stream.height = int(height)
    stream.pix_fmt = "yuv420p"
    stream.thread_type = "AUTO"
    stream.options = {"crf": "20", "preset": "veryfast"}
    try:
        for image in frames:
            frame = av.VideoFrame.from_ndarray(np.ascontiguousarray(image), format="rgb24")
            for packet in stream.encode(frame):
                container.mux(packet)
        for packet in stream.encode():
            container.mux(packet)
    finally:
        container.close()
    return out_path
