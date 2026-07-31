"""LeRobot v3 adapter wrapping the migrated demo backend."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import lerobot_v3_lib as lib
from . import media
from .base import DatasetAdapter, DatasetWriter
from .view import FORMAT_LEROBOT_V3, CameraRef, DatasetView, EpisodeView


class LeRobotV3Adapter(DatasetAdapter):
    format_id = FORMAT_LEROBOT_V3

    def __init__(self, path: Path):
        super().__init__(path)
        self._info_cache: dict[str, Any] | None = None
        self._episode_meta_cache: dict[int, dict[str, Any]] | None = None

    @classmethod
    def detect(cls, path: Path) -> bool:
        info_path = path / "meta" / "info.json"
        if not info_path.is_file():
            return False
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return str(info.get("codebase_version", "")) in {"v3.0", "v3"}

    def inspect(self) -> DatasetView:
        raw = lib.inspect_dataset(self.path)
        episodes: list[EpisodeView] = []
        for item in raw["episodes"]:
            cameras = {
                key: CameraRef(
                    key=key,
                    kind="video",
                    path=video.get("path"),
                    from_timestamp=video.get("fromTimestamp"),
                    to_timestamp=video.get("toTimestamp"),
                )
                for key, video in (item.get("videos") or {}).items()
            }
            episodes.append(
                EpisodeView(
                    episode_index=int(item["episodeIndex"]),
                    length=int(item["length"]),
                    duration=float(item["duration"]),
                    tasks=list(item.get("tasks") or []),
                    has_intervention=bool(item.get("hasIntervention")),
                    cameras=cameras,
                    extras={"platformEpisodeId": item.get("platformEpisodeId") or ""},
                )
            )
        return DatasetView(
            format_id=FORMAT_LEROBOT_V3,
            path=raw["path"],
            name=raw["name"],
            fps=float(raw.get("fps") or 0),
            robot_type=raw.get("robotType"),
            features=raw.get("features") or {},
            episodes=episodes,
            total_frames=int(raw.get("totalFrames") or 0),
            total_tasks=int(raw.get("totalTasks") or 0),
            extras={"codebase_version": raw.get("codebaseVersion")},
        )

    def _validated_info(self) -> dict[str, Any]:
        if self._info_cache is None:
            self._info_cache = lib.validate_dataset(self.path)
        return self._info_cache

    def _episode_meta(self) -> dict[int, dict[str, Any]]:
        """episode_index → episode meta row (data/chunk_index, data/file_index, …)."""
        if self._episode_meta_cache is None:
            info = self._validated_info()
            table = pq.read_table(info["_episode_files"])
            rows = table.to_pylist()
            self._episode_meta_cache = {int(row["episode_index"]): row for row in rows}
        return self._episode_meta_cache

    def _data_files_for_episode(self, episode_index: int) -> list[Path]:
        """Locate the parquet shard(s) holding one episode via meta indices."""
        info = self._validated_info()
        meta = self._episode_meta().get(episode_index)
        data_path_tpl = str(info.get("data_path") or "")
        if meta is not None and data_path_tpl and "data/chunk_index" in meta and "data/file_index" in meta:
            candidate = Path(info["_root"]) / data_path_tpl.format(
                chunk_index=int(meta["data/chunk_index"]),
                file_index=int(meta["data/file_index"]),
            )
            if candidate.is_file():
                return [candidate]
        return list(info["_data_files"])

    @staticmethod
    def _wanted_column(column: str, keys: list[str] | None) -> bool:
        if column in {"episode_index", "index", "frame_index", "timestamp", "task_index"}:
            return False
        if keys is None:
            return column in {"action", "observation.state"} or column.startswith(
                ("action", "observation.state")
            )
        return column in keys or any(column.startswith(k) for k in keys)

    def get_timeseries(self, episode_index: int, keys: list[str] | None = None) -> dict[str, np.ndarray]:
        import pyarrow.compute as pc

        from . import tabular

        result: dict[str, np.ndarray] = {}
        for data_file in self._data_files_for_episode(episode_index):
            parquet = pq.ParquetFile(data_file)
            schema_names = parquet.schema_arrow.names
            if "episode_index" not in schema_names:
                continue
            columns = [name for name in schema_names if self._wanted_column(name, keys)]
            if not columns:
                continue
            table = pq.read_table(data_file, columns=["episode_index", *columns])
            mask = pc.equal(table["episode_index"], episode_index)
            if not pc.any(mask).as_py():
                continue
            table = table.filter(mask)
            for column in columns:
                try:
                    values = tabular.column_to_ndarray(table[column])
                except (TypeError, ValueError, pa.ArrowInvalid):
                    continue
                if column in result:
                    result[column] = np.concatenate([result[column], values], axis=0)
                else:
                    result[column] = values
        return result

    def export_subset(
        self,
        output: Path,
        episode_indices: list[int],
        media_mode: str = "hardlink",
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        import os
        import tempfile

        with tempfile.NamedTemporaryFile("w", suffix=".json", encoding="utf-8", delete=False) as handle:
            json.dump({"episodes": episode_indices}, handle)
            selection = Path(handle.name)
        try:
            return lib.create_dataset(self.path, output, selection, media_mode)
        finally:
            selection.unlink(missing_ok=True)


class LeRobotV3Writer(DatasetWriter):
    format_id = FORMAT_LEROBOT_V3

    def write_from_episodes(
        self,
        output: Path,
        episodes: Any,
        meta: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Write a minimal LeRobot v3 dataset from normalized episode payloads.

        ``episodes`` may be any iterable (including a generator); data is
        written incrementally so peak memory stays at one episode.
        """
        import shutil
        import tempfile
        from datetime import datetime, timezone

        from . import stats as stats_mod
        from . import tabular

        output = output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"目标目录已经存在：{output}")
        mapping = mapping or {}
        fps = float(meta.get("fps") or mapping.get("fps") or 30.0)
        media_mode = str(mapping.get("media_mode") or "hardlink")
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
        data_writer: pq.ParquetWriter | None = None
        try:
            episode_meta_rows: list[dict[str, Any]] = []
            global_index = 0
            video_keys: set[str] = set()
            video_shapes: dict[str, list[int]] = {}
            state_dim: int | None = None
            action_dim: int | None = None
            all_tasks: set[str] = set()
            collector = stats_mod.StatsCollector()
            data_path = temporary / "data" / "chunk-000" / "file-000.parquet"
            data_path.parent.mkdir(parents=True, exist_ok=True)
            data_schema = None
            for new_index, ep in enumerate(episodes):
                length = int(ep.get("length") or 0)
                state = ep.get("state")
                action = ep.get("action")
                if state is not None:
                    state = np.asarray(state)
                    length = length or int(state.shape[0])
                if action is not None:
                    action = np.asarray(action)
                    length = length or int(action.shape[0])
                if length <= 0:
                    continue
                table = tabular.episode_frame_table(
                    episode_index=new_index,
                    length=length,
                    fps=fps,
                    base_index=global_index,
                    state=state,
                    action=action,
                )
                if data_writer is None:
                    data_schema = table.schema
                    data_writer = pq.ParquetWriter(data_path, data_schema, compression="zstd")
                else:
                    table = tabular.align_to_schema(table, data_schema)
                data_writer.write_table(table)
                if state is not None:
                    state_dim = state_dim or int(np.asarray(state).reshape(length, -1).shape[-1])
                    collector.update("observation.state", np.asarray(state)[:length])
                if action is not None:
                    action_dim = action_dim or int(np.asarray(action).reshape(length, -1).shape[-1])
                    collector.update("action", np.asarray(action)[:length])
                all_tasks.update(ep.get("tasks") or [])
                global_index += length
                episode_meta_rows.append(
                    {
                        "episode_index": new_index,
                        "length": length,
                        "dataset_from_index": global_index - length,
                        "dataset_to_index": global_index,
                        "tasks": list(ep.get("tasks") or []),
                        "data/chunk_index": 0,
                        "data/file_index": 0,
                    }
                )
                # Preserve camera streams: external MP4s are linked/copied per
                # episode (one shard per episode so files never collide) and
                # in-memory frames are encoded to MP4.
                ep_chunk = new_index // 1000
                ep_file = new_index % 1000
                images = ep.get("images") or {}
                for cam in sorted(set(ep.get("video_paths") or {}) | set(images)):
                    video_keys.add(cam)
                    rel = f"videos/{cam}/chunk-{ep_chunk:03d}/file-{ep_file:03d}.mp4"
                    dest = temporary / rel
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    src = (ep.get("video_paths") or {}).get(cam)
                    if src is not None:
                        src_path = Path(src)
                        if not src_path.is_file():
                            continue
                        if media_mode == "copy":
                            shutil.copy2(src_path, dest)
                        else:
                            try:
                                os.link(src_path, dest)
                            except OSError:
                                shutil.copy2(src_path, dest)
                    else:
                        media.encode_frames_to_mp4(images[cam], dest, fps=fps)
                    if cam not in video_shapes:
                        try:
                            width, height = media.probe_mp4_meta(dest)["size"]
                            video_shapes[cam] = [int(height), int(width), 3]
                        except Exception:  # noqa: BLE001
                            pass
                    episode_meta_rows[-1][f"videos/{cam}/chunk_index"] = ep_chunk
                    episode_meta_rows[-1][f"videos/{cam}/file_index"] = ep_file
                    episode_meta_rows[-1][f"videos/{cam}/from_timestamp"] = 0.0
                    episode_meta_rows[-1][f"videos/{cam}/to_timestamp"] = length / fps

            if data_writer is None:
                raise ValueError("没有可写入的帧")
            data_writer.close()
            data_writer = None

            ep_dir = temporary / "meta" / "episodes" / "chunk-000"
            ep_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(episode_meta_rows), ep_dir / "file-000.parquet", compression="zstd")

            features: dict[str, Any] = {}
            if state_dim is not None:
                features["observation.state"] = {"dtype": "float32", "shape": [state_dim], "names": None}
            if action_dim is not None:
                features["action"] = {"dtype": "float32", "shape": [action_dim], "names": None}
            for cam in sorted(video_keys):
                features[cam] = {
                    "dtype": "video",
                    "shape": video_shapes.get(cam) or [240, 320, 3],
                    "names": ["height", "width", "channels"],
                }

            info = {
                "codebase_version": "v3.0",
                "robot_type": meta.get("robot_type") or mapping.get("robot_type"),
                "fps": fps,
                "total_episodes": len(episode_meta_rows),
                "total_frames": global_index,
                "total_tasks": len(all_tasks),
                "splits": {"train": f"0:{len(episode_meta_rows)}"},
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": features,
                "chunks_size": 1000,
            }
            meta_dir = temporary / "meta"
            meta_dir.mkdir(parents=True, exist_ok=True)
            (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            (meta_dir / "stats.json").write_text(
                json.dumps(collector.to_stats_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            tasks = sorted(all_tasks)
            with (meta_dir / "tasks.jsonl").open("w", encoding="utf-8") as handle:
                for idx, task in enumerate(tasks):
                    handle.write(json.dumps({"task_index": idx, "task": task}, ensure_ascii=False) + "\n")

            os.replace(temporary, output)
            return {
                "output": str(output),
                "totalEpisodes": len(episode_meta_rows),
                "totalFrames": global_index,
                "format": FORMAT_LEROBOT_V3,
                "createdAt": datetime.now(timezone.utc).isoformat(),
            }
        except Exception:
            if data_writer is not None:
                try:
                    data_writer.close()
                except Exception:  # noqa: BLE001
                    pass
            shutil.rmtree(temporary, ignore_errors=True)
            raise
