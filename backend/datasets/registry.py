"""Adapter registry."""

from __future__ import annotations

import threading
from collections import OrderedDict
from pathlib import Path

from .base import DatasetAdapter, DatasetWriter
from .detect import detect_format, resolve_dataset_path
from .hdf5_robomimic import Hdf5Adapter, Hdf5Writer
from .lerobot_v21 import LeRobotV21Adapter, LeRobotV21Writer
from .lerobot_v3 import LeRobotV3Adapter, LeRobotV3Writer
from .mcap_adapter import McapAdapter, McapWriter
from .view import (
    FORMAT_HDF5,
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_MCAP,
)

_ADAPTERS: dict[str, type[DatasetAdapter]] = {
    FORMAT_LEROBOT_V3: LeRobotV3Adapter,
    FORMAT_LEROBOT_V21: LeRobotV21Adapter,
    FORMAT_HDF5: Hdf5Adapter,
    FORMAT_MCAP: McapAdapter,
}

_WRITERS: dict[str, type[DatasetWriter]] = {
    FORMAT_LEROBOT_V3: LeRobotV3Writer,
    FORMAT_LEROBOT_V21: LeRobotV21Writer,
    FORMAT_HDF5: Hdf5Writer,
    FORMAT_MCAP: McapWriter,
}


def get_adapter(format_id: str) -> type[DatasetAdapter]:
    if format_id not in _ADAPTERS:
        raise ValueError(f"不支持的格式：{format_id}")
    return _ADAPTERS[format_id]


def get_writer(format_id: str) -> DatasetWriter:
    if format_id not in _WRITERS:
        raise ValueError(f"不支持的写入格式：{format_id}")
    return _WRITERS[format_id]()


_ADAPTER_CACHE: "OrderedDict[tuple[str, str, int], DatasetAdapter]" = OrderedDict()
_ADAPTER_CACHE_LOCK = threading.Lock()
_ADAPTER_CACHE_SIZE = 16


def _dataset_mtime_ns(path: Path) -> int:
    """Cheap change signal: mtime of the dataset entry point (info.json for LeRobot)."""
    try:
        if path.is_file():
            return path.stat().st_mtime_ns
        best = path.stat().st_mtime_ns
        info = path / "meta" / "info.json"
        if info.is_file():
            best = max(best, info.stat().st_mtime_ns)
        return best
    except OSError:
        return 0


def open_dataset(path: Path, format_id: str | None = None) -> DatasetAdapter:
    resolved = resolve_dataset_path(path)
    fmt = format_id or detect_format(resolved)
    if not fmt:
        # try original path if resolve changed it
        fmt = detect_format(path)
        resolved = path.expanduser().resolve()
    if not fmt:
        raise ValueError(f"无法识别数据集格式：{path}")

    # Reuse adapters across API calls (keyed by path + mtime) so expensive
    # per-dataset scans (MCAP full-message scan, v21 directory walk, …) run
    # once instead of on every request.
    key = (str(resolved), fmt, _dataset_mtime_ns(resolved))
    with _ADAPTER_CACHE_LOCK:
        cached = _ADAPTER_CACHE.get(key)
        if cached is not None:
            _ADAPTER_CACHE.move_to_end(key)
            return cached
    adapter = get_adapter(fmt)(resolved)
    with _ADAPTER_CACHE_LOCK:
        # Drop stale entries for the same path (older mtimes).
        for stale in [k for k in _ADAPTER_CACHE if k[0] == str(resolved) and k != key]:
            _ADAPTER_CACHE.pop(stale, None)
        _ADAPTER_CACHE[key] = adapter
        while len(_ADAPTER_CACHE) > _ADAPTER_CACHE_SIZE:
            _ADAPTER_CACHE.popitem(last=False)
    return adapter
