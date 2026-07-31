"""Subset export orchestration (same-format or convert-on-export)."""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from datasets.registry import open_dataset
from datasets.view import FORMAT_LABELS
from convert.pipeline import convert_dataset


DECISION_PASS = "pass"
DECISION_REVIEW = "review"
DECISION_QUARANTINE = "quarantine"

LEGACY_STATE_MAP = {
    "keep": DECISION_PASS,
    "exclude": DECISION_QUARANTINE,
    "pending": DECISION_REVIEW,
}


def normalize_decision(value: str) -> str:
    value = (value or "").strip().lower()
    if value in LEGACY_STATE_MAP:
        return LEGACY_STATE_MAP[value]
    if value in {DECISION_PASS, DECISION_REVIEW, DECISION_QUARANTINE}:
        return value
    return DECISION_REVIEW


def episodes_for_export(states: dict[str, str], include_review: bool = False) -> list[int]:
    selected: list[int] = []
    for key, value in states.items():
        decision = normalize_decision(value)
        if decision == DECISION_PASS or (include_review and decision == DECISION_REVIEW):
            selected.append(int(key))
    return sorted(selected)


def export_dataset(
    source: Path,
    output: Path,
    episode_indices: list[int],
    *,
    target_format: str | None = None,
    media_mode: str = "hardlink",
    mapping: dict[str, Any] | None = None,
    labels_path: Path | None = None,
    progress_callback=None,
) -> dict[str, Any]:
    adapter = open_dataset(source)
    source_format = adapter.format_id
    target = target_format or source_format
    episode_indices = sorted({int(i) for i in episode_indices})
    if not episode_indices:
        raise ValueError("没有可导出的 episode（需要 pass 决策）")

    if target == source_format:
        if progress_callback is not None:
            try:
                progress_callback({"stage": "export", "progress": 0.2, "message": "同格式子集导出…"})
            except Exception:  # noqa: BLE001
                pass
        result = adapter.export_subset(output, episode_indices, media_mode=media_mode, mapping=mapping)
    else:
        result = convert_dataset(
            source,
            output,
            target_format=target,
            episode_indices=episode_indices,
            mapping={**(mapping or {}), "media_mode": media_mode},
            progress_callback=progress_callback,
        )

    manifest = {
        "version": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "source_dataset": str(Path(source).expanduser().resolve()),
        "source_format": source_format,
        "source_format_label": FORMAT_LABELS.get(source_format, source_format),
        "target_format": target,
        "target_format_label": FORMAT_LABELS.get(target, target),
        "selected_source_episodes": episode_indices,
        "media_mode_requested": media_mode,
        "result": result,
    }
    out = Path(result.get("output") or output).expanduser().resolve()
    if out.is_dir():
        manifest_path = out / "selection_manifest.json"
    else:
        manifest_path = out.with_name(out.name + ".selection_manifest.json")
    # Merge with any adapter-written manifest: export-level fields win, the
    # adapter's extra fields are kept for keys we do not set ourselves.
    if manifest_path.is_file():
        try:
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                manifest = {**existing, **manifest}
        except json.JSONDecodeError:
            pass
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if labels_path and labels_path.is_file():
        dest = manifest_path.parent / "labels.jsonl"
        shutil.copy2(labels_path, dest)
        manifest["labels_copied"] = str(dest)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result["manifest"] = str(manifest_path)
    result["sourceFormat"] = source_format
    result["targetFormat"] = target
    return result
