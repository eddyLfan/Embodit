"""MCAP dataset adapter (topic-based logs, single-file or multi-file directories)."""

from __future__ import annotations

import hashlib
import json
import shutil
import struct
import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from .base import DatasetAdapter, DatasetWriter
from .detect import collect_mcap_files
from .view import FORMAT_MCAP, CameraRef, DatasetView, EpisodeView

_video_locks: dict[str, threading.Lock] = {}
_video_locks_guard = threading.Lock()


def _require_mcap():
    try:
        from mcap.reader import make_reader
    except ImportError as error:
        raise ImportError("读取 MCAP 需要安装 mcap：uv/pip install mcap") from error
    return make_reader


def _ffmpeg_exe() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception as error:  # noqa: BLE001
        raise ImportError("MCAP 视频预览需要 imageio-ffmpeg：uv/pip install imageio-ffmpeg") from error


def _is_image_topic(topic: str, schema_name: str | None = None) -> bool:
    topic_l = topic.lower()
    name = (schema_name or "").lower()
    if "camera_info" in topic_l or "calibration" in name:
        return False
    if "compressedimage" in name:
        return True
    if name.endswith(".image") or name.endswith("rawimage"):
        return True
    return "compressed" in topic_l and ("camera" in topic_l or "image" in topic_l or "rgb" in topic_l)


def _is_joint_topic(topic: str, schema_name: str | None = None) -> bool:
    topic_l = topic.lower()
    name = (schema_name or "").lower()
    if "pose" in topic_l or "poseinframe" in name:
        return True
    return any(token in topic_l for token in ("joint", "state", "action", "qpos", "proprio")) or any(
        token in name for token in ("jointstate", "float64multiarray")
    )


def _split_episodes(timestamps_ns: list[int], gap_s: float = 2.0) -> list[tuple[int, int]]:
    """Return inclusive index ranges for episodes based on time gaps."""
    if not timestamps_ns:
        return []
    ranges: list[tuple[int, int]] = []
    start = 0
    gap_ns = int(gap_s * 1e9)
    for i in range(1, len(timestamps_ns)):
        if timestamps_ns[i] - timestamps_ns[i - 1] > gap_ns:
            ranges.append((start, i - 1))
            start = i
    ranges.append((start, len(timestamps_ns) - 1))
    return ranges


def _parse_protobuf_fields(data: bytes) -> list[tuple[int, int, Any]]:
    """Minimal protobuf wire parser → (field_num, wire_type, value)."""
    i = 0
    n = len(data)
    out: list[tuple[int, int, Any]] = []
    while i < n:
        key = 0
        shift = 0
        while True:
            if i >= n:
                return out
            byte = data[i]
            i += 1
            key |= (byte & 0x7F) << shift
            if not (byte & 0x80):
                break
            shift += 7
            if shift > 63:
                return out
        field_num = key >> 3
        wire = key & 7
        if wire == 0:
            val = 0
            shift = 0
            while True:
                if i >= n:
                    return out
                byte = data[i]
                i += 1
                val |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            out.append((field_num, wire, val))
        elif wire == 1:
            if i + 8 > n:
                return out
            out.append((field_num, wire, data[i : i + 8]))
            i += 8
        elif wire == 2:
            length = 0
            shift = 0
            while True:
                if i >= n:
                    return out
                byte = data[i]
                i += 1
                length |= (byte & 0x7F) << shift
                if not (byte & 0x80):
                    break
                shift += 7
            if i + length > n:
                return out
            out.append((field_num, wire, data[i : i + length]))
            i += length
        elif wire == 5:
            if i + 4 > n:
                return out
            out.append((field_num, wire, data[i : i + 4]))
            i += 4
        else:
            break
    return out


def _bytes_field_map(data: bytes) -> dict[int, bytes]:
    return {num: val for num, wire, val in _parse_protobuf_fields(data) if wire == 2 and isinstance(val, (bytes, bytearray))}


def _doubles_in(data: bytes) -> list[float]:
    values: list[float] = []
    for _num, wire, val in _parse_protobuf_fields(data):
        if wire == 1 and isinstance(val, (bytes, bytearray)) and len(val) == 8:
            values.append(struct.unpack("<d", val)[0])
        elif wire == 2 and isinstance(val, (bytes, bytearray)):
            values.extend(_doubles_in(val))
    return values


def _decode_pose_in_frame(data: bytes) -> list[float] | None:
    """Extract [x, y, z, qx, qy, qz, qw] from foxglove.PoseInFrame-like payload."""
    fields = _bytes_field_map(data)
    pose = fields.get(3) or fields.get(1)
    if not pose:
        return None
    nested = _bytes_field_map(pose)
    position = nested.get(1)
    orientation = nested.get(2)
    if not position:
        # Maybe the pose bytes themselves hold doubles
        doubles = _doubles_in(pose)
        if len(doubles) >= 3:
            while len(doubles) < 7:
                doubles.append(0.0)
            return doubles[:7]
        return None
    xyz = _doubles_in(position)
    quat = _doubles_in(orientation) if orientation else []
    if len(xyz) < 3:
        return None
    values = xyz[:3] + (quat[:4] if len(quat) >= 4 else [0.0, 0.0, 0.0, 1.0])
    return values


def _decode_compressed_image(data: bytes) -> tuple[bytes, str] | None:
    """Return (payload, format) for foxglove.CompressedImage."""
    fields = _bytes_field_map(data)
    payload = fields.get(2) or fields.get(1)
    if not payload:
        return None
    fmt_raw = fields.get(3) or fields.get(4) or b""
    fmt = fmt_raw.decode("utf-8", errors="ignore").lower().strip() if isinstance(fmt_raw, (bytes, bytearray)) else ""
    if not fmt:
        if payload[:2] == b"\xff\xd8":
            fmt = "jpeg"
        elif payload[:8] == b"\x00\x00\x00\x01" or payload[:4] == b"\x00\x00\x01":
            fmt = "h264"
        else:
            fmt = "bin"
    return bytes(payload), fmt


def _topic_key(topic: str) -> str:
    return topic.strip("/").replace("/", "_")


def _infer_task_name(path: Path) -> str:
    if path.is_file():
        path = path.parent
    # Prefer non-numeric directory names (skip shard folders like 00001).
    for candidate in (path, path.parent):
        name = candidate.name
        if name and not name.isdigit():
            return name
    return path.name


def _episode_window(ep: EpisodeView) -> tuple[int | None, int | None]:
    """Return the (start_ns, end_ns) filter window for an episode.

    ``None`` means unbounded. Using explicit ``None`` (instead of treating 0 as
    falsy) matters: gap-split episodes can legitimately start at log time 0 and
    would otherwise read the whole file.
    """
    start = ep.extras.get("startNs")
    end = ep.extras.get("endNs")
    if start is None or end is None:
        return None, None
    start_ns = int(start)
    end_ns = int(end)
    if end_ns < start_ns or (start_ns == 0 and end_ns == 0):
        return None, None
    return start_ns, end_ns


def _outside_window(log_time: int, start_ns: int | None, end_ns: int | None) -> bool:
    if start_ns is None or end_ns is None:
        return False
    return log_time < start_ns or log_time > end_ns


def _quick_file_meta(file_path: Path) -> dict[str, Any]:
    make_reader = _require_mcap()
    with file_path.open("rb") as handle:
        reader = make_reader(handle)
        summary = reader.get_summary()
    if not summary:
        return {"topics": {}, "start_ns": 0, "end_ns": 0, "message_count": 0}
    schemas = summary.schemas or {}
    channels = summary.channels or {}
    stats = summary.statistics
    topics: dict[str, dict[str, Any]] = {}
    for channel in channels.values():
        schema = schemas.get(channel.schema_id)
        count = 0
        if stats and stats.channel_message_counts:
            count = int(stats.channel_message_counts.get(channel.id, 0))
        topics[channel.topic] = {
            "count": count,
            "schema": schema.name if schema else None,
            "encoding": channel.message_encoding,
        }
    start_ns = int(stats.message_start_time) if stats and stats.message_start_time else 0
    end_ns = int(stats.message_end_time) if stats and stats.message_end_time else start_ns
    message_count = int(stats.message_count) if stats and stats.message_count else sum(t["count"] for t in topics.values())
    return {
        "topics": topics,
        "start_ns": start_ns,
        "end_ns": end_ns,
        "message_count": message_count,
    }


class McapAdapter(DatasetAdapter):
    format_id = FORMAT_MCAP

    def __init__(self, path: Path):
        super().__init__(path)
        self._view_cache: DatasetView | None = None
        self._episode_files_cache: list[Path] | None = None

    @classmethod
    def detect(cls, path: Path) -> bool:
        return bool(collect_mcap_files(path))

    def _episode_files(self) -> list[Path]:
        if self._episode_files_cache is None:
            files = collect_mcap_files(self.path)
            if not files:
                raise ValueError(f"未找到 MCAP 文件：{self.path}")
            self._episode_files_cache = files
        return self._episode_files_cache

    def _file(self) -> Path:
        files = self._episode_files()
        if len(files) != 1:
            raise ValueError(f"单文件接口需要恰好一个 MCAP：{self.path}")
        return files[0]

    def _is_multifile(self) -> bool:
        return len(self._episode_files()) > 1

    def _summary_scan(self, file_path: Path) -> dict[str, Any]:
        """Topic taxonomy + anchor-topic timestamps for the gap-split mode.

        Prefers the MCAP summary section (no message scan) for topic counts and
        only iterates messages of the single anchor topic, instead of caching
        every timestamp of every topic in memory.
        """
        make_reader = _require_mcap()
        meta = _quick_file_meta(file_path)
        topics: dict[str, dict[str, Any]] = {
            topic: {"count": int(info.get("count") or 0), "schema": info.get("schema"), "times": []}
            for topic, info in meta["topics"].items()
        }
        if not topics or not any(info["count"] for info in topics.values()):
            # No summary statistics: fall back to one full scan for counts.
            topics = {}
            with file_path.open("rb") as handle:
                reader = make_reader(handle)
                for schema, channel, message in reader.iter_messages():
                    info = topics.setdefault(
                        channel.topic,
                        {"count": 0, "schema": schema.name if schema else None, "times": []},
                    )
                    info["count"] += 1

        image_topics = [t for t, info in topics.items() if _is_image_topic(t, info.get("schema"))]
        joint_topics = [t for t, info in topics.items() if _is_joint_topic(t, info.get("schema"))]
        anchor = None
        if joint_topics:
            anchor = max(joint_topics, key=lambda t: topics[t]["count"])
        elif image_topics:
            anchor = max(image_topics, key=lambda t: topics[t]["count"])
        elif topics:
            anchor = max(topics.keys(), key=lambda t: topics[t]["count"])

        anchor_times: list[int] = []
        if anchor:
            with file_path.open("rb") as handle:
                reader = make_reader(handle)
                for _schema, channel, message in reader.iter_messages(topics=[anchor]):
                    if channel.topic == anchor:
                        anchor_times.append(message.log_time)
            topics[anchor]["times"] = sorted(anchor_times)

        start_ns = int(meta.get("start_ns") or 0)
        end_ns = int(meta.get("end_ns") or 0)
        message_count = int(meta.get("message_count") or 0) or sum(int(i["count"]) for i in topics.values())
        return {
            "topics": topics,
            "anchor": anchor,
            "startNs": start_ns,
            "endNs": end_ns,
            "messageCount": message_count,
        }

    def inspect(self) -> DatasetView:
        if self._view_cache is None:
            if self._is_multifile():
                self._view_cache = self._inspect_multifile()
            else:
                self._view_cache = self._inspect_single(self._episode_files()[0])
        return self._view_cache

    def _inspect_multifile(self) -> DatasetView:
        files = self._episode_files()
        task = _infer_task_name(self.path)
        # Probe first file for topic taxonomy / cameras
        probe = _quick_file_meta(files[0])
        image_topics = [
            topic for topic, info in probe["topics"].items() if _is_image_topic(topic, info.get("schema"))
        ]
        joint_topics = [
            topic for topic, info in probe["topics"].items() if _is_joint_topic(topic, info.get("schema"))
        ]
        features = {
            topic: {"dtype": "topic", "schema": info.get("schema"), "count": info["count"]}
            for topic, info in probe["topics"].items()
        }

        episodes: list[EpisodeView] = []
        total_frames = 0
        fps_samples: list[float] = []
        for idx, file_path in enumerate(files):
            meta = _quick_file_meta(file_path) if idx > 0 else probe
            start_ns = int(meta["start_ns"] or 0)
            end_ns = int(meta["end_ns"] or start_ns)
            duration = max(0.0, (end_ns - start_ns) / 1e9) if end_ns > start_ns else 0.0
            # Prefer densest image topic count as length proxy
            length = 0
            for topic in image_topics:
                length = max(length, int(meta["topics"].get(topic, {}).get("count") or 0))
            if length <= 0:
                length = int(meta.get("message_count") or 0)
            if duration > 0 and length > 1:
                fps_samples.append((length - 1) / duration)
            cameras = {
                _topic_key(topic): CameraRef(
                    key=_topic_key(topic),
                    kind="topic",
                    topic=topic,
                    from_timestamp=0.0,
                    to_timestamp=duration,
                )
                for topic in image_topics
            }
            episodes.append(
                EpisodeView(
                    episode_index=idx,
                    length=length,
                    duration=duration,
                    tasks=[task],
                    cameras=cameras,
                    extras={
                        "startNs": start_ns,
                        "endNs": end_ns,
                        "mcapFile": str(file_path),
                        "mcapName": file_path.name,
                        "prompt": task,
                    },
                )
            )
            total_frames += length

        fps = float(np.mean(fps_samples)) if fps_samples else 0.0
        root = self.path if self.path.is_dir() else self.path.parent
        return DatasetView(
            format_id=FORMAT_MCAP,
            path=str(root),
            name=root.name,
            fps=fps,
            robot_type=None,
            features=features,
            episodes=episodes,
            total_frames=total_frames,
            total_tasks=1,
            extras={
                "imageTopics": image_topics,
                "jointTopics": joint_topics,
                "layout": "multifile",
                "prompt": task,
            },
        )

    def _inspect_single(self, file_path: Path) -> DatasetView:
        from settings import MCAP_GAP_S

        mapping_gap = MCAP_GAP_S
        summary = self._summary_scan(file_path)
        topics = summary["topics"]
        image_topics = [topic for topic, info in topics.items() if _is_image_topic(topic, info.get("schema"))]
        joint_topics = [topic for topic, info in topics.items() if _is_joint_topic(topic, info.get("schema"))]
        task = _infer_task_name(file_path)
        anchor = summary.get("anchor")

        if anchor and topics[anchor]["times"]:
            anchor_times = sorted(topics[anchor]["times"])
            ranges = _split_episodes(anchor_times, gap_s=mapping_gap)
            fps = 0.0
            if len(anchor_times) > 1:
                duration_s = (anchor_times[-1] - anchor_times[0]) / 1e9
                fps = (len(anchor_times) - 1) / duration_s if duration_s > 0 else 0.0
            episodes: list[EpisodeView] = []
            for idx, (start_i, end_i) in enumerate(ranges):
                length = end_i - start_i + 1
                start_t = anchor_times[start_i]
                end_t = anchor_times[end_i]
                duration = max(0.0, (end_t - start_t) / 1e9)
                cameras = {
                    _topic_key(topic): CameraRef(
                        key=_topic_key(topic),
                        kind="topic",
                        topic=topic,
                        from_timestamp=0.0,
                        to_timestamp=duration,
                    )
                    for topic in image_topics
                }
                episodes.append(
                    EpisodeView(
                        episode_index=idx,
                        length=length,
                        duration=duration,
                        tasks=[task],
                        cameras=cameras,
                        extras={
                            "startNs": start_t,
                            "endNs": end_t,
                            "anchorTopic": anchor,
                            "mcapFile": str(file_path),
                            "prompt": task,
                        },
                    )
                )
        else:
            fps = 0.0
            start_ns = int(summary.get("startNs") or 0)
            end_ns = int(summary.get("endNs") or 0)
            episodes = [
                EpisodeView(
                    episode_index=0,
                    length=int(summary.get("messageCount") or 0),
                    duration=max(0.0, (end_ns - start_ns) / 1e9) if end_ns > start_ns else 0.0,
                    tasks=[task],
                    cameras={},
                    extras={"mcapFile": str(file_path), "prompt": task},
                )
            ]

        features = {
            topic: {"dtype": "topic", "schema": info.get("schema"), "count": info["count"]}
            for topic, info in topics.items()
        }
        return DatasetView(
            format_id=FORMAT_MCAP,
            path=str(file_path),
            name=file_path.name,
            fps=float(fps),
            robot_type=None,
            features=features,
            episodes=episodes,
            total_frames=sum(ep.length for ep in episodes),
            total_tasks=1,
            extras={
                "imageTopics": image_topics,
                "jointTopics": joint_topics,
                "anchorTopic": anchor,
                "layout": "single",
                "prompt": task,
            },
        )

    def _episode_mcap(self, episode_index: int) -> tuple[Path, EpisodeView]:
        view = self.inspect()
        if episode_index < 0 or episode_index >= len(view.episodes):
            raise IndexError(episode_index)
        ep = view.episodes[episode_index]
        file_path = Path(ep.extras.get("mcapFile") or self._episode_files()[episode_index if self._is_multifile() else 0])
        return file_path, ep

    def get_timeseries(self, episode_index: int, keys: list[str] | None = None) -> dict[str, np.ndarray]:
        """Decode numeric / pose payloads for trajectory visualization."""
        make_reader = _require_mcap()
        file_path, ep = self._episode_mcap(episode_index)
        start_ns, end_ns = _episode_window(ep)
        view = self.inspect()
        joint_topics = list(view.extras.get("jointTopics") or [])
        if not joint_topics:
            # Fallback: discover pose-like topics from this file
            meta = _quick_file_meta(file_path)
            joint_topics = [topic for topic, info in meta["topics"].items() if _is_joint_topic(topic, info.get("schema"))]

        pose_series: dict[str, list[list[float]]] = {}
        float_series: dict[str, list[list[float]]] = {}
        with file_path.open("rb") as handle:
            reader = make_reader(handle)
            for schema, channel, message in reader.iter_messages(topics=joint_topics or None):
                if joint_topics and channel.topic not in joint_topics:
                    continue
                if _outside_window(message.log_time, start_ns, end_ns):
                    continue
                schema_name = schema.name if schema else ""
                if "pose" in channel.topic.lower() or "poseinframe" in schema_name.lower():
                    pose = _decode_pose_in_frame(message.data)
                    if pose is not None:
                        pose_series.setdefault(channel.topic, []).append(pose)
                    continue
                values = _try_decode_floats(message.data)
                if values is not None:
                    float_series.setdefault(channel.topic, []).append(values)

        result: dict[str, np.ndarray] = {}
        # Prefer concatenating dual-arm eef poses into `action`
        if pose_series:
            ordered = sorted(pose_series.keys())
            rows = []
            # Align by min length
            length = min(len(pose_series[t]) for t in ordered)
            for i in range(length):
                row: list[float] = []
                for topic in ordered:
                    row.extend(pose_series[topic][i])
                rows.append(row)
            if rows:
                result["action"] = np.asarray(rows, dtype=np.float64)
                # Also expose first two topics as named XY helpers
                for topic in ordered[:2]:
                    arr = np.asarray(pose_series[topic][:length], dtype=np.float64)
                    key = "eef." + _topic_key(topic)
                    result[key] = arr
        for topic, rows in float_series.items():
            key = "action" if "action" in topic.lower() else "observation.state"
            if key in result:
                key = topic
            if keys is not None and key not in keys and topic not in keys:
                continue
            try:
                result[key] = np.asarray(rows, dtype=np.float64)
            except ValueError:
                continue
        if keys is not None:
            result = {k: v for k, v in result.items() if k in keys or any(k.startswith(prefix) for prefix in ("eef.",))}
        return result

    def materialize_topic_video(self, episode_index: int, topic: str) -> Path:
        """Extract CompressedImage payloads and remux to a cached MP4 for browser playback."""
        file_path, ep = self._episode_mcap(episode_index)
        start_ns, end_ns = _episode_window(ep)
        stamp = f"{file_path.stat().st_mtime_ns}:{file_path.stat().st_size}:{episode_index}:{topic}:{start_ns}:{end_ns}"
        digest = hashlib.sha1(stamp.encode("utf-8")).hexdigest()[:16]
        cache_dir = Path(tempfile.gettempdir()) / "embody-mcap-video"
        cache_dir.mkdir(parents=True, exist_ok=True)
        out = cache_dir / f"{digest}.mp4"
        if out.is_file() and out.stat().st_size > 0:
            return out

        with _video_locks_guard:
            lock = _video_locks.setdefault(digest, threading.Lock())
        with lock:
            if out.is_file() and out.stat().st_size > 0:
                return out
            make_reader = _require_mcap()
            fmt = "h264"
            frame_count = 0
            ffmpeg = _ffmpeg_exe()
            with tempfile.TemporaryDirectory(prefix="embody-mcap-") as tmp:
                tmp_path = Path(tmp)
                raw_path = tmp_path / "stream.bin"
                # Stream payloads straight to disk instead of accumulating
                # the whole video in a Python list.
                with file_path.open("rb") as handle, raw_path.open("wb") as raw_out:
                    reader = make_reader(handle)
                    for _schema, channel, message in reader.iter_messages(topics=[topic]):
                        if channel.topic != topic:
                            continue
                        if _outside_window(message.log_time, start_ns, end_ns):
                            continue
                        decoded = _decode_compressed_image(message.data)
                        if not decoded:
                            continue
                        payload, detected = decoded
                        fmt = detected or fmt
                        raw_out.write(payload)
                        frame_count += 1
                if frame_count == 0:
                    raise ValueError(f"话题无图像帧：{topic}")
                tmp_out = tmp_path / "out.mp4"
                if fmt in {"h264", "avc", "video/h264"}:
                    cmd = [
                        ffmpeg,
                        "-y",
                        "-fflags",
                        "+genpts",
                        "-f",
                        "h264",
                        "-i",
                        str(raw_path),
                        "-c",
                        "copy",
                        "-an",
                        "-movflags",
                        "+faststart",
                        str(tmp_out),
                    ]
                elif fmt in {"jpeg", "jpg", "png"}:
                    # Image sequence concat via image2pipe is awkward for mixed sizes;
                    # remux as mjpeg.
                    cmd = [
                        ffmpeg,
                        "-y",
                        "-f",
                        "image2pipe",
                        "-vcodec",
                        "mjpeg" if fmt in {"jpeg", "jpg"} else "png",
                        "-i",
                        str(raw_path),
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
                else:
                    raise ValueError(f"暂不支持的图像编码：{fmt}")
                result = subprocess.run(cmd, capture_output=True, check=False)
                if result.returncode != 0 or not tmp_out.is_file():
                    detail = (result.stderr or b"").decode("utf-8", errors="ignore")[-400:]
                    raise RuntimeError(f"MCAP 视频转码失败：{detail}")
                tmp_partial = out.with_name(out.name + ".part")
                shutil.move(str(tmp_out), tmp_partial)
                tmp_partial.replace(out)
            return out

    def export_subset(
        self,
        output: Path,
        episode_indices: list[int],
        media_mode: str = "hardlink",
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Export selected episodes into a new MCAP file (or copy multifile subset)."""
        try:
            from mcap.reader import make_reader
            from mcap.writer import Writer
        except ImportError as error:
            raise ImportError("导出 MCAP 需要安装 mcap") from error

        view = self.inspect()
        selected = sorted({int(i) for i in episode_indices})
        output = output.expanduser().resolve()
        if output.suffix.lower() != ".mcap":
            output = output.with_suffix(".mcap")
        if output.exists():
            raise FileExistsError(f"目标已存在：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)

        with output.open("wb") as dst:
            writer = Writer(dst)
            writer.start()
            schema_ids: dict[tuple[str, bytes], int] = {}
            channel_ids: dict[str, int] = {}
            for idx in selected:
                file_path, ep = self._episode_mcap(idx)
                start_ns, end_ns = _episode_window(ep)
                with file_path.open("rb") as src:
                    reader = make_reader(src)
                    for schema, channel, message in reader.iter_messages():
                        if _outside_window(message.log_time, start_ns, end_ns):
                            continue
                        schema_key = (schema.name if schema else "", schema.data if schema else b"")
                        if schema and schema_key not in schema_ids:
                            schema_ids[schema_key] = writer.register_schema(
                                name=schema.name,
                                encoding=schema.encoding,
                                data=schema.data,
                            )
                        if channel.topic not in channel_ids:
                            channel_ids[channel.topic] = writer.register_channel(
                                topic=channel.topic,
                                message_encoding=channel.message_encoding,
                                schema_id=schema_ids.get(schema_key, 0) if schema else 0,
                                metadata=dict(channel.metadata or {}),
                            )
                        writer.add_message(
                            channel_id=channel_ids[channel.topic],
                            log_time=message.log_time,
                            data=message.data,
                            publish_time=message.publish_time,
                        )
            writer.finish()
        return {
            "output": str(output),
            "totalEpisodes": len(selected),
            "format": FORMAT_MCAP,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }


def _try_decode_floats(data: bytes) -> list[float] | None:
    if len(data) < 4:
        return None
    # ROS2 CDR often has 4-byte encapsulation header
    for offset in (0, 4):
        payload = data[offset:]
        if len(payload) % 8 == 0 and len(payload) >= 8:
            count = len(payload) // 8
            try:
                values = list(struct.unpack("<" + "d" * count, payload))
                if all(np.isfinite(v) for v in values[: min(32, count)]):
                    return values
            except struct.error:
                pass
        if len(payload) % 4 == 0 and len(payload) >= 4:
            count = len(payload) // 4
            try:
                values = list(struct.unpack("<" + "f" * count, payload))
                if all(np.isfinite(v) for v in values[: min(32, count)]):
                    return [float(v) for v in values]
            except struct.error:
                pass
    return None


class McapWriter(DatasetWriter):
    format_id = FORMAT_MCAP

    def write_from_episodes(
        self,
        output: Path,
        episodes: Any,
        meta: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``episodes`` may be any iterable; messages stream directly to disk."""
        try:
            from mcap.writer import Writer
        except ImportError as error:
            raise ImportError("写入 MCAP 需要安装 mcap") from error

        output = output.expanduser().resolve()
        if output.suffix.lower() != ".mcap":
            output = output.with_suffix(".mcap")
        if output.exists():
            raise FileExistsError(f"目标已存在：{output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        mapping = mapping or {}
        state_topic = mapping.get("state_topic", "/observation/state")
        action_topic = mapping.get("action_topic", "/action")
        fps = float(meta.get("fps") or mapping.get("fps") or 30.0)
        gap_ns = int(3.0 * 1e9)

        with output.open("wb") as handle:
            writer = Writer(handle)
            writer.start()
            schema_id = writer.register_schema(
                name="float64_array",
                encoding="",
                data=b"",
            )
            state_ch = writer.register_channel(
                topic=state_topic,
                message_encoding="raw",
                schema_id=schema_id,
            )
            action_ch = writer.register_channel(
                topic=action_topic,
                message_encoding="raw",
                schema_id=schema_id,
            )
            cursor_ns = 0
            total_episodes = 0
            total_frames = 0
            for ep in episodes:
                state = ep.get("state")
                action = ep.get("action")
                length = int(ep.get("length") or 0)
                if state is not None:
                    state = np.asarray(state, dtype=np.float64)
                    length = length or int(state.shape[0])
                if action is not None:
                    action = np.asarray(action, dtype=np.float64)
                    length = length or int(action.shape[0])
                dt_ns = int(1e9 / fps) if fps > 0 else 33_000_000
                for i in range(length):
                    t = cursor_ns + i * dt_ns
                    if state is not None:
                        writer.add_message(
                            channel_id=state_ch,
                            log_time=t,
                            data=np.asarray(state[i], dtype=np.float64).tobytes(),
                            publish_time=t,
                        )
                    if action is not None:
                        writer.add_message(
                            channel_id=action_ch,
                            log_time=t,
                            data=np.asarray(action[i], dtype=np.float64).tobytes(),
                            publish_time=t,
                        )
                cursor_ns += length * dt_ns + gap_ns
                total_episodes += 1
                total_frames += length
            writer.finish()
        return {
            "output": str(output),
            "totalEpisodes": total_episodes,
            "totalFrames": total_frames,
            "format": FORMAT_MCAP,
            "createdAt": datetime.now(timezone.utc).isoformat(),
            "tasks": meta.get("tasks"),
            "notes": json.dumps({"prompt": meta.get("prompt")}, ensure_ascii=False) if meta.get("prompt") else None,
        }
