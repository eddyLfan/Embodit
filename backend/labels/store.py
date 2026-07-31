"""Label schema and JSONL store."""

from __future__ import annotations

import fcntl
import json
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schema import PRESET_TAGS, validate_label


def default_labels_path(dataset: Path) -> Path:
    dataset = dataset.expanduser().resolve()
    if dataset.is_file():
        return dataset.with_name(dataset.name + ".labels.jsonl")
    return dataset / "labels.jsonl"


@contextmanager
def _file_lock(path: Path) -> Iterator[None]:
    """Exclusive advisory lock guarding read-modify-write cycles."""
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("w") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def load_labels(path: Path) -> list[dict[str, Any]]:
    path = path.expanduser().resolve()
    if not path.is_file():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            # Tolerate corrupt lines instead of failing the whole file.
            continue
    return rows


def save_labels(path: Path, labels: list[dict[str, Any]]) -> Path:
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    validated = [validate_label(item) for item in labels]
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    with temporary.open("w", encoding="utf-8") as handle:
        for item in validated:
            handle.write(json.dumps(item, ensure_ascii=False) + "\n")
    os.replace(temporary, path)
    return path


def identity(item: dict[str, Any]) -> tuple:
    """Stable identity used by upsert/delete to match existing records."""
    target = item.get("target")
    episode_index = item.get("episode_index")
    if target == "interval":
        # One episode may have many intervals; identity is time span only.
        start = item.get("start_s")
        end = item.get("end_s")
        return (
            "interval",
            episode_index,
            None if start is None else round(float(start), 3),
            None if end is None else round(float(end), 3),
        )
    if target == "frame":
        return (
            "frame",
            episode_index,
            item.get("frame_index"),
            None if item.get("start_s") is None else round(float(item["start_s"]), 3),
        )
    # episode-level: one record per episode
    return ("episode", episode_index, None, None)


def upsert_label(path: Path, label: dict[str, Any]) -> list[dict[str, Any]]:
    label = validate_label(label)
    path = path.expanduser().resolve()
    with _file_lock(path):
        labels = load_labels(path)
        new_id = identity(label)
        kept = [item for item in labels if identity(item) != new_id]
        kept.append(label)
        save_labels(path, kept)
    return kept


def delete_label(path: Path, label: dict[str, Any]) -> list[dict[str, Any]]:
    """Delete a label matching the same identity rules as upsert."""
    probe = validate_label(label)
    path = path.expanduser().resolve()
    with _file_lock(path):
        labels = load_labels(path)
        target_id = identity(probe)
        kept = [item for item in labels if identity(item) != target_id]
        save_labels(path, kept)
    return kept


def labels_for_episode(labels: list[dict[str, Any]], episode_index: int) -> list[dict[str, Any]]:
    return [item for item in labels if int(item.get("episode_index", -1)) == episode_index]


def preset_tags() -> list[str]:
    return list(PRESET_TAGS)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
