"""Phase-3 hardening tests: shared job store and labels store."""

from __future__ import annotations

from pathlib import Path

import jobs_common
from labels import store as labels_store


def test_job_roundtrip_and_cancel_without_worker(tmp_path: Path) -> None:
    jobs_dir = jobs_common.ensure_jobs_dir(tmp_path / "jobs")
    job = {
        "jobId": "abc123",
        "status": "running",
        "pid": None,
        "createdAt": jobs_common.now_iso(),
    }
    jobs_common.write_job(jobs_dir, job)
    loaded = jobs_common.read_job(jobs_dir, "abc123")
    assert loaded is not None and loaded["status"] == "running"

    cancelled = jobs_common.cancel_job(jobs_dir, "abc123")
    assert cancelled["status"] == "cancelled"

    # Terminal jobs are left untouched by another cancel.
    again = jobs_common.cancel_job(jobs_dir, "abc123")
    assert again["status"] == "cancelled"

    assert jobs_common.delete_job(jobs_dir, "abc123") is True
    assert jobs_common.read_job(jobs_dir, "abc123") is None


def test_refresh_liveness_marks_dead_pid_failed(tmp_path: Path) -> None:
    job = {"jobId": "dead", "status": "running", "pid": 2**22 + 12345, "createdAt": jobs_common.now_iso()}
    patched = jobs_common.refresh_job_liveness(job)
    assert patched["status"] == "failed"


def test_labels_tolerate_corrupt_lines_and_upsert_identity(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text(
        '{"target": "episode", "episode_index": 0, "note": "ok"}\n'
        "{corrupt json line\n"
        '{"target": "interval", "episode_index": 0, "start_s": 1.0, "end_s": 2.0}\n',
        encoding="utf-8",
    )
    rows = labels_store.load_labels(path)
    assert len(rows) == 2  # corrupt line skipped

    # Upsert replaces the episode-level record instead of appending a duplicate.
    updated = labels_store.upsert_label(path, {"target": "episode", "episode_index": 0, "note": "new"})
    episode_rows = [r for r in updated if r.get("target") == "episode"]
    assert len(episode_rows) == 1 and episode_rows[0]["note"] == "new"

    # Delete removes by identity (interval time span).
    remaining = labels_store.delete_label(
        path, {"target": "interval", "episode_index": 0, "start_s": 1.0, "end_s": 2.0}
    )
    assert all(r.get("target") != "interval" for r in remaining)
