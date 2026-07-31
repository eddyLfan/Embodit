"""RoboMimic-style HDF5 adapter (single-file or multi-file episode dirs)."""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from . import media
from .base import DatasetAdapter, DatasetWriter
from .detect import collect_hdf5_files
from .view import FORMAT_HDF5, CameraRef, DatasetView, EpisodeView

_video_locks: dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


def _open_h5(path: Path):
    try:
        import h5py
    except ImportError as error:
        raise ImportError("读取 HDF5 需要安装 h5py：uv/pip install h5py") from error
    return h5py.File(path, "r")


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:  # noqa: BLE001
        raise ImportError("HDF5 视频预览需要 imageio-ffmpeg：uv/pip install imageio-ffmpeg") from error


def _demo_keys(handle) -> list[str]:
    if "data" in handle:
        group = handle["data"]
        keys = sorted(group.keys(), key=lambda k: (len(k), k))
        return [k for k in keys if k.startswith("demo_")]
    # flat demos at root
    keys = sorted(handle.keys(), key=lambda k: (len(k), k))
    return [k for k in keys if k.startswith("demo_")]


def _group_for(handle, demo_key: str):
    if "data" in handle and demo_key in handle["data"]:
        return handle["data"][demo_key]
    return handle[demo_key]


def _infer_task_name(path: Path) -> str:
    if path.is_file():
        path = path.parent
    for candidate in (path, path.parent):
        name = candidate.name
        if name and not name.isdigit():
            return name
    return path.name


def _episode_length(group) -> int:
    if "actions" in group:
        return int(group["actions"].shape[0])
    if "states" in group:
        return int(group["states"].shape[0])
    if "obs" in group:
        obs = group["obs"]
        for name in obs.keys():
            dset = obs[name]
            if getattr(dset, "ndim", 0) >= 1:
                return int(dset.shape[0])
    return 0


def _infer_fps(env_args: Any) -> float | None:
    """Pull the control frequency out of robomimic-style env_args when present."""
    if not isinstance(env_args, dict):
        return None
    candidates: list[Any] = [
        env_args.get("control_freq"),
        env_args.get("control_hz"),
        env_args.get("fps"),
    ]
    kwargs = env_args.get("env_kwargs")
    if isinstance(kwargs, dict):
        candidates.extend([kwargs.get("control_freq"), kwargs.get("control_hz"), kwargs.get("fps")])
    for value in candidates:
        try:
            fps = float(value)
        except (TypeError, ValueError):
            continue
        if fps > 0:
            return fps
    return None


def _camera_names(group) -> list[str]:
    if "obs" not in group:
        return []
    names = []
    for name in group["obs"].keys():
        dset = group["obs"][name]
        if getattr(dset, "ndim", 0) >= 3:
            names.append(name)
    return names


class Hdf5Adapter(DatasetAdapter):
    format_id = FORMAT_HDF5

    def __init__(self, path: Path):
        super().__init__(path)
        self._view_cache: DatasetView | None = None
        self._files_cache: list[Path] | None = None

    @classmethod
    def detect(cls, path: Path) -> bool:
        return bool(collect_hdf5_files(path))

    def _files(self) -> list[Path]:
        if self._files_cache is None:
            files = collect_hdf5_files(self.path)
            if not files:
                raise ValueError(f"未找到 HDF5 文件：{self.path}")
            self._files_cache = files
        return self._files_cache

    def _file(self) -> Path:
        files = self._files()
        if len(files) != 1:
            raise ValueError(f"单文件接口需要恰好一个 HDF5：{self.path}")
        return files[0]

    def inspect(self) -> DatasetView:
        if self._view_cache is None:
            self._view_cache = self._inspect_uncached()
        return self._view_cache

    def _inspect_uncached(self) -> DatasetView:
        files = self._files()
        task = _infer_task_name(self.path)
        from settings import HDF5_DEFAULT_FPS

        fps = HDF5_DEFAULT_FPS
        fps_assumed = True
        episodes: list[EpisodeView] = []
        features: dict[str, Any] = {}
        env_args: dict[str, Any] = {}
        dialect = "robomimic"

        for file_path in files:
            with _open_h5(file_path) as handle:
                demos = _demo_keys(handle)
                if not demos:
                    # Treat whole file as a single anonymous episode if no demo_* keys.
                    demos = [""]
                if not env_args and "data" in handle and "env_args" in handle["data"].attrs:
                    raw = handle["data"].attrs["env_args"]
                    if isinstance(raw, bytes):
                        raw = raw.decode("utf-8")
                    try:
                        env_args = json.loads(raw) if isinstance(raw, str) else {}
                    except json.JSONDecodeError:
                        env_args = {}
                    inferred = _infer_fps(env_args)
                    if inferred:
                        fps = inferred
                        fps_assumed = False
                for demo_key in demos:
                    if demo_key:
                        group = _group_for(handle, demo_key)
                    else:
                        group = handle["data"] if "data" in handle else handle
                    length = _episode_length(group)
                    cameras: dict[str, CameraRef] = {}
                    for name in _camera_names(group):
                        cameras[name] = CameraRef(
                            key=name,
                            kind="frames",
                            path=None,
                            from_timestamp=0.0,
                            to_timestamp=(length / fps) if fps else 0.0,
                        )
                        dset = group["obs"][name]
                        features[name] = {"dtype": "image", "shape": list(dset.shape[1:])}
                    if "actions" in group:
                        features["action"] = {
                            "dtype": "float32",
                            "shape": list(group["actions"].shape[1:]),
                        }
                    if "states" in group or ("obs" in group and "states" in group["obs"]):
                        src = group["states"] if "states" in group else group["obs"]["states"]
                        features["observation.state"] = {
                            "dtype": "float32",
                            "shape": list(src.shape[1:]),
                        }
                    episodes.append(
                        EpisodeView(
                            episode_index=len(episodes),
                            length=length,
                            duration=length / fps if fps else 0.0,
                            tasks=[task],
                            cameras=cameras,
                            extras={
                                "demoKey": demo_key or None,
                                "hdf5File": str(file_path),
                                "hdf5Name": file_path.name,
                                "prompt": task,
                            },
                        )
                    )

        root = self.path if self.path.is_dir() else self.path.parent
        name = root.name if len(files) > 1 or self.path.is_dir() else files[0].name
        path_str = str(root if len(files) > 1 or self.path.is_dir() else files[0])
        return DatasetView(
            format_id=FORMAT_HDF5,
            path=path_str,
            name=name,
            fps=fps,
            robot_type=env_args.get("env_name") if isinstance(env_args, dict) else None,
            features=features,
            episodes=episodes,
            total_frames=sum(ep.length for ep in episodes),
            total_tasks=1 if task else 0,
            extras={
                "dialect": dialect,
                "layout": "multifile" if len(files) > 1 else "single",
                "prompt": task,
                "fileCount": len(files),
                "fpsAssumed": fps_assumed,
            },
        )

    def _episode_ref(self, episode_index: int) -> tuple[Path, str | None, EpisodeView]:
        view = self.inspect()
        if episode_index < 0 or episode_index >= len(view.episodes):
            raise IndexError(f"episode_index 超出范围：{episode_index}")
        ep = view.episodes[episode_index]
        file_path = Path(ep.extras.get("hdf5File") or self._files()[0])
        demo_key = ep.extras.get("demoKey")
        return file_path, demo_key, ep

    def get_timeseries(self, episode_index: int, keys: list[str] | None = None) -> dict[str, np.ndarray]:
        file_path, demo_key, _ep = self._episode_ref(episode_index)
        result: dict[str, np.ndarray] = {}
        with _open_h5(file_path) as handle:
            group = _group_for(handle, demo_key) if demo_key else (handle["data"] if "data" in handle else handle)
            if "actions" in group and (keys is None or "action" in keys or "actions" in keys):
                result["action"] = np.asarray(group["actions"][()], dtype=np.float64)
            if "states" in group and (keys is None or "observation.state" in keys or "states" in keys):
                result["observation.state"] = np.asarray(group["states"][()], dtype=np.float64)
            if "obs" in group and "states" in group["obs"]:
                if keys is None or "observation.state" in keys:
                    result["observation.state"] = np.asarray(group["obs"]["states"][()], dtype=np.float64)
            # Common eef position keys for XY trajectory
            if "obs" in group:
                for name in ("robot0_eef_pos", "robot1_eef_pos", "ee_pos", "eef_pos"):
                    if name in group["obs"]:
                        result[f"eef.{name}"] = np.asarray(group["obs"][name][()], dtype=np.float64)
        return result

    def get_frames(self, episode_index: int, camera_key: str, chunk: int = 64):
        """Yield uint8 RGB frames for an in-HDF5 camera without loading the full clip."""
        file_path, demo_key, _ep = self._episode_ref(episode_index)
        with _open_h5(file_path) as handle:
            group = _group_for(handle, demo_key) if demo_key else (handle["data"] if "data" in handle else handle)
            if "obs" not in group or camera_key not in group["obs"]:
                raise ValueError(f"相机不存在：{camera_key}")
            dset = group["obs"][camera_key]
            total = int(dset.shape[0])
            for start in range(0, total, max(1, chunk)):
                block = np.asarray(dset[start : start + chunk])
                for frame in block:
                    yield media.normalize_frame_uint8(frame)

    def materialize_camera_video(self, episode_index: int, camera_key: str) -> Path:
        """Encode in-HDF5 image frames to a cached MP4 for browser playback."""
        file_path, demo_key, ep = self._episode_ref(episode_index)
        stamp = f"{file_path.stat().st_mtime_ns}:{file_path.stat().st_size}:{episode_index}:{demo_key}:{camera_key}"
        digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16]
        cache_dir = Path(tempfile.gettempdir()) / "embody-hdf5-video"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{digest}.mp4"
        if out.is_file() and out.stat().st_size > 0:
            return out

        with _video_locks_guard:
            lock = _video_locks.setdefault(digest, threading.Lock())
        with lock:
            if out.is_file() and out.stat().st_size > 0:
                return out
            from settings import HDF5_DEFAULT_FPS

            fps = float(self.inspect().fps or HDF5_DEFAULT_FPS)
            with _open_h5(file_path) as handle:
                group = _group_for(handle, demo_key) if demo_key else (handle["data"] if "data" in handle else handle)
                if "obs" not in group or camera_key not in group["obs"]:
                    raise ValueError(f"相机不存在：{camera_key}")
                dset = group["obs"][camera_key]
                if getattr(dset, "ndim", 0) < 3:
                    raise ValueError(f"相机帧维度异常：{dset.shape}")
                sample = media.normalize_frame_uint8(np.asarray(dset[0]))
                height, width = sample.shape[:2]

                ffmpeg = _ffmpeg_exe()
                with tempfile.TemporaryDirectory(prefix="embody-hdf5-") as tmp:
                    tmp_out = Path(tmp) / "out.mp4"
                    cmd = [
                        ffmpeg,
                        "-y",
                        "-f",
                        "rawvideo",
                        "-pix_fmt",
                        "rgb24",
                        "-s",
                        f"{width}x{height}",
                        "-r",
                        str(fps),
                        "-i",
                        "pipe:0",
                        "-c:v",
                        "libx264",
                        "-preset",
                        "veryfast",
                        "-pix_fmt",
                        "yuv420p",
                        "-an",
                        "-movflags",
                        "+faststart",
                        str(tmp_out),
                    ]
                    proc = subprocess.Popen(
                        cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.PIPE,
                    )
                    assert proc.stdin is not None and proc.stderr is not None
                    try:
                        # Stream frames in blocks so the whole camera array is
                        # never resident in memory.
                        total = int(dset.shape[0])
                        block = 64
                        for start in range(0, total, block):
                            chunk = np.asarray(dset[start : start + block])
                            for frame in chunk:
                                proc.stdin.write(media.normalize_frame_uint8(frame).tobytes())
                    except BrokenPipeError:
                        pass
                    finally:
                        try:
                            proc.stdin.close()
                        except Exception:  # noqa: BLE001
                            pass
                    stderr = proc.stderr.read()
                    code = proc.wait()
                    if code != 0 or not tmp_out.is_file():
                        detail = (stderr or b"").decode("utf-8", errors="ignore")[-400:]
                        raise RuntimeError(f"HDF5 视频转码失败：{detail}")
                    tmp_partial = out.with_name(out.name + ".part")
                    shutil.move(str(tmp_out), tmp_partial)
                    tmp_partial.replace(out)
            # Keep duration metadata consistent for UI.
            duration = float(ep.length / fps) if fps else 0.0
            if camera_key in ep.cameras:
                ep.cameras[camera_key].to_timestamp = duration
            return out

    def export_subset(
        self,
        output: Path,
        episode_indices: list[int],
        media_mode: str = "hardlink",
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import h5py

        view = self.inspect()
        selected = sorted({int(i) for i in episode_indices})
        output = output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"目标已存在：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if output.suffix.lower() not in {".hdf5", ".h5"}:
            output = output.with_suffix(".hdf5")

        with h5py.File(output, "w") as dst_f:
            dst_data = dst_f.create_group("data")
            copied_env = False
            for new_idx, old_idx in enumerate(selected):
                file_path, demo_key, _ep = self._episode_ref(old_idx)
                with h5py.File(file_path, "r") as src_f:
                    if demo_key:
                        src_group = _group_for(src_f, demo_key)
                    else:
                        src_group = src_f["data"] if "data" in src_f else src_f
                    src_f.copy(src_group, dst_data, name=f"demo_{new_idx}")
                    if not copied_env and "data" in src_f:
                        for attr_key, attr_val in src_f["data"].attrs.items():
                            dst_data.attrs[attr_key] = attr_val
                        copied_env = True
        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "source_dataset": str(self.path),
            "selected_source_episodes": selected,
            "format": FORMAT_HDF5,
        }
        output.with_suffix(output.suffix + ".selection_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        return {"output": str(output), "totalEpisodes": len(selected), "format": FORMAT_HDF5}


def _write_frames_dataset(obs_group, name: str, frames) -> None:
    """Stream frames into a chunked, gzip-compressed HDF5 dataset."""
    dset = None
    count = 0
    for frame in frames:
        frame = np.asarray(frame)
        if dset is None:
            shape = frame.shape
            dset = obs_group.create_dataset(
                name,
                shape=(0, *shape),
                maxshape=(None, *shape),
                dtype=frame.dtype,
                chunks=(1, *shape),
                compression="gzip",
                compression_opts=4,
            )
        dset.resize(count + 1, axis=0)
        dset[count] = frame
        count += 1
    if dset is None:
        raise ValueError(f"相机 {name} 没有可写入的帧")


class Hdf5Writer(DatasetWriter):
    format_id = FORMAT_HDF5

    def write_from_episodes(
        self,
        output: Path,
        episodes: Any,
        meta: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``episodes`` may be any iterable; episodes are written as they arrive."""
        import h5py

        output = output.expanduser().resolve()
        if output.suffix.lower() not in {".hdf5", ".h5"}:
            output = output.with_suffix(".hdf5")
        if output.exists():
            raise FileExistsError(f"目标已存在：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        total_episodes = 0
        total_frames = 0
        try:
            with h5py.File(output, "w") as handle:
                data = handle.create_group("data")
                for idx, ep in enumerate(episodes):
                    group = data.create_group(f"demo_{idx}")
                    action = ep.get("action")
                    state = ep.get("state")
                    length = int(ep.get("length") or 0)
                    if action is not None:
                        action = np.asarray(action, dtype=np.float32)
                        length = length or int(action.shape[0])
                        group.create_dataset("actions", data=action)
                    if state is not None:
                        state = np.asarray(state, dtype=np.float32)
                        length = length or int(state.shape[0])
                        group.create_dataset("states", data=state)
                        obs = group.create_group("obs")
                        obs.create_dataset("states", data=state)
                    images = ep.get("images") or {}
                    if images:
                        obs = group.require_group("obs")
                        for cam, frames in images.items():
                            _write_frames_dataset(obs, cam, iter(frames))
                    for cam, src in (ep.get("video_paths") or {}).items():
                        if cam in images:
                            continue
                        src_path = Path(src)
                        if not src_path.is_file():
                            continue
                        obs = group.require_group("obs")
                        _write_frames_dataset(obs, cam, media.decode_mp4_frames(src_path))
                    total_episodes += 1
                    total_frames += length
                data.attrs["total"] = total_episodes
        except Exception:
            output.unlink(missing_ok=True)
            raise
        return {
            "output": str(output),
            "totalEpisodes": total_episodes,
            "totalFrames": total_frames,
            "format": FORMAT_HDF5,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
