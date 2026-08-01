"""Cheap source fingerprints used for report staleness and cache reuse."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any


def dataset_id(path: Path) -> str:
    resolved = str(path.expanduser().resolve())
    return hashlib.sha256(resolved.encode("utf-8")).hexdigest()[:24]


def _stat_payload(path: Path) -> dict[str, Any]:
    try:
        stat = path.stat()
        return {"path": str(path), "size": stat.st_size, "mtimeNs": stat.st_mtime_ns}
    except OSError:
        return {"path": str(path), "missing": True}


def dataset_fingerprint(dataset: Path, view: Any) -> str:
    dataset = dataset.expanduser().resolve()
    files: dict[str, dict[str, Any]] = {}
    if dataset.is_file():
        files[str(dataset)] = _stat_payload(dataset)
        root = dataset.parent
    else:
        root = dataset
        for relative in ("meta/info.json", "meta/episodes.jsonl", "meta/tasks.jsonl"):
            candidate = root / relative
            if candidate.exists():
                files[str(candidate)] = _stat_payload(candidate)
    episodes = []
    for ep in view.episodes:
        cameras = {}
        for key, cam in ep.cameras.items():
            item: dict[str, Any] = {
                "kind": cam.kind,
                "path": cam.path,
                "topic": cam.topic,
                "from": cam.from_timestamp,
                "to": cam.to_timestamp,
            }
            if cam.path:
                target = root / cam.path
                files.setdefault(str(target), _stat_payload(target))
            cameras[key] = item
        extras = {}
        for key in ("hdf5File", "mcapFile", "demoKey"):
            value = ep.extras.get(key)
            if value is not None:
                extras[key] = str(value)
                if key.endswith("File"):
                    target = Path(value)
                    files.setdefault(str(target), _stat_payload(target))
        episodes.append(
            {
                "index": ep.episode_index,
                "length": ep.length,
                "duration": ep.duration,
                "cameras": cameras,
                "extras": extras,
            }
        )
    payload = {
        "dataset": str(dataset),
        "format": view.format_id,
        "fps": view.fps,
        "features": view.features,
        "episodes": episodes,
        "files": sorted(files.values(), key=lambda row: row["path"]),
    }
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()
