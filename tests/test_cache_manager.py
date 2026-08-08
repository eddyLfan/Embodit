"""Unified cache layout, migration, and retention tests."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from cache_manager import DAY, CacheLayout, cleanup, migrate_legacy


def _write(path: Path, content: str = "data") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def _age(path: Path, now: float, days: int) -> None:
    stamp = now - days * DAY
    os.utime(path, (stamp, stamp))


def test_migrate_legacy_layout_and_rewrite_job_paths(tmp_path: Path) -> None:
    project = tmp_path / "project"
    cache = project / ".embodit_cache"
    temp = tmp_path / "tmp"
    layout = CacheLayout.under(cache)
    old_jobs = project / ".augment_jobs"
    old_previews = project / ".augment_previews"
    old_report = cache / "qc" / "dataset" / "scan.qc.sqlite3"
    job = {
        "jobId": "abc",
        "status": "completed",
        "logPath": str(old_jobs / "abc.log"),
        "previewDir": str(old_previews / "abc"),
    }
    _write(old_jobs / "abc.json", json.dumps(job))
    _write(old_jobs / "abc.log")
    _write(old_previews / "abc" / "meta.json")
    _write(old_report)
    _write(temp / "embody-hdf5-video" / "one.mp4")

    result = migrate_legacy(layout, project_root=project, temp_root=temp)

    assert result["moved"]
    assert (layout.augment_jobs / "abc.json").is_file()
    assert (layout.augment_previews / "abc" / "meta.json").is_file()
    assert (layout.qc_reports / "dataset" / "scan.qc.sqlite3").is_file()
    assert (layout.hdf5_media / "one.mp4").is_file()
    assert not old_jobs.exists() and not old_jobs.is_symlink()
    assert not old_previews.exists() and not old_previews.is_symlink()
    rewritten = json.loads((layout.augment_jobs / "abc.json").read_text(encoding="utf-8"))
    assert rewritten["logPath"] == str(layout.augment_jobs / "abc.log")
    assert rewritten["previewDir"] == str(layout.augment_previews / "abc")


def test_migrate_removes_old_compatibility_links(tmp_path: Path) -> None:
    project = tmp_path / "project"
    layout = CacheLayout.under(project / ".embodit_cache")
    _write(layout.augment_jobs / "existing.json", "{}")
    _write(layout.qc_jobs / "existing.json", "{}")
    old_jobs = project / ".augment_jobs"
    old_qc_jobs = layout.root / "qc_jobs"
    old_jobs.symlink_to(layout.augment_jobs, target_is_directory=True)
    old_qc_jobs.symlink_to(layout.qc_jobs, target_is_directory=True)

    result = migrate_legacy(layout, project_root=project, temp_root=tmp_path / "tmp")

    assert not old_jobs.exists() and not old_jobs.is_symlink()
    assert not old_qc_jobs.exists() and not old_qc_jobs.is_symlink()
    assert result["removedLegacyLinks"] == [str(old_jobs), str(old_qc_jobs)]
    assert (layout.augment_jobs / "existing.json").is_file()
    assert (layout.qc_jobs / "existing.json").is_file()


def test_auto_cleanup_respects_retention_and_references(tmp_path: Path, monkeypatch) -> None:
    layout = CacheLayout.under(tmp_path / "cache")
    now = time.time()
    monkeypatch.setenv("EMBODIT_PREVIEW_TTL_DAYS", "7")
    monkeypatch.setenv("EMBODIT_MEDIA_TTL_DAYS", "7")
    monkeypatch.setenv("EMBODIT_SAM_CACHE_TTL_DAYS", "30")
    monkeypatch.setenv("EMBODIT_JOB_TTL_DAYS", "30")
    monkeypatch.setenv("EMBODIT_QC_REPORTS_PER_DATASET", "1")

    old_media = _write(layout.hdf5_media / "old.mp4")
    fresh_media = _write(layout.hdf5_media / "fresh.mp4")
    old_sam = _write(layout.sam_tracks / "old.npz")
    _age(old_media, now, 8)
    _age(old_sam, now, 31)

    terminal_job = _write(
        layout.convert_jobs / "done.json",
        json.dumps({"jobId": "done", "status": "completed"}),
    )
    terminal_log = _write(layout.convert_jobs / "done.log")
    _age(terminal_job, now, 31)
    _age(terminal_log, now, 31)

    preview_job = _write(
        layout.augment_jobs / "preview.json",
        json.dumps({"jobId": "preview", "status": "completed", "mode": "preview"}),
    )
    preview_asset = _write(layout.augment_previews / "preview" / "meta.json")
    _age(preview_job, now, 8)
    _age(preview_asset, now, 8)

    report_dir = layout.qc_reports / "dataset"
    newest = _write(report_dir / "new.qc.sqlite3")
    referenced = _write(report_dir / "referenced.qc.sqlite3")
    removable = _write(report_dir / "old.qc.sqlite3")
    _age(referenced, now, 2)
    _age(removable, now, 3)
    _write(
        layout.qc_jobs / "active.json",
        json.dumps(
            {"jobId": "active", "status": "running", "reportPath": str(referenced)}
        ),
    )

    result = cleanup("auto", layout=layout, now=now)

    assert result["removed"] >= 7
    assert not old_media.exists() and fresh_media.exists()
    assert not old_sam.exists()
    assert not terminal_job.exists() and not terminal_log.exists()
    assert not preview_job.exists() and not preview_asset.exists()
    assert newest.exists() and referenced.exists() and not removable.exists()


def test_dry_run_does_not_create_or_delete(tmp_path: Path) -> None:
    layout = CacheLayout.under(tmp_path / "missing")
    result = cleanup("all", dry_run=True, layout=layout)
    assert result["removed"] == 0
    assert not layout.root.exists()


def test_clean_all_preserves_unknown_root_files(tmp_path: Path) -> None:
    layout = CacheLayout.under(tmp_path / "cache")
    managed = _write(layout.media / "hdf5" / "video.mp4")
    unrelated = _write(layout.root / "keep-me.txt")
    cleanup("all", layout=layout)
    assert not managed.exists()
    assert unrelated.exists()
