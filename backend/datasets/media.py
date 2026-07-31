"""Shared streaming video helpers built on imageio-ffmpeg pipes."""

from __future__ import annotations

from pathlib import Path
from typing import Iterable, Iterator

import numpy as np


def _require_imageio_ffmpeg():
    try:
        import imageio_ffmpeg
    except ImportError as error:  # noqa: BLE001
        raise ImportError("视频编解码需要 imageio-ffmpeg：uv/pip install imageio-ffmpeg") from error
    return imageio_ffmpeg


def normalize_frame_uint8(frame: np.ndarray) -> np.ndarray:
    """Coerce a single H,W[,C] frame to uint8 RGB (3 channels)."""
    frame = np.asarray(frame)
    if frame.dtype != np.uint8:
        if np.issubdtype(frame.dtype, np.floating):
            # Heuristic: floats in [0, 1] need upscaling to [0, 255].
            scale = 255.0 if float(np.nanmax(frame)) <= 1.5 else 1.0
            frame = np.clip(frame * scale, 0, 255).astype(np.uint8)
        else:
            frame = np.clip(frame, 0, 255).astype(np.uint8)
    if frame.ndim == 2:
        frame = np.stack([frame] * 3, axis=-1)
    elif frame.ndim == 3 and frame.shape[-1] == 1:
        frame = np.repeat(frame, 3, axis=-1)
    elif frame.ndim == 3 and frame.shape[-1] == 4:
        frame = frame[..., :3]
    if frame.ndim != 3 or frame.shape[-1] != 3:
        raise ValueError(f"不支持的帧形状：{frame.shape}")
    return np.ascontiguousarray(frame)


def encode_frames_to_mp4(
    frames: Iterable[np.ndarray],
    output: Path,
    fps: float,
) -> int:
    """Stream RGB frames into an H.264 MP4. Returns the frame count."""
    imageio_ffmpeg = _require_imageio_ffmpeg()
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    writer = None
    count = 0
    try:
        for frame in frames:
            frame = normalize_frame_uint8(frame)
            if writer is None:
                height, width = frame.shape[:2]
                writer = imageio_ffmpeg.write_frames(
                    str(output),
                    size=(width, height),
                    fps=max(float(fps or 30.0), 1e-3),
                    codec="libx264",
                    quality=None,
                    ffmpeg_log_level="error",
                    output_params=[
                        "-preset", "veryfast",
                        "-crf", "23",
                        "-pix_fmt", "yuv420p",
                        "-movflags", "+faststart",
                    ],
                    macro_block_size=1,
                )
                writer.send(None)
            writer.send(frame)
            count += 1
    finally:
        if writer is not None:
            writer.close()
    if count == 0:
        output.unlink(missing_ok=True)
        raise ValueError("没有可编码的帧")
    return count


def decode_mp4_frames(path: Path) -> Iterator[np.ndarray]:
    """Yield RGB uint8 frames from an MP4 without loading the whole clip."""
    imageio_ffmpeg = _require_imageio_ffmpeg()
    reader = imageio_ffmpeg.read_frames(str(path))
    meta = reader.__next__()
    width, height = meta["size"]
    for raw in reader:
        yield np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 3)


def probe_mp4_meta(path: Path) -> dict:
    """Return {'size': (w, h), 'fps': float, 'nframes': int|None} for an MP4."""
    imageio_ffmpeg = _require_imageio_ffmpeg()
    reader = imageio_ffmpeg.read_frames(str(path))
    try:
        meta = reader.__next__()
    finally:
        reader.close()
    return {
        "size": tuple(meta.get("size") or ()),
        "fps": float(meta.get("fps") or 0.0),
        "nframes": meta.get("nframes"),
    }
