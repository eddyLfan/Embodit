"""Format detection for supported dataset layouts."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any

from .view import (
    FORMAT_HDF5,
    FORMAT_LABELS,
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_MCAP,
)


def collect_mcap_files(path: Path) -> list[Path]:
    """Collect episode MCAP files for a path.

    Supported layouts:
    - single ``*.mcap`` file
    - directory with one or more top-level ``*.mcap``
    - directory with one nesting level of shard dirs (``*/*.mcap``), e.g. ``clean_bowl/00001/*.mcap``
    """
    path = path.expanduser().resolve()
    if path.is_file():
        return [path] if path.suffix.lower() == ".mcap" else []
    if not path.is_dir():
        return []
    top = sorted(path.glob("*.mcap"))
    if top:
        return top
    return sorted(path.glob("*/*.mcap"))


def collect_hdf5_files(path: Path) -> list[Path]:
    """Collect HDF5 files for a path (single file, top-level dir, or one nested level)."""
    path = path.expanduser().resolve()
    if path.is_file():
        return [path] if path.suffix.lower() in {".hdf5", ".h5"} else []
    if not path.is_dir():
        return []
    top = sorted(path.glob("*.hdf5")) + sorted(path.glob("*.h5"))
    if top:
        return sorted(top, key=lambda p: p.name.lower())
    nested = sorted(path.glob("*/*.hdf5")) + sorted(path.glob("*/*.h5"))
    return sorted(nested, key=lambda p: p.name.lower())


# Short-TTL detection cache: directory listings re-probe the same paths on
# every request; format rarely changes within a few seconds.
_DETECT_TTL_S = 10.0
_DETECT_CACHE: dict[str, tuple[float, str | None]] = {}
_DETECT_LOCK = threading.Lock()


def detect_format(path: Path) -> str | None:
    path = path.expanduser().resolve()
    key = str(path)
    now = time.monotonic()
    with _DETECT_LOCK:
        hit = _DETECT_CACHE.get(key)
        if hit is not None and now - hit[0] < _DETECT_TTL_S:
            return hit[1]
    fmt = _detect_format_uncached(path)
    with _DETECT_LOCK:
        if len(_DETECT_CACHE) > 4096:
            _DETECT_CACHE.clear()
        _DETECT_CACHE[key] = (now, fmt)
    return fmt


def _detect_format_uncached(path: Path) -> str | None:
    if path.is_file():
        suffix = path.suffix.lower()
        if suffix in {".hdf5", ".h5"}:
            return FORMAT_HDF5
        if suffix == ".mcap":
            return FORMAT_MCAP
        return None

    if not path.is_dir():
        return None

    info_path = path / "meta" / "info.json"
    if info_path.is_file():
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        version = str(info.get("codebase_version", ""))
        if version in {"v3.0", "v3"}:
            return FORMAT_LEROBOT_V3
        if version in {"v2.1", "v2.0"}:
            return FORMAT_LEROBOT_V21
        # Prefer layout heuristics
        if (path / "meta" / "episodes").is_dir():
            return FORMAT_LEROBOT_V3
        if (path / "meta" / "episodes.jsonl").is_file():
            return FORMAT_LEROBOT_V21

    # Directory of HDF5 / MCAP files (multi-file dirs count as one dataset)
    if collect_hdf5_files(path):
        return FORMAT_HDF5
    if collect_mcap_files(path):
        return FORMAT_MCAP
    return None


def resolve_dataset_path(path: Path) -> Path:
    """Normalize dataset path; keep multi-file HDF5/MCAP directories intact."""
    path = path.expanduser().resolve()
    if path.is_file():
        return path
    fmt = detect_format(path)
    if fmt == FORMAT_HDF5:
        files = collect_hdf5_files(path)
        top = list(path.glob("*.hdf5")) + list(path.glob("*.h5"))
        if len(top) == 1 and len(files) == 1:
            return top[0]
    if fmt == FORMAT_MCAP:
        files = collect_mcap_files(path)
        # Only collapse to a single file when the directory literally holds one top-level mcap.
        top = list(path.glob("*.mcap"))
        if len(top) == 1 and len(files) == 1:
            return top[0]
    return path


def _labels_brief(dataset_path: Path) -> dict[str, Any] | None:
    """Cheap summary of the sidecar labels.jsonl next to a dataset."""
    if dataset_path.is_file():
        labels_path = dataset_path.with_name(dataset_path.name + ".labels.jsonl")
    else:
        labels_path = dataset_path / "labels.jsonl"
    if not labels_path.is_file():
        return None
    count = 0
    episodes: set[int] = set()
    tags: dict[str, int] = {}
    try:
        with labels_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                count += 1
                try:
                    episodes.add(int(row.get("episode_index")))
                except (TypeError, ValueError):
                    pass
                for tag in row.get("tags") or []:
                    tags[str(tag)] = tags.get(str(tag), 0) + 1
    except OSError:
        return None
    if count == 0:
        return None
    top_tags = sorted(tags.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "labelCount": count,
        "labeledEpisodes": len(episodes),
        "topTags": [{"tag": tag, "count": n} for tag, n in top_tags],
    }


def dataset_brief(dataset_path: Path, fmt: str | None) -> dict[str, Any] | None:
    """Lightweight dataset summary for directory listings.

    Reads only tiny sidecar files (meta/info.json, labels.jsonl); never opens
    the actual data so listing large folders stays fast.
    """
    if fmt is None:
        return None
    brief: dict[str, Any] = {}
    if fmt in {FORMAT_LEROBOT_V21, FORMAT_LEROBOT_V3} and dataset_path.is_dir():
        info_path = dataset_path / "meta" / "info.json"
        try:
            info = json.loads(info_path.read_text(encoding="utf-8"))
            for src, dst in (
                ("total_episodes", "totalEpisodes"),
                ("total_frames", "totalFrames"),
                ("fps", "fps"),
                ("robot_type", "robotType"),
            ):
                if info.get(src) is not None:
                    brief[dst] = info[src]
        except (OSError, json.JSONDecodeError):
            pass
    labels = _labels_brief(dataset_path)
    if labels:
        brief["labels"] = labels
    return brief or None


def list_entries(directory: Path) -> list[dict[str, Any]]:
    directory = directory.expanduser().resolve()
    entries: list[dict[str, Any]] = []
    children = sorted(
        (item for item in directory.iterdir() if not item.name.startswith(".")),
        key=lambda item: (not item.is_dir(), item.name.lower()),
    )
    sibling_mcaps = [item for item in children if item.is_file() and item.suffix.lower() == ".mcap"]
    sibling_hdf5 = [
        item for item in children if item.is_file() and item.suffix.lower() in {".hdf5", ".h5"}
    ]
    multi_mcap_folder = len(sibling_mcaps) > 1
    multi_hdf5_folder = len(sibling_hdf5) > 1
    for item in children:
        try:
            if item.is_dir():
                fmt = detect_format(item)
                entries.append(
                    {
                        "name": item.name,
                        "path": str(item),
                        "isDir": True,
                        "isDataset": fmt is not None,
                        "format": fmt,
                        "formatLabel": FORMAT_LABELS.get(fmt or "", ""),
                        "brief": dataset_brief(item, fmt),
                    }
                )
            elif item.suffix.lower() in {".hdf5", ".h5", ".mcap"}:
                # In a multi-file folder, the directory is the dataset — do not
                # badge every episode file as its own dataset.
                if item.suffix.lower() == ".mcap" and multi_mcap_folder:
                    entries.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "isDir": False,
                            "isDataset": False,
                            "format": FORMAT_MCAP,
                            "formatLabel": FORMAT_LABELS[FORMAT_MCAP],
                        }
                    )
                    continue
                if item.suffix.lower() in {".hdf5", ".h5"} and multi_hdf5_folder:
                    entries.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "isDir": False,
                            "isDataset": False,
                            "format": FORMAT_HDF5,
                            "formatLabel": FORMAT_LABELS[FORMAT_HDF5],
                        }
                    )
                    continue
                fmt = detect_format(item)
                entries.append(
                    {
                        "name": item.name,
                        "path": str(item),
                        "isDir": False,
                        "isDataset": fmt is not None,
                        "format": fmt,
                        "formatLabel": FORMAT_LABELS.get(fmt or "", ""),
                        "brief": dataset_brief(item, fmt),
                    }
                )
        except PermissionError:
            continue
    return entries
