"""QC cache and job path helpers."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import settings

from .fingerprint import dataset_id


def cache_root() -> Path:
    root = settings.QC_REPORT_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def jobs_dir() -> Path:
    root = settings.QC_JOBS_DIR
    root.mkdir(parents=True, exist_ok=True)
    return root


def report_path(dataset: Path, scan_id: str, config_hash: str) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = cache_root() / dataset_id(dataset)
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"scan-{stamp}-{scan_id[:8]}-{config_hash[:6]}.qc.sqlite3"


def find_report(scan_id: str) -> Path | None:
    for path in cache_root().glob("*/*.qc.sqlite3"):
        try:
            from .store import scan_info

            if scan_info(path).get("scan_id") == scan_id or scan_info(path).get("scanId") == scan_id:
                return path
        except Exception:  # noqa: BLE001
            continue
    return None
