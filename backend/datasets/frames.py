"""Unified frame-source abstraction.

A camera's pixels can live in three places depending on the dataset format:

- an MP4 file on disk (LeRobot v2.1 / v3),
- embedded arrays / compressed frames that the adapter can decode
  (HDF5 ``obs`` datasets, MCAP CompressedImage topics),
- a directory of image files.

``episode_frame_source`` normalizes all of them into a :class:`FrameSource`
so consumers (augment, convert) do not care where frames come from.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator

import numpy as np

from .view import CameraRef, DatasetView, EpisodeView

IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png"}


class FrameSource:
    """A stream of decoded uint8 RGB frames for one episode camera.

    ``video_path`` is set when the source is backed by an MP4 file so callers
    with a faster/threaded decoder can use it directly.
    """

    kind: str = "unknown"
    video_path: Path | None = None

    def iter_rgb(self) -> Iterator[np.ndarray]:
        raise NotImplementedError

    def load_rgb(self) -> np.ndarray:
        """Materialize all frames as one (T, H, W, 3) uint8 array."""
        frames = list(self.iter_rgb())
        if not frames:
            raise ValueError("帧源为空")
        return np.stack(frames, axis=0)


class Mp4FrameSource(FrameSource):
    kind = "video"

    def __init__(self, path: Path) -> None:
        self.video_path = Path(path)
        if not self.video_path.is_file():
            raise FileNotFoundError(f"视频文件不存在：{self.video_path}")

    def iter_rgb(self) -> Iterator[np.ndarray]:
        from . import media

        yield from media.decode_mp4_frames(self.video_path)


class AdapterFrameSource(FrameSource):
    """Frames decoded on demand by the dataset adapter (HDF5 embedded, …)."""

    kind = "frames"

    def __init__(self, adapter: Any, episode_index: int, camera_key: str) -> None:
        self.adapter = adapter
        self.episode_index = int(episode_index)
        self.camera_key = camera_key

    def iter_rgb(self) -> Iterator[np.ndarray]:
        yield from self.adapter.get_frames(self.episode_index, self.camera_key)


class ImageDirFrameSource(FrameSource):
    kind = "image_dir"

    def __init__(self, directory: Path) -> None:
        self.directory = Path(directory)
        if not self.directory.is_dir():
            raise FileNotFoundError(f"图像目录不存在：{self.directory}")

    def iter_rgb(self) -> Iterator[np.ndarray]:
        try:
            import imageio.v3 as iio
        except ImportError as error:  # pragma: no cover
            raise ImportError("读取图像目录需要 imageio") from error

        files = sorted(
            item for item in self.directory.iterdir() if item.suffix.lower() in IMAGE_SUFFIXES
        )
        if not files:
            raise ValueError(f"图像目录为空：{self.directory}")
        for item in files:
            frame = np.asarray(iio.imread(item))
            if frame.ndim == 2:
                frame = np.stack([frame] * 3, axis=-1)
            yield frame[..., :3].astype(np.uint8, copy=False)


def episode_frame_source(
    adapter: Any,
    view: DatasetView,
    ep: EpisodeView,
    cam_key: str,
) -> FrameSource | None:
    """Best frame source for one episode camera, or None when unsupported."""
    cam: CameraRef | None = ep.cameras.get(cam_key)
    if cam is None:
        return None

    if cam.kind == "video" and cam.path:
        abs_path = Path(view.path) / cam.path
        if abs_path.is_file():
            return Mp4FrameSource(abs_path)
        if abs_path.is_dir():
            return ImageDirFrameSource(abs_path)
        return None

    if cam.kind == "frames":
        # HDF5-style embedded frames: adapter decodes them on demand.
        try:
            return AdapterFrameSource(adapter, ep.episode_index, cam_key)
        except Exception:  # noqa: BLE001
            return None

    if cam.kind == "topic":
        # MCAP topics: materialize once into a cached MP4, then treat as video.
        materialize = getattr(adapter, "materialize_topic_video", None)
        if materialize is None or not cam.topic:
            return None
        path = materialize(ep.episode_index, cam.topic)
        return Mp4FrameSource(Path(path))

    return None
