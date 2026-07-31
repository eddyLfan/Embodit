"""LeRobot v2.1 dataset adapter."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq

from . import media
from .base import DatasetAdapter, DatasetWriter
from .view import FORMAT_LEROBOT_V21, CameraRef, DatasetView, EpisodeView


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            rows.append(json.loads(line))
    return rows


def _load_info(root: Path) -> dict[str, Any]:
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        raise ValueError(f"不是 LeRobot 数据集，缺少：{info_path}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    version = str(info.get("codebase_version", ""))
    if version not in {"v2.1", "v2.0"}:
        raise ValueError(f"期望 LeRobot v2.1，检测到：{version or 'unknown'}")
    return info


class LeRobotV21Adapter(DatasetAdapter):
    format_id = FORMAT_LEROBOT_V21

    def __init__(self, path: Path):
        super().__init__(path)
        self._parquet_index: dict[int, Path] | None = None
        self._video_index: dict[tuple[str, int], Path] | None = None

    def _build_indices(self) -> None:
        """One directory walk instead of per-episode/per-camera globbing."""
        parquet_index: dict[int, Path] = {}
        for path in self.path.glob("data/chunk-*/episode_*.parquet"):
            try:
                parquet_index[int(path.stem.split("_")[-1])] = path
            except ValueError:
                continue
        video_index: dict[tuple[str, int], Path] = {}
        for path in self.path.glob("videos/chunk-*/*/episode_*.mp4"):
            key = path.parent.name
            try:
                idx = int(path.stem.split("_")[-1])
            except ValueError:
                continue
            video_index[(key, idx)] = path
        self._parquet_index = parquet_index
        self._video_index = video_index

    def _episode_parquet_index(self) -> dict[int, Path]:
        if self._parquet_index is None:
            self._build_indices()
        return self._parquet_index or {}

    def _episode_video_index(self) -> dict[tuple[str, int], Path]:
        if self._video_index is None:
            self._build_indices()
        return self._video_index or {}

    @classmethod
    def detect(cls, path: Path) -> bool:
        info_path = path / "meta" / "info.json"
        if not info_path.is_file():
            return False
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return str(info.get("codebase_version", "")) in {"v2.1", "v2.0"}

    def inspect(self) -> DatasetView:
        info = _load_info(self.path)
        fps = float(info.get("fps") or 0)
        features = info.get("features") or {}
        video_keys = [
            key
            for key, feat in features.items()
            if isinstance(feat, dict) and feat.get("dtype") == "video"
        ]
        episodes_meta = _read_jsonl(self.path / "meta" / "episodes.jsonl")
        if not episodes_meta:
            # Infer from data parquet files (row counts come from parquet
            # metadata; the data pages are never read).
            for idx, parquet_path in sorted(self._episode_parquet_index().items()):
                length = int(pq.ParquetFile(parquet_path).metadata.num_rows)
                episodes_meta.append({"episode_index": idx, "length": length, "tasks": []})

        video_index = self._episode_video_index()
        episodes: list[EpisodeView] = []
        for row in episodes_meta:
            idx = int(row.get("episode_index", 0))
            length = int(row.get("length") or 0)
            cameras: dict[str, CameraRef] = {}
            for key in video_keys:
                found = video_index.get((key, idx))
                if found is not None:
                    rel = str(found.relative_to(self.path))
                    cameras[key] = CameraRef(
                        key=key,
                        kind="video",
                        path=rel,
                        from_timestamp=0.0,
                        to_timestamp=(length / fps) if fps else None,
                    )
            episodes.append(
                EpisodeView(
                    episode_index=idx,
                    length=length,
                    duration=(length / fps) if fps else 0.0,
                    tasks=list(row.get("tasks") or ([row["task"]] if row.get("task") else [])),
                    cameras=cameras,
                )
            )
        episodes.sort(key=lambda ep: ep.episode_index)
        return DatasetView(
            format_id=FORMAT_LEROBOT_V21,
            path=str(self.path),
            name=self.path.name,
            fps=fps,
            robot_type=info.get("robot_type"),
            features=features,
            episodes=episodes,
            total_frames=int(info.get("total_frames") or sum(ep.length for ep in episodes)),
            total_tasks=int(info.get("total_tasks") or 0),
            extras={"codebase_version": info.get("codebase_version")},
        )

    def _episode_parquet(self, episode_index: int) -> Path:
        found = self._episode_parquet_index().get(int(episode_index))
        if found is None:
            raise FileNotFoundError(f"找不到 episode {episode_index} 的 parquet")
        return found

    def get_timeseries(self, episode_index: int, keys: list[str] | None = None) -> dict[str, np.ndarray]:
        from . import tabular

        path = self._episode_parquet(episode_index)
        schema_names = pq.ParquetFile(path).schema_arrow.names
        skip = {"index", "episode_index", "frame_index", "timestamp", "task_index"}
        columns = []
        for column in schema_names:
            if column in skip:
                continue
            if keys is not None and column not in keys:
                continue
            if keys is None and column not in {"action", "observation.state"} and not column.startswith(
                ("action", "observation.state")
            ):
                continue
            columns.append(column)
        if not columns:
            return {}
        table = pq.read_table(path, columns=columns)
        result: dict[str, np.ndarray] = {}
        for column in columns:
            try:
                result[column] = tabular.column_to_ndarray(table[column])
            except (TypeError, ValueError, pa.ArrowInvalid):
                continue
        return result

    def export_subset(
        self,
        output: Path,
        episode_indices: list[int],
        media_mode: str = "hardlink",
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        info = _load_info(self.path)
        output = output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"目标目录已经存在：{output}")
        selected = sorted({int(i) for i in episode_indices})
        if not selected:
            raise ValueError("没有选择任何 episode")
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
        try:
            (temporary / "meta").mkdir(parents=True)
            (temporary / "data" / "chunk-000").mkdir(parents=True)
            old_to_new = {old: new for new, old in enumerate(selected)}
            total_frames = 0
            episode_rows: list[dict[str, Any]] = []
            linked = copied = 0
            episodes_meta_by_index = {
                int(row.get("episode_index", -1)): row
                for row in _read_jsonl(self.path / "meta" / "episodes.jsonl")
            }
            video_index = self._episode_video_index()
            for old in selected:
                src_parquet = self._episode_parquet(old)
                new_idx = old_to_new[old]
                dest_parquet = temporary / "data" / "chunk-000" / f"episode_{new_idx:06d}.parquet"
                table = pq.read_table(src_parquet)
                if "episode_index" in table.column_names:
                    pos = table.schema.get_field_index("episode_index")
                    field = table.schema.field(pos)
                    table = table.set_column(pos, field, pa.array([new_idx] * table.num_rows, type=field.type))
                pq.write_table(table, dest_parquet, compression="zstd")
                length = table.num_rows
                total_frames += length
                tasks: list[str] = []
                meta_row = episodes_meta_by_index.get(old)
                if meta_row:
                    tasks = list(meta_row.get("tasks") or ([meta_row["task"]] if meta_row.get("task") else []))
                episode_rows.append({"episode_index": new_idx, "length": length, "tasks": tasks})

                # videos
                for (_key, ep_i), video in video_index.items():
                    if ep_i != old:
                        continue
                    rel = video.relative_to(self.path)
                    parts = list(rel.parts)
                    # rewrite episode id in filename
                    parts[-1] = f"episode_{new_idx:06d}.mp4"
                    dest = temporary.joinpath(*parts)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    if media_mode == "hardlink":
                        try:
                            os.link(video, dest)
                            linked += 1
                            continue
                        except OSError:
                            pass
                    shutil.copy2(video, dest)
                    copied += 1

            with (temporary / "meta" / "episodes.jsonl").open("w", encoding="utf-8") as handle:
                for row in episode_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")

            # copy static meta
            for name in ("tasks.jsonl", "episodes_stats.jsonl", "stats.json"):
                src = self.path / "meta" / name
                if src.is_file():
                    shutil.copy2(src, temporary / "meta" / name)

            new_info = dict(info)
            new_info["codebase_version"] = "v2.1"
            new_info["total_episodes"] = len(selected)
            new_info["total_frames"] = total_frames
            new_info["splits"] = {"train": f"0:{len(selected)}"}
            (temporary / "meta" / "info.json").write_text(
                json.dumps(new_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            manifest = {
                "version": 1,
                "created_at": datetime.now(timezone.utc).isoformat(),
                "source_dataset": str(self.path),
                "selected_source_episodes": selected,
                "episode_index_mapping": {str(k): v for k, v in old_to_new.items()},
                "media_mode_requested": media_mode,
                "hardlinked_files": linked,
                "copied_files": copied,
                "format": FORMAT_LEROBOT_V21,
            }
            (temporary / "selection_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            os.replace(temporary, output)
            return {
                "output": str(output),
                "totalEpisodes": len(selected),
                "totalFrames": total_frames,
                "hardlinkedVideoFiles": linked,
                "copiedVideoFiles": copied,
            }
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise


class LeRobotV21Writer(DatasetWriter):
    format_id = FORMAT_LEROBOT_V21

    def write_from_episodes(
        self,
        output: Path,
        episodes: Any,
        meta: dict[str, Any],
        mapping: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """``episodes`` may be any iterable; data is written per episode."""
        from . import stats as stats_mod
        from . import tabular

        output = output.expanduser().resolve()
        if output.exists():
            raise FileExistsError(f"目标目录已经存在：{output}")
        mapping = mapping or {}
        fps = float(meta.get("fps") or mapping.get("fps") or 30.0)
        media_mode = str(mapping.get("media_mode") or "hardlink")
        temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
        try:
            data_dir = temporary / "data" / "chunk-000"
            data_dir.mkdir(parents=True)
            (temporary / "meta").mkdir(parents=True)
            episode_rows: list[dict[str, Any]] = []
            total_frames = 0
            features: dict[str, Any] = {}
            all_tasks: set[str] = set()
            collector = stats_mod.StatsCollector()
            for new_index, ep in enumerate(episodes):
                state = ep.get("state")
                action = ep.get("action")
                length = int(ep.get("length") or 0)
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
                    base_index=total_frames,
                    state=state,
                    action=action,
                )
                pq.write_table(
                    table,
                    data_dir / f"episode_{new_index:06d}.parquet",
                    compression="zstd",
                )
                if state is not None:
                    collector.update("observation.state", state[:length])
                if action is not None:
                    collector.update("action", action[:length])
                all_tasks.update(ep.get("tasks") or [])
                images = ep.get("images") or {}
                for cam in sorted(set(ep.get("video_paths") or {}) | set(images)):
                    dest = temporary / "videos" / "chunk-000" / cam / f"episode_{new_index:06d}.mp4"
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
                    if cam not in features:
                        try:
                            width, height = media.probe_mp4_meta(dest)["size"]
                            shape = [int(height), int(width), 3]
                        except Exception:  # noqa: BLE001
                            shape = [240, 320, 3]
                        features[cam] = {
                            "dtype": "video",
                            "shape": shape,
                            "names": ["height", "width", "channels"],
                        }
                if state is not None:
                    features["observation.state"] = {
                        "dtype": "float32",
                        "shape": [int(state.shape[-1])],
                        "names": None,
                    }
                if action is not None:
                    features["action"] = {
                        "dtype": "float32",
                        "shape": [int(action.shape[-1])],
                        "names": None,
                    }
                episode_rows.append(
                    {"episode_index": new_index, "length": length, "tasks": list(ep.get("tasks") or [])}
                )
                total_frames += length

            with (temporary / "meta" / "episodes.jsonl").open("w", encoding="utf-8") as handle:
                for row in episode_rows:
                    handle.write(json.dumps(row, ensure_ascii=False) + "\n")
            info = {
                "codebase_version": "v2.1",
                "robot_type": meta.get("robot_type") or mapping.get("robot_type"),
                "fps": fps,
                "total_episodes": len(episode_rows),
                "total_frames": total_frames,
                "total_tasks": len(all_tasks),
                "splits": {"train": f"0:{len(episode_rows)}"},
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
                "features": features,
            }
            (temporary / "meta" / "info.json").write_text(
                json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            (temporary / "meta" / "stats.json").write_text(
                json.dumps(collector.to_stats_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
            with (temporary / "meta" / "tasks.jsonl").open("w", encoding="utf-8") as handle:
                for idx, task in enumerate(sorted(all_tasks)):
                    handle.write(json.dumps({"task_index": idx, "task": task}, ensure_ascii=False) + "\n")
            os.replace(temporary, output)
            return {
                "output": str(output),
                "totalEpisodes": len(episode_rows),
                "totalFrames": total_frames,
                "format": FORMAT_LEROBOT_V21,
            }
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise
