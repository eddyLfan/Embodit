#!/usr/bin/env python3
"""Backend for the VS Code LeRobot v3 dataset filter.

The backend deliberately never transcodes video. A filtered dataset rewrites
Parquet metadata/data while hard-linking or copying only referenced source MP4
shards. Referenced shards may still contain unselected frames, but those frames
are not addressable from the filtered episode metadata.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq


VIDEO_COLUMN = re.compile(r"^videos/(.+)/(chunk_index|file_index|from_timestamp|to_timestamp)$")
STAT_COLUMN = re.compile(r"^stats/(.+)/(min|max|mean|std|count|q01|q10|q50|q90|q99)$")
STAT_METRICS = ("min", "max", "mean", "std", "count", "q01", "q10", "q50", "q90", "q99")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect and filter LeRobot v3 datasets")
    subparsers = parser.add_subparsers(dest="command", required=True)

    inspect_parser = subparsers.add_parser("inspect")
    inspect_parser.add_argument("--dataset", required=True, type=Path)

    create_parser = subparsers.add_parser("create")
    create_parser.add_argument("--dataset", required=True, type=Path)
    create_parser.add_argument("--output", required=True, type=Path)
    create_parser.add_argument("--selection", required=True, type=Path)
    create_parser.add_argument("--media-mode", choices=("hardlink", "copy"), default="hardlink")

    args = parser.parse_args()
    if args.command == "inspect":
        result = inspect_dataset(args.dataset)
    else:
        result = create_dataset(args.dataset, args.output, args.selection, args.media_mode)
    print(json.dumps(result, ensure_ascii=False))


def validate_dataset(root: Path) -> dict[str, Any]:
    root = root.expanduser().resolve()
    info_path = root / "meta" / "info.json"
    if not info_path.is_file():
        raise ValueError(f"所选目录不是 LeRobot 数据集，缺少：{info_path}")
    info = json.loads(info_path.read_text(encoding="utf-8"))
    if str(info.get("codebase_version", "")) not in {"v3.0", "v3"}:
        raise ValueError(f"目前只支持 LeRobot v3.0，检测到：{info.get('codebase_version', 'unknown')}")
    episode_files = sorted(root.glob("meta/episodes/chunk-*/*.parquet"))
    data_files = sorted(root.glob("data/chunk-*/*.parquet"))
    if not episode_files:
        raise ValueError("数据集缺少 meta/episodes Parquet 文件")
    if not data_files:
        raise ValueError("数据集缺少 data Parquet 文件")
    info["_root"] = root
    info["_episode_files"] = episode_files
    info["_data_files"] = data_files
    return info


def inspect_dataset(root: Path) -> dict[str, Any]:
    info = validate_dataset(root)
    root = info.pop("_root")
    episode_files = info.pop("_episode_files")
    info.pop("_data_files")
    episodes_table = pq.read_table(episode_files)
    video_keys = discover_video_keys(episodes_table.column_names)
    fps = float(info.get("fps") or 0)
    episodes: list[dict[str, Any]] = []

    for row in episodes_table.to_pylist():
        videos: dict[str, Any] = {}
        durations: list[float] = []
        for key in video_keys:
            chunk = int(row[f"videos/{key}/chunk_index"])
            file_index = int(row[f"videos/{key}/file_index"])
            start = float(row[f"videos/{key}/from_timestamp"])
            end = float(row[f"videos/{key}/to_timestamp"])
            relative = format_video_path(info["video_path"], key, chunk, file_index)
            videos[key] = {
                "path": relative,
                "fromTimestamp": start,
                "toTimestamp": end,
            }
            durations.append(max(0.0, end - start))
        length = int(row["length"])
        duration = min(durations) if durations else (length / fps if fps else 0.0)
        intervention_values = [
            value
            for column, value in row.items()
            if column.startswith("stats/") and ".is_intervene/max" in column
        ]
        has_intervention = any(any_number_greater_than_zero(value) for value in intervention_values)
        episodes.append(
            {
                "episodeIndex": int(row["episode_index"]),
                "platformEpisodeId": str(row.get("platform_episode_id") or ""),
                "tasks": list(row.get("tasks") or []),
                "length": length,
                "duration": duration,
                "hasIntervention": has_intervention,
                "videos": videos,
            }
        )

    return {
        "path": str(root),
        "name": root.name,
        "codebaseVersion": info.get("codebase_version"),
        "robotType": info.get("robot_type"),
        "totalEpisodes": int(info.get("total_episodes", len(episodes))),
        "totalFrames": int(info.get("total_frames", sum(item["length"] for item in episodes))),
        "totalTasks": int(info.get("total_tasks", 0)),
        "fps": fps,
        "videoKeys": video_keys,
        "features": info.get("features", {}),
        "episodes": episodes,
    }


def create_dataset(source: Path, output: Path, selection_path: Path, media_mode: str) -> dict[str, Any]:
    info = validate_dataset(source)
    source = info.pop("_root")
    episode_files = info.pop("_episode_files")
    data_files = info.pop("_data_files")
    output = output.expanduser().resolve()
    if output.exists():
        raise FileExistsError(f"目标目录已经存在：{output}")
    if output == source or source in output.parents:
        raise ValueError("目标目录不能是源数据集本身，也不能位于源数据集内部")

    requested = json.loads(selection_path.read_text(encoding="utf-8")).get("episodes", [])
    selected = sorted({int(index) for index in requested})
    if not selected:
        raise ValueError("没有选择任何 episode")

    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{output.name}.building-", dir=output.parent))
    try:
        result = build_dataset(source, temporary, info, episode_files, data_files, selected, media_mode)
        os.replace(temporary, output)
        result["output"] = str(output)
        return result
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def build_dataset(
    source: Path,
    target: Path,
    info: dict[str, Any],
    episode_files: list[Path],
    data_files: list[Path],
    selected: list[int],
    media_mode: str,
) -> dict[str, Any]:
    episodes_all = pq.read_table(episode_files)
    available = {int(value) for value in episodes_all["episode_index"].to_pylist()}
    missing = [index for index in selected if index not in available]
    if missing:
        raise ValueError(f"选择中包含不存在的 episode：{missing[:20]}")

    old_positions = {int(value): position for position, value in enumerate(episodes_all["episode_index"].to_pylist())}
    selected_table = episodes_all.take(pa.array([old_positions[index] for index in selected], type=pa.int64()))
    old_to_new = {old: new for new, old in enumerate(selected)}

    # Narrow the data-file scan to shards actually referenced by the selected
    # episodes (captured before the chunk/file columns are renumbered below).
    relevant_files = list(data_files)
    columns = set(selected_table.column_names)
    data_path_tpl = str(info.get("data_path") or "")
    if data_path_tpl and {"data/chunk_index", "data/file_index"} <= columns:
        pairs = sorted(
            {
                (int(chunk), int(file_index))
                for chunk, file_index in zip(
                    selected_table["data/chunk_index"].to_pylist(),
                    selected_table["data/file_index"].to_pylist(),
                )
            }
        )
        candidates = [
            source / data_path_tpl.format(chunk_index=chunk, file_index=file_index)
            for chunk, file_index in pairs
        ]
        if all(path.is_file() for path in candidates):
            relevant_files = candidates
    lengths = [int(value) for value in selected_table["length"].to_pylist()]
    total_frames = sum(lengths)

    selected_table = replace_column(selected_table, "episode_index", list(range(len(selected))))
    from_indices: list[int] = []
    to_indices: list[int] = []
    cursor = 0
    for length in lengths:
        from_indices.append(cursor)
        cursor += length
        to_indices.append(cursor)
    selected_table = replace_column(selected_table, "dataset_from_index", from_indices)
    selected_table = replace_column(selected_table, "dataset_to_index", to_indices)
    if "data/chunk_index" in selected_table.column_names:
        selected_table = replace_column(selected_table, "data/chunk_index", [0] * len(selected))
    if "data/file_index" in selected_table.column_names:
        selected_table = replace_column(selected_table, "data/file_index", [0] * len(selected))

    write_episode_metadata(selected_table, target, int(info.get("chunks_size") or 1000))

    copy_static_metadata(source, target)
    written_frames = write_filtered_data(relevant_files, target, selected, old_to_new)
    if written_frames != total_frames:
        raise ValueError(
            f"过滤后的帧数与元数据不一致：data={written_frames}，meta={total_frames}；"
            "源数据集可能损坏或 episode length 不准确"
        )
    linked, copied = preserve_video_shards(source, target, info, selected_table, media_mode)

    new_info = dict(info)
    new_info["total_episodes"] = len(selected)
    new_info["total_frames"] = total_frames
    new_info["splits"] = {"train": f"0:{len(selected)}"}
    (target / "meta" / "info.json").write_text(
        json.dumps(new_info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    stats = aggregate_episode_stats(selected_table, lengths, total_frames)
    (target / "meta" / "stats.json").write_text(
        json.dumps(stats, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    manifest = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset": str(source),
        "selected_source_episodes": selected,
        "episode_index_mapping": {str(old): new for old, new in old_to_new.items()},
        "video_policy": "original encoded MP4 shards are preserved without transcoding",
        "media_mode_requested": media_mode,
        "hardlinked_files": linked,
        "copied_files": copied,
        "stats_note": "min/max/mean/std/count are merged from episode stats; quantiles are weighted episode estimates",
    }
    (target / "selection_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )

    return {
        "totalEpisodes": len(selected),
        "totalFrames": total_frames,
        "hardlinkedVideoFiles": linked,
        "copiedVideoFiles": copied,
    }


def copy_static_metadata(source: Path, target: Path) -> None:
    source_meta = source / "meta"
    target_meta = target / "meta"
    target_meta.mkdir(parents=True, exist_ok=True)
    dynamic = {"info.json", "stats.json", "episodes"}
    for item in source_meta.iterdir():
        if item.name in dynamic:
            continue
        destination = target_meta / item.name
        if item.is_dir():
            shutil.copytree(item, destination)
        else:
            shutil.copy2(item, destination)
    report = source / "conversion_report.txt"
    if report.is_file():
        shutil.copy2(report, target / report.name)


def write_episode_metadata(table: pa.Table, target: Path, chunk_size: int) -> None:
    chunk_size = max(1, chunk_size)
    for start in range(0, table.num_rows, chunk_size):
        chunk_index = start // chunk_size
        output_dir = target / "meta" / "episodes" / f"chunk-{chunk_index:03d}"
        output_dir.mkdir(parents=True, exist_ok=True)
        pq.write_table(
            table.slice(start, min(chunk_size, table.num_rows - start)),
            output_dir / "file-000.parquet",
            compression="zstd",
        )


def write_filtered_data(
    data_files: Iterable[Path], target: Path, selected: list[int], old_to_new: dict[int, int]
) -> int:
    output_dir = target / "data" / "chunk-000"
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / "file-000.parquet"
    writer: pq.ParquetWriter | None = None
    global_index = 0
    selected_values = pa.array(selected, type=pa.int64())
    try:
        for data_file in data_files:
            parquet_file = pq.ParquetFile(data_file)
            for row_group in range(parquet_file.num_row_groups):
                table = parquet_file.read_row_group(row_group)
                mask = pc.is_in(table["episode_index"], value_set=selected_values)
                table = table.filter(mask)
                if table.num_rows == 0:
                    continue
                mapped_episodes = [old_to_new[int(value)] for value in table["episode_index"].to_pylist()]
                table = replace_column(table, "episode_index", mapped_episodes)
                if "index" in table.column_names:
                    table = replace_column(table, "index", range(global_index, global_index + table.num_rows))
                global_index += table.num_rows
                if writer is None:
                    writer = pq.ParquetWriter(output_file, table.schema, compression="zstd")
                writer.write_table(table)
    finally:
        if writer is not None:
            writer.close()
    if writer is None:
        raise ValueError("所选 episode 没有找到对应的数据帧")
    return global_index


def preserve_video_shards(
    source: Path,
    target: Path,
    info: dict[str, Any],
    selected_table: pa.Table,
    media_mode: str,
) -> tuple[int, int]:
    video_keys = discover_video_keys(selected_table.column_names)
    referenced: set[str] = set()
    rows = selected_table.to_pylist()
    for row in rows:
        for key in video_keys:
            relative = format_video_path(
                info["video_path"],
                key,
                int(row[f"videos/{key}/chunk_index"]),
                int(row[f"videos/{key}/file_index"]),
            )
            referenced.add(relative)

    linked = 0
    copied = 0
    for relative in sorted(referenced):
        source_file = source / relative
        target_file = target / relative
        if not source_file.is_file():
            raise FileNotFoundError(f"缺少 episode 引用的视频文件：{source_file}")
        target_file.parent.mkdir(parents=True, exist_ok=True)
        if media_mode == "hardlink":
            try:
                os.link(source_file, target_file)
                linked += 1
                continue
            except OSError:
                pass
        shutil.copy2(source_file, target_file)
        copied += 1
    return linked, copied


def aggregate_episode_stats(table: pa.Table, lengths: list[int], total_frames: int) -> dict[str, Any]:
    grouped: dict[str, dict[str, list[Any]]] = {}
    for column in table.column_names:
        match = STAT_COLUMN.match(column)
        if not match:
            continue
        feature, metric = match.groups()
        grouped.setdefault(feature, {})[metric] = table[column].to_pylist()

    result: dict[str, Any] = {}
    for feature, metrics in grouped.items():
        counts = [scalar_count(value) for value in metrics.get("count", [])]
        if not counts or sum(counts) <= 0:
            continue
        feature_stats: dict[str, Any] = {}
        for metric in ("min", "max"):
            raw_rows = metrics.get(metric, [])
            rows = normalize_rows(raw_rows)
            if rows:
                function = min if metric == "min" else max
                combined = [function(values) for values in zip(*rows)]
                feature_stats[metric] = rebuild_like(first_value(raw_rows), iter(combined))
        raw_means = metrics.get("mean", [])
        means = normalize_rows(raw_means)
        if means:
            total_count = sum(counts)
            combined_mean = [
                sum(row[dimension] * count for row, count in zip(means, counts)) / total_count
                for dimension in range(len(means[0]))
            ]
            feature_stats["mean"] = rebuild_like(first_value(raw_means), iter(combined_mean))
            raw_stds = metrics.get("std", [])
            stds = normalize_rows(raw_stds)
            if stds and len(stds) == len(means):
                combined_std = [
                    math.sqrt(max(0.0, sum(
                        count * (std_row[dimension] ** 2 + (mean_row[dimension] - combined_mean[dimension]) ** 2)
                        for mean_row, std_row, count in zip(means, stds, counts)
                    ) / total_count))
                    for dimension in range(len(combined_mean))
                ]
                feature_stats["std"] = rebuild_like(first_value(raw_stds), iter(combined_std))
            feature_stats["count"] = [total_count]
        for metric in ("q01", "q10", "q50", "q90", "q99"):
            raw_rows = metrics.get(metric, [])
            rows = normalize_rows(raw_rows)
            if rows:
                total_count = sum(counts)
                combined = [
                    sum(row[dimension] * count for row, count in zip(rows, counts)) / total_count
                    for dimension in range(len(rows[0]))
                ]
                feature_stats[metric] = rebuild_like(first_value(raw_rows), iter(combined))
        result[feature] = feature_stats

    if total_frames:
        result["index"] = sequential_stats(total_frames)
        result["episode_index"] = repeated_value_stats(lengths)
    return result


def sequential_stats(count: int) -> dict[str, list[float | int]]:
    maximum = count - 1
    return {
        "min": [0],
        "max": [maximum],
        "mean": [maximum / 2],
        "std": [math.sqrt((count * count - 1) / 12) if count > 1 else 0.0],
        "count": [count],
        "q01": [maximum * 0.01],
        "q10": [maximum * 0.10],
        "q50": [maximum * 0.50],
        "q90": [maximum * 0.90],
        "q99": [maximum * 0.99],
    }


def repeated_value_stats(lengths: list[int]) -> dict[str, list[float | int]]:
    total = sum(lengths)
    weighted_mean = sum(index * length for index, length in enumerate(lengths)) / total
    variance = sum(length * (index - weighted_mean) ** 2 for index, length in enumerate(lengths)) / total

    def quantile(probability: float) -> int:
        threshold = probability * max(0, total - 1)
        cursor = 0
        for index, length in enumerate(lengths):
            cursor += length
            if cursor - 1 >= threshold:
                return index
        return len(lengths) - 1

    return {
        "min": [0],
        "max": [len(lengths) - 1],
        "mean": [weighted_mean],
        "std": [math.sqrt(variance)],
        "count": [total],
        "q01": [quantile(0.01)],
        "q10": [quantile(0.10)],
        "q50": [quantile(0.50)],
        "q90": [quantile(0.90)],
        "q99": [quantile(0.99)],
    }


def replace_column(table: pa.Table, name: str, values: Iterable[Any]) -> pa.Table:
    position = table.schema.get_field_index(name)
    if position < 0:
        return table
    field = table.schema.field(position)
    return table.set_column(position, field, pa.array(list(values), type=field.type))


def discover_video_keys(columns: Iterable[str]) -> list[str]:
    keys = set()
    for column in columns:
        match = VIDEO_COLUMN.match(column)
        if match:
            keys.add(match.group(1))
    return sorted(keys)


def format_video_path(template: str, key: str, chunk: int, file_index: int) -> str:
    return template.format(video_key=key, chunk_index=chunk, file_index=file_index)


def any_number_greater_than_zero(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, (list, tuple)):
        return any(any_number_greater_than_zero(item) for item in value)
    try:
        return float(value) > 0
    except (TypeError, ValueError):
        return False


def scalar_count(value: Any) -> int:
    if isinstance(value, (list, tuple)):
        return int(value[0]) if value else 0
    return int(value or 0)


def normalize_rows(rows: list[Any]) -> list[list[float]]:
    normalized: list[list[float]] = []
    for row in rows:
        if row is None:
            continue
        normalized.append(flatten_numbers(row))
    return normalized


def flatten_numbers(value: Any) -> list[float]:
    if isinstance(value, (list, tuple)):
        flattened: list[float] = []
        for item in value:
            flattened.extend(flatten_numbers(item))
        return flattened
    return [float(value)]


def rebuild_like(template: Any, values: Any) -> Any:
    if isinstance(template, (list, tuple)):
        return [rebuild_like(item, values) for item in template]
    return next(values)


def first_value(rows: list[Any]) -> Any:
    for row in rows:
        if row is not None:
            return row
    return []


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"{type(error).__name__}: {error}", file=sys.stderr)
        raise
