"""Strict same-format dataset merge pipeline.

The merge path deliberately works on each format's native payload instead of
round-tripping through ``EpisodePayload``.  That keeps arbitrary Parquet
columns, HDF5 groups, encoded videos, and MCAP messages intact while only
rewriting identifiers and (for MCAP) timestamps needed to make the result a
single coherent dataset.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

import pyarrow as pa
import pyarrow.parquet as pq

from datasets.registry import open_dataset
from datasets.view import (
    FORMAT_HDF5,
    FORMAT_LABELS,
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_MCAP,
)
from labels.store import default_labels_path, load_labels, save_labels

ProgressCallback = Callable[[dict[str, Any]], None]


def _emit(callback: ProgressCallback | None, **payload: Any) -> None:
    if callback is None:
        return
    try:
        callback(payload)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("merge progress callback failed")


def _canonical(value: Any) -> Any:
    """Remove volatile feature fields before strict schema comparison."""
    if isinstance(value, dict):
        return {
            str(key): _canonical(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in {"count", "total", "min", "max", "mean", "std"}
        }
    if isinstance(value, (list, tuple)):
        return [_canonical(item) for item in value]
    return value


def _schema_text(schema: pa.Schema) -> str:
    return str(schema.remove_metadata())


def _native_schemas(adapter: Any) -> dict[str, list[str]]:
    """Return native table schemas so strict mode catches hidden columns too."""
    if adapter.format_id == FORMAT_LEROBOT_V21:
        schemas = {
            _schema_text(pq.ParquetFile(path).schema_arrow)
            for path in adapter._episode_parquet_index().values()  # noqa: SLF001
        }
        return {"data": sorted(schemas)}
    if adapter.format_id == FORMAT_LEROBOT_V3:
        info = adapter._validated_info()  # noqa: SLF001
        data = {_schema_text(pq.ParquetFile(path).schema_arrow) for path in info["_data_files"]}
        episodes = {
            _schema_text(pq.ParquetFile(path).schema_arrow) for path in info["_episode_files"]
        }
        return {"data": sorted(data), "episodes": sorted(episodes)}
    if adapter.format_id == FORMAT_HDF5:
        import h5py

        signatures: set[str] = set()

        def visit(group: Any, prefix: str = "") -> list[dict[str, Any]]:
            rows: list[dict[str, Any]] = []
            for name in sorted(group.keys()):
                item = group[name]
                path = f"{prefix}/{name}" if prefix else name
                if isinstance(item, h5py.Group):
                    rows.append({"path": path, "kind": "group"})
                    rows.extend(visit(item, path))
                else:
                    shape = list(item.shape)
                    # Episode length is allowed to vary; the remaining axes
                    # and dtype define the native per-frame schema.
                    rows.append(
                        {
                            "path": path,
                            "kind": "dataset",
                            "dtype": str(item.dtype),
                            "shape": shape[1:] if shape else [],
                        }
                    )
            return rows

        for episode in adapter.inspect().episodes:
            file_path, demo_key, _ = adapter._episode_ref(episode.episode_index)  # noqa: SLF001
            with h5py.File(file_path, "r") as handle:
                if demo_key and "data" in handle and demo_key in handle["data"]:
                    group = handle["data"][demo_key]
                elif demo_key:
                    group = handle[demo_key]
                elif "data" in handle:
                    group = handle["data"]
                else:
                    group = handle
                signatures.add(json.dumps(visit(group), ensure_ascii=False, sort_keys=True))
        return {"hdf5": sorted(signatures)}
    if adapter.format_id == FORMAT_MCAP:
        from mcap.reader import make_reader

        files = sorted(
            {
                str(episode.extras.get("mcapFile"))
                for episode in adapter.inspect().episodes
                if episode.extras.get("mcapFile")
            }
        )
        signatures: set[str] = set()
        for raw_path in files:
            with Path(raw_path).open("rb") as handle:
                reader = make_reader(handle)
                summary = reader.get_summary()
            schemas = summary.schemas if summary else {}
            channels = summary.channels if summary else {}
            rows = []
            for _channel_id, channel in sorted(channels.items()):
                schema = schemas.get(channel.schema_id)
                rows.append(
                    {
                        "topic": channel.topic,
                        "message_encoding": channel.message_encoding,
                        "schema_name": schema.name if schema else "",
                        "schema_encoding": schema.encoding if schema else "",
                        "schema_sha256": hashlib.sha256(schema.data).hexdigest() if schema else "",
                        "metadata": dict(channel.metadata or {}),
                    }
                )
            if not rows:
                # Valid MCAP files may omit the optional summary section.
                # Fall back to one sequential pass and de-duplicate channel
                # descriptors without retaining message payloads.
                seen: set[str] = set()
                with Path(raw_path).open("rb") as handle:
                    for schema, channel, _message in make_reader(handle).iter_messages():
                        row = {
                            "topic": channel.topic,
                            "message_encoding": channel.message_encoding,
                            "schema_name": schema.name if schema else "",
                            "schema_encoding": schema.encoding if schema else "",
                            "schema_sha256": hashlib.sha256(schema.data).hexdigest() if schema else "",
                            "metadata": dict(channel.metadata or {}),
                        }
                        key = json.dumps(row, ensure_ascii=False, sort_keys=True)
                        if key not in seen:
                            rows.append(row)
                            seen.add(key)
            rows.sort(key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True))
            signatures.add(json.dumps(rows, ensure_ascii=False, sort_keys=True))
        return {"mcap": sorted(signatures)}
    return {}


def _source_summary(adapter: Any, view: Any) -> dict[str, Any]:
    labels_path = default_labels_path(Path(view.path))
    return {
        "path": view.path,
        "name": view.name,
        "format": view.format_id,
        "formatLabel": FORMAT_LABELS.get(view.format_id, view.format_id),
        "totalEpisodes": len(view.episodes),
        "totalFrames": int(view.total_frames or sum(ep.length for ep in view.episodes)),
        "fps": float(view.fps or 0),
        "robotType": view.robot_type,
        "cameraKeys": sorted({key for episode in view.episodes for key in episode.cameras}),
        "features": _canonical(view.features),
        "dialect": view.extras.get("dialect"),
        "labels": len(load_labels(labels_path)) if labels_path.is_file() else 0,
        "nativeSchemas": _native_schemas(adapter),
    }


def preflight_merge(sources: Iterable[Path]) -> dict[str, Any]:
    resolved = [Path(source).expanduser().resolve() for source in sources]
    if len(resolved) < 2:
        raise ValueError("至少需要两个源数据集")
    if len(set(resolved)) != len(resolved):
        raise ValueError("源数据集列表中存在重复路径")

    adapters = [open_dataset(source) for source in resolved]
    views = [adapter.inspect() for adapter in adapters]
    summaries = [_source_summary(adapter, view) for adapter, view in zip(adapters, views)]
    conflicts: list[dict[str, Any]] = []

    def conflict(code: str, message: str, source_index: int | None = None) -> None:
        row: dict[str, Any] = {"code": code, "message": message}
        if source_index is not None:
            row["sourceIndex"] = source_index
        conflicts.append(row)

    baseline = summaries[0]
    if baseline["totalEpisodes"] <= 0:
        conflict("empty_dataset", f"源数据集没有 episode：{baseline['path']}", 0)
    for kind, schemas in baseline["nativeSchemas"].items():
        if len(schemas) > 1:
            conflict("internal_schema", f"源数据集内部存在多种 {kind} schema：{baseline['path']}", 0)

    for index, item in enumerate(summaries[1:], start=1):
        if item["totalEpisodes"] <= 0:
            conflict("empty_dataset", f"源数据集没有 episode：{item['path']}", index)
        if item["format"] != baseline["format"]:
            conflict(
                "format_mismatch",
                f"格式不一致：{baseline['formatLabel']} 与 {item['formatLabel']}",
                index,
            )
            continue
        if not math.isclose(float(item["fps"]), float(baseline["fps"]), rel_tol=1e-6, abs_tol=1e-6):
            conflict("fps_mismatch", f"FPS 不一致：{baseline['fps']} 与 {item['fps']}", index)
        if item["robotType"] != baseline["robotType"]:
            conflict(
                "robot_mismatch",
                f"robot_type 不一致：{baseline['robotType'] or '未设置'} 与 {item['robotType'] or '未设置'}",
                index,
            )
        if item["cameraKeys"] != baseline["cameraKeys"]:
            conflict(
                "camera_mismatch",
                f"相机字段不一致：{baseline['cameraKeys']} 与 {item['cameraKeys']}",
                index,
            )
        if item["features"] != baseline["features"]:
            conflict("feature_mismatch", f"特征 schema 不一致：{item['path']}", index)
        if item["dialect"] != baseline["dialect"]:
            conflict(
                "dialect_mismatch",
                f"HDF5 方言不一致：{baseline['dialect']} 与 {item['dialect']}",
                index,
            )
        schema_kinds = set(baseline["nativeSchemas"]) | set(item["nativeSchemas"])
        for kind in sorted(schema_kinds):
            schemas = item["nativeSchemas"].get(kind, [])
            if len(schemas) > 1:
                conflict("internal_schema", f"源数据集内部存在多种 {kind} schema：{item['path']}", index)
            if schemas != baseline["nativeSchemas"].get(kind, []):
                conflict("native_schema_mismatch", f"原生 {kind} 表 schema 不一致：{item['path']}", index)

    public_summaries = [
        {key: value for key, value in item.items() if key not in {"features", "nativeSchemas"}}
        for item in summaries
    ]
    return {
        "compatible": not conflicts,
        "format": baseline["format"],
        "formatLabel": baseline["formatLabel"],
        "sources": public_summaries,
        "conflicts": conflicts,
        "totalEpisodes": sum(int(item["totalEpisodes"]) for item in summaries),
        "totalFrames": sum(int(item["totalFrames"]) for item in summaries),
        "totalLabels": sum(int(item["labels"]) for item in summaries),
        "policy": "strict",
    }


def _is_inside(parent: Path, child: Path) -> bool:
    try:
        child.relative_to(parent)
        return True
    except ValueError:
        return False


def _actual_output(output: Path, format_id: str) -> Path:
    output = output.expanduser().resolve()
    if format_id == FORMAT_HDF5 and output.suffix.lower() not in {".hdf5", ".h5"}:
        return output.with_suffix(".hdf5")
    if format_id == FORMAT_MCAP and output.suffix.lower() != ".mcap":
        return output.with_suffix(".mcap")
    return output


def _copy_or_link(source: Path, destination: Path, media_mode: str) -> str:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if media_mode == "hardlink":
        try:
            os.link(source, destination)
            return "linked"
        except OSError:
            pass
    shutil.copy2(source, destination)
    return "copied"


def _replace_column(table: pa.Table, name: str, values: Iterable[Any]) -> pa.Table:
    position = table.schema.get_field_index(name)
    if position < 0:
        return table
    field = table.schema.field(position)
    return table.set_column(position, field, pa.array(list(values), type=field.type))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def _task_rows(root: Path) -> list[dict[str, Any]]:
    rows = _read_jsonl(root / "meta" / "tasks.jsonl")
    if rows:
        return rows
    candidates = list(root.glob("meta/tasks*.parquet")) + list(root.glob("meta/tasks/**/*.parquet"))
    if candidates:
        return pq.read_table(sorted(set(candidates))).to_pylist()
    return []


def _task_maps(adapters: list[Any], views: list[Any]) -> tuple[list[str], list[dict[int, int]]]:
    tasks: list[str] = []
    task_to_new: dict[str, int] = {}
    source_maps: list[dict[int, int]] = []
    for adapter, view in zip(adapters, views):
        old_to_task: dict[int, str] = {}
        for row in _task_rows(Path(view.path)):
            task = str(row.get("task") or row.get("name") or "")
            if not task:
                continue
            old_to_task[int(row.get("task_index", len(old_to_task)))] = task
        for episode in view.episodes:
            for task in episode.tasks:
                task = str(task)
                if task and task not in task_to_new:
                    task_to_new[task] = len(tasks)
                    tasks.append(task)
        for task in old_to_task.values():
            if task not in task_to_new:
                task_to_new[task] = len(tasks)
                tasks.append(task)
        source_maps.append({old: task_to_new[task] for old, task in old_to_task.items()})
    return tasks, source_maps


def _write_v3_tasks(target: Path, baseline: Path, tasks: list[str]) -> None:
    """Keep the baseline dataset's task-table layout (JSONL or Parquet)."""
    rows = [{"task_index": index, "task": task} for index, task in enumerate(tasks)]
    flat = baseline / "meta" / "tasks.parquet"
    nested = sorted((baseline / "meta" / "tasks").glob("**/*.parquet"))
    if flat.is_file():
        schema = pq.ParquetFile(flat).schema_arrow
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), target / "meta" / "tasks.parquet")
        return
    if nested:
        schema = pq.ParquetFile(nested[0]).schema_arrow
        relative = nested[0].relative_to(baseline / "meta" / "tasks")
        destination = target / "meta" / "tasks" / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(pa.Table.from_pylist(rows, schema=schema), destination)
        return
    with (target / "meta" / "tasks.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _remap_task_column(table: pa.Table, mapping: dict[int, int]) -> pa.Table:
    if "task_index" not in table.column_names or not mapping:
        return table
    values = table["task_index"].to_pylist()
    remapped = [mapping.get(int(value), int(value)) if value is not None else None for value in values]
    return _replace_column(table, "task_index", remapped)


def _merge_v21(
    adapters: list[Any], views: list[Any], target: Path, media_mode: str, callback: ProgressCallback | None
) -> dict[str, Any]:
    from datasets import stats as stats_mod
    from datasets import tabular
    from datasets.lerobot_v21 import _load_info

    baseline = Path(views[0].path)
    info = _load_info(baseline)
    (target / "meta").mkdir(parents=True, exist_ok=True)
    for item in (baseline / "meta").iterdir():
        if item.name in {"info.json", "episodes.jsonl", "tasks.jsonl", "stats.json", "episodes_stats.jsonl"}:
            continue
        destination = target / "meta" / item.name
        shutil.copytree(item, destination) if item.is_dir() else shutil.copy2(item, destination)

    tasks, task_maps = _task_maps(adapters, views)
    episode_meta: list[dict[str, Any]] = []
    merged_episode_stats: list[dict[str, Any]] = []
    collector = stats_mod.StatsCollector()
    global_frame = 0
    new_index = 0
    linked = copied = 0
    total = sum(len(view.episodes) for view in views)

    for source_no, (adapter, view) in enumerate(zip(adapters, views)):
        source_root = Path(view.path)
        source_meta = {
            int(row.get("episode_index", -1)): row
            for row in _read_jsonl(source_root / "meta" / "episodes.jsonl")
        }
        source_episode_stats = {
            int(row.get("episode_index", -1)): row
            for row in _read_jsonl(source_root / "meta" / "episodes_stats.jsonl")
        }
        video_index = adapter._episode_video_index()  # noqa: SLF001
        for episode in view.episodes:
            old_index = int(episode.episode_index)
            table = pq.read_table(adapter._episode_parquet(old_index))  # noqa: SLF001
            table = _replace_column(table, "episode_index", [new_index] * table.num_rows)
            table = _replace_column(table, "index", range(global_frame, global_frame + table.num_rows))
            table = _remap_task_column(table, task_maps[source_no])
            chunk = new_index // 1000
            data_path = target / "data" / f"chunk-{chunk:03d}" / f"episode_{new_index:06d}.parquet"
            data_path.parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(table, data_path, compression="zstd")

            for name in table.column_names:
                if name in {"index", "episode_index", "frame_index", "timestamp", "task_index"}:
                    continue
                try:
                    collector.update(name, tabular.column_to_ndarray(table[name]))
                except Exception:  # noqa: BLE001
                    continue

            row = dict(source_meta.get(old_index) or {})
            row.update(
                episode_index=new_index,
                length=table.num_rows,
                tasks=list(episode.tasks),
            )
            if row.get("task_index") is not None:
                row["task_index"] = task_maps[source_no].get(int(row["task_index"]), int(row["task_index"]))
            episode_meta.append(row)
            if old_index in source_episode_stats:
                stats_row = dict(source_episode_stats[old_index])
                stats_row["episode_index"] = new_index
                merged_episode_stats.append(stats_row)

            for (camera, ep_index), source_video in video_index.items():
                if ep_index != old_index:
                    continue
                destination = target / "videos" / f"chunk-{chunk:03d}" / camera / f"episode_{new_index:06d}.mp4"
                outcome = _copy_or_link(source_video, destination, media_mode)
                linked += outcome == "linked"
                copied += outcome == "copied"

            global_frame += table.num_rows
            new_index += 1
            _emit(
                callback,
                stage="merge",
                current=new_index,
                total=total,
                progress=0.08 + 0.82 * new_index / max(1, total),
                message=f"合并 episode（{new_index}/{total}）",
            )

    with (target / "meta" / "episodes.jsonl").open("w", encoding="utf-8") as handle:
        for row in episode_meta:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (target / "meta" / "tasks.jsonl").open("w", encoding="utf-8") as handle:
        for index, task in enumerate(tasks):
            handle.write(json.dumps({"task_index": index, "task": task}, ensure_ascii=False) + "\n")
    if merged_episode_stats:
        with (target / "meta" / "episodes_stats.jsonl").open("w", encoding="utf-8") as handle:
            for row in merged_episode_stats:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (target / "meta" / "stats.json").write_text(
        json.dumps(collector.to_stats_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    info = dict(info)
    info.update(
        codebase_version="v2.1",
        total_episodes=new_index,
        total_frames=global_frame,
        total_tasks=len(tasks),
        splits={"train": f"0:{new_index}"},
    )
    (target / "meta" / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "totalEpisodes": new_index,
        "totalFrames": global_frame,
        "hardlinkedVideoFiles": linked,
        "copiedVideoFiles": copied,
    }


def _merge_v3(
    adapters: list[Any], views: list[Any], target: Path, media_mode: str, callback: ProgressCallback | None
) -> dict[str, Any]:
    from datasets import lerobot_v3_lib as lib
    from datasets import stats as stats_mod
    from datasets import tabular

    infos = [adapter._validated_info() for adapter in adapters]  # noqa: SLF001
    baseline_root = Path(infos[0]["_root"])
    info = {key: value for key, value in infos[0].items() if not str(key).startswith("_")}
    (target / "meta").mkdir(parents=True, exist_ok=True)
    for item in (baseline_root / "meta").iterdir():
        if item.name in {"info.json", "stats.json", "episodes", "tasks.jsonl", "tasks.parquet", "tasks"}:
            continue
        destination = target / "meta" / item.name
        shutil.copytree(item, destination) if item.is_dir() else shutil.copy2(item, destination)

    tasks, task_maps = _task_maps(adapters, views)
    episode_rows: list[dict[str, Any]] = []
    lengths: list[int] = []
    source_episode_maps: list[dict[int, int]] = []
    video_destinations: dict[tuple[str, str, str], tuple[int, int]] = {}
    linked = copied = 0
    new_index = 0
    global_frame = 0
    video_keys = lib.discover_video_keys(pq.read_table(infos[0]["_episode_files"]).column_names)

    for source_no, (source_info, view) in enumerate(zip(infos, views)):
        source_root = Path(source_info["_root"])
        table = pq.read_table(source_info["_episode_files"])
        rows_by_index = {int(row["episode_index"]): row for row in table.to_pylist()}
        index_map: dict[int, int] = {}
        for episode in view.episodes:
            old = int(episode.episode_index)
            row = dict(rows_by_index[old])
            index_map[old] = new_index
            length = int(row.get("length") or episode.length)
            row["episode_index"] = new_index
            row["dataset_from_index"] = global_frame
            global_frame += length
            row["dataset_to_index"] = global_frame
            row["data/chunk_index"] = 0
            row["data/file_index"] = 0
            if row.get("task_index") is not None:
                row["task_index"] = task_maps[source_no].get(int(row["task_index"]), int(row["task_index"]))
            for camera in video_keys:
                relative = lib.format_video_path(
                    source_info["video_path"],
                    camera,
                    int(row[f"videos/{camera}/chunk_index"]),
                    int(row[f"videos/{camera}/file_index"]),
                )
                key = (str(source_root), camera, relative)
                if key not in video_destinations:
                    file_number = len(video_destinations)
                    destination_chunk = file_number // 1000
                    destination_file = file_number % 1000
                    video_destinations[key] = (destination_chunk, destination_file)
                    destination_relative = lib.format_video_path(
                        info["video_path"], camera, destination_chunk, destination_file
                    )
                    outcome = _copy_or_link(source_root / relative, target / destination_relative, media_mode)
                    linked += outcome == "linked"
                    copied += outcome == "copied"
                destination_chunk, destination_file = video_destinations[key]
                row[f"videos/{camera}/chunk_index"] = destination_chunk
                row[f"videos/{camera}/file_index"] = destination_file
            episode_rows.append(row)
            lengths.append(length)
            new_index += 1
        source_episode_maps.append(index_map)

    episode_schema = pq.read_table(infos[0]["_episode_files"]).schema
    episode_table = pa.Table.from_pylist(episode_rows, schema=episode_schema)
    lib.write_episode_metadata(episode_table, target, int(info.get("chunks_size") or 1000))

    output_data = target / str(info["data_path"]).format(chunk_index=0, file_index=0)
    output_data.parent.mkdir(parents=True, exist_ok=True)
    writer: pq.ParquetWriter | None = None
    written = 0
    last_episode = -1
    total_episodes = len(episode_rows)
    frame_counts = {index: 0 for index in range(total_episodes)}
    completed_episodes = 0
    collector = stats_mod.StatsCollector()
    feature_names = set((info.get("features") or {}).keys())
    try:
        for source_no, source_info in enumerate(infos):
            index_map = source_episode_maps[source_no]
            for data_file in source_info["_data_files"]:
                parquet = pq.ParquetFile(data_file)
                for row_group in range(parquet.num_row_groups):
                    table = parquet.read_row_group(row_group)
                    old_values = table["episode_index"].to_pylist()
                    keep = [value is not None and int(value) in index_map for value in old_values]
                    if not all(keep):
                        table = table.filter(pa.array(keep))
                        old_values = table["episode_index"].to_pylist()
                    if table.num_rows == 0:
                        continue
                    mapped = [index_map[int(value)] for value in old_values]
                    if mapped and (mapped[0] < last_episode or any(a > b for a, b in zip(mapped, mapped[1:]))):
                        raise ValueError(f"源 LeRobot v3 数据帧未按 episode 连续排列：{data_file}")
                    if mapped:
                        last_episode = mapped[-1]
                        for value in mapped:
                            frame_counts[value] += 1
                    table = _replace_column(table, "episode_index", mapped)
                    table = _replace_column(table, "index", range(written, written + table.num_rows))
                    table = _remap_task_column(table, task_maps[source_no])
                    for name in feature_names.intersection(table.column_names):
                        try:
                            collector.update(name, tabular.column_to_ndarray(table[name]))
                        except Exception:  # noqa: BLE001
                            continue
                    if writer is None:
                        writer = pq.ParquetWriter(output_data, table.schema, compression="zstd")
                    writer.write_table(table)
                    written += table.num_rows
            completed_episodes += len(index_map)
            _emit(
                callback,
                stage="merge",
                current=completed_episodes,
                total=total_episodes,
                progress=0.08 + 0.82 * completed_episodes / max(1, total_episodes),
                message=f"合并数据集（{source_no + 1}/{len(infos)}）",
            )
    finally:
        if writer is not None:
            writer.close()
    if writer is None or written != sum(lengths) or any(
        frame_counts[index] != length for index, length in enumerate(lengths)
    ):
        raise ValueError(f"合并后的帧数与 episode 元数据不一致：data={written}，meta={sum(lengths)}")

    _write_v3_tasks(target, baseline_root, tasks)
    stats = collector.to_stats_dict()
    structural_stats = lib.aggregate_episode_stats(episode_table, lengths, written)
    for name in ("index", "episode_index"):
        if name in structural_stats:
            stats[name] = structural_stats[name]
    (target / "meta" / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    info.update(
        codebase_version="v3.0",
        total_episodes=len(episode_rows),
        total_frames=written,
        total_tasks=len(tasks),
        splits={"train": f"0:{len(episode_rows)}"},
    )
    (target / "meta" / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return {
        "totalEpisodes": len(episode_rows),
        "totalFrames": written,
        "hardlinkedVideoFiles": linked,
        "copiedVideoFiles": copied,
    }


def _merge_hdf5(
    adapters: list[Any], views: list[Any], target: Path, callback: ProgressCallback | None
) -> dict[str, Any]:
    import h5py

    total = sum(len(view.episodes) for view in views)
    new_index = 0
    total_frames = 0
    with h5py.File(target, "w") as destination:
        destination_data = destination.create_group("data")
        copied_root_attrs = False
        copied_data_attrs = False
        for adapter, view in zip(adapters, views):
            for episode in view.episodes:
                source_file, demo_key, _ = adapter._episode_ref(episode.episode_index)  # noqa: SLF001
                with h5py.File(source_file, "r") as source:
                    if demo_key and "data" in source and demo_key in source["data"]:
                        source_group = source["data"][demo_key]
                    elif demo_key:
                        source_group = source[demo_key]
                    elif "data" in source:
                        source_group = source["data"]
                    else:
                        source_group = None
                    if source_group is not None:
                        source.copy(source_group, destination_data, name=f"demo_{new_index}")
                    else:
                        # HDF5 dialects such as Astribot store one episode at
                        # the file root.  HDF5 cannot copy the root object
                        # itself, so reproduce its children and attributes.
                        merged_group = destination_data.create_group(f"demo_{new_index}")
                        for key, value in source.attrs.items():
                            merged_group.attrs[key] = value
                        for name in source.keys():
                            source.copy(source[name], merged_group, name=name)
                    if not copied_root_attrs:
                        for key, value in source.attrs.items():
                            destination.attrs[key] = value
                        copied_root_attrs = True
                    if not copied_data_attrs and "data" in source:
                        for key, value in source["data"].attrs.items():
                            destination_data.attrs[key] = value
                        copied_data_attrs = True
                total_frames += int(episode.length)
                new_index += 1
                _emit(
                    callback,
                    stage="merge",
                    current=new_index,
                    total=total,
                    progress=0.08 + 0.82 * new_index / max(1, total),
                    message=f"复制 HDF5 episode（{new_index}/{total}）",
                )
        destination_data.attrs["total"] = new_index
    return {"totalEpisodes": new_index, "totalFrames": total_frames}


def _merge_mcap(
    adapters: list[Any], views: list[Any], target: Path, callback: ProgressCallback | None
) -> dict[str, Any]:
    from mcap.reader import make_reader
    from mcap.writer import Writer

    from datasets.mcap_adapter import _episode_window

    total = sum(len(view.episodes) for view in views)
    cursor_ns = 0
    gap_ns = int(3e9)
    completed = 0
    message_count = 0
    with target.open("wb") as handle:
        writer = Writer(handle)
        writer.start()
        schema_ids: dict[tuple[str, str, bytes], int] = {}
        channel_ids: dict[tuple[Any, ...], int] = {}
        for adapter, view in zip(adapters, views):
            for episode in view.episodes:
                source_file, source_episode = adapter._episode_mcap(episode.episode_index)  # noqa: SLF001
                start_ns, end_ns = _episode_window(source_episode)
                max_relative = 0
                with source_file.open("rb") as source_handle:
                    reader = make_reader(source_handle)
                    for schema, channel, message in reader.iter_messages():
                        if message.log_time < start_ns or message.log_time > end_ns:
                            continue
                        schema_key = (
                            schema.name if schema else "",
                            schema.encoding if schema else "",
                            schema.data if schema else b"",
                        )
                        if schema and schema_key not in schema_ids:
                            schema_ids[schema_key] = writer.register_schema(
                                name=schema.name, encoding=schema.encoding, data=schema.data
                            )
                        metadata_key = json.dumps(dict(channel.metadata or {}), sort_keys=True)
                        channel_key = (
                            channel.topic,
                            channel.message_encoding,
                            schema_key,
                            metadata_key,
                        )
                        if channel_key not in channel_ids:
                            channel_ids[channel_key] = writer.register_channel(
                                topic=channel.topic,
                                message_encoding=channel.message_encoding,
                                schema_id=schema_ids.get(schema_key, 0) if schema else 0,
                                metadata=dict(channel.metadata or {}),
                            )
                        relative_log = max(0, int(message.log_time) - int(start_ns))
                        relative_publish = max(0, int(message.publish_time) - int(start_ns))
                        max_relative = max(max_relative, relative_log, relative_publish)
                        writer.add_message(
                            channel_id=channel_ids[channel_key],
                            log_time=cursor_ns + relative_log,
                            publish_time=cursor_ns + relative_publish,
                            data=message.data,
                        )
                        message_count += 1
                cursor_ns += max(max_relative, int(end_ns) - int(start_ns), 1) + gap_ns
                completed += 1
                _emit(
                    callback,
                    stage="merge",
                    current=completed,
                    total=total,
                    progress=0.08 + 0.82 * completed / max(1, total),
                    message=f"重排 MCAP episode（{completed}/{total}）",
                )
        writer.finish()
    return {"totalEpisodes": completed, "totalFrames": message_count, "messageCount": message_count}


def _merge_labels(
    sources: list[Path], views: list[Any], destination: Path
) -> tuple[int, list[dict[str, Any]]]:
    merged: list[dict[str, Any]] = []
    mapping_rows: list[dict[str, Any]] = []
    output_index = 0
    for source, view in zip(sources, views):
        source_labels = load_labels(default_labels_path(source))
        labels_by_episode: dict[int, list[dict[str, Any]]] = {}
        for label in source_labels:
            try:
                labels_by_episode.setdefault(int(label["episode_index"]), []).append(label)
            except (KeyError, TypeError, ValueError):
                continue
        for episode in view.episodes:
            old_index = int(episode.episode_index)
            mapping_rows.append(
                {
                    "output_episode_index": output_index,
                    "source_dataset": str(source),
                    "source_episode_index": old_index,
                }
            )
            for label in labels_by_episode.get(old_index, []):
                remapped = dict(label)
                remapped["episode_index"] = output_index
                merged.append(remapped)
            output_index += 1
    if merged:
        save_labels(destination, merged)
    return len(merged), mapping_rows


def merge_datasets(
    sources: Iterable[Path],
    output: Path,
    *,
    media_mode: str = "hardlink",
    copy_labels: bool = True,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    source_paths = [Path(source).expanduser().resolve() for source in sources]
    if media_mode not in {"hardlink", "copy"}:
        raise ValueError("media_mode 只能是 hardlink 或 copy")
    _emit(progress_callback, stage="preflight", progress=0.02, message="检查数据集兼容性…")
    preflight = preflight_merge(source_paths)
    if not preflight["compatible"]:
        messages = "；".join(item["message"] for item in preflight["conflicts"][:8])
        raise ValueError(f"数据集不兼容：{messages}")

    format_id = str(preflight["format"])
    actual_output = _actual_output(Path(output), format_id)
    if actual_output.exists():
        raise FileExistsError(f"输出路径已经存在：{actual_output}")
    directory_output = format_id in {FORMAT_LEROBOT_V21, FORMAT_LEROBOT_V3}
    if not directory_output:
        manifest_sidecar = actual_output.with_name(actual_output.name + ".merge_manifest.json")
        labels_sidecar = default_labels_path(actual_output)
        if manifest_sidecar.exists():
            raise FileExistsError(f"合并清单已经存在：{manifest_sidecar}")
        if copy_labels and labels_sidecar.exists():
            raise FileExistsError(f"标注输出已经存在：{labels_sidecar}")
    for source in source_paths:
        if actual_output == source or (source.is_dir() and _is_inside(source, actual_output)):
            raise ValueError(f"输出路径不能等于或位于源数据集内部：{source}")
    actual_output.parent.mkdir(parents=True, exist_ok=True)

    adapters = [open_dataset(source) for source in source_paths]
    views = [adapter.inspect() for adapter in adapters]
    if directory_output:
        staging = Path(tempfile.mkdtemp(prefix=f".{actual_output.name}.building-", dir=actual_output.parent))
    else:
        staging = actual_output.with_name(f".{actual_output.name}.building-{uuid.uuid4().hex}")

    try:
        if format_id == FORMAT_LEROBOT_V21:
            result = _merge_v21(adapters, views, staging, media_mode, progress_callback)
        elif format_id == FORMAT_LEROBOT_V3:
            result = _merge_v3(adapters, views, staging, media_mode, progress_callback)
        elif format_id == FORMAT_HDF5:
            result = _merge_hdf5(adapters, views, staging, progress_callback)
        elif format_id == FORMAT_MCAP:
            result = _merge_mcap(adapters, views, staging, progress_callback)
        else:
            raise ValueError(f"不支持合并的格式：{format_id}")

        labels_count = 0
        mapping_rows: list[dict[str, Any]] = []
        output_index = 0
        for source, view in zip(source_paths, views):
            for episode in view.episodes:
                mapping_rows.append(
                    {
                        "output_episode_index": output_index,
                        "source_dataset": str(source),
                        "source_episode_index": int(episode.episode_index),
                    }
                )
                output_index += 1

        if copy_labels and directory_output:
            labels_count, _ = _merge_labels(source_paths, views, staging / "labels.jsonl")

        manifest = {
            "version": 1,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "format": format_id,
            "format_label": FORMAT_LABELS.get(format_id, format_id),
            "output": str(actual_output),
            "compatibility_policy": "strict",
            "media_mode_requested": media_mode,
            "sources": [
                {
                    "path": item["path"],
                    "total_episodes": item["totalEpisodes"],
                    "total_frames": item["totalFrames"],
                }
                for item in preflight["sources"]
            ],
            "episode_index_mapping": mapping_rows,
            "labels_merged": labels_count,
            "result": result,
        }
        if directory_output:
            (staging / "merge_manifest.json").write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        os.replace(staging, actual_output)

        if not directory_output:
            if copy_labels:
                labels_count, _ = _merge_labels(
                    source_paths, views, default_labels_path(actual_output)
                )
                manifest["labels_merged"] = labels_count
            manifest_path = actual_output.with_name(actual_output.name + ".merge_manifest.json")
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
            )
        else:
            manifest_path = actual_output / "merge_manifest.json"

        result.update(
            output=str(actual_output),
            format=format_id,
            manifest=str(manifest_path),
            labelsMerged=labels_count,
            sources=len(source_paths),
        )
        _emit(
            progress_callback,
            stage="done",
            current=int(result.get("totalEpisodes") or 0),
            total=int(result.get("totalEpisodes") or 0),
            progress=1.0,
            message="合并导出完成",
        )
        return result
    except BaseException:
        if staging.is_dir():
            shutil.rmtree(staging, ignore_errors=True)
        elif staging.exists():
            staging.unlink(missing_ok=True)
        raise
