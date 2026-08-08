"""Phase-3 hardening tests: shared job store and labels store."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import signal
import sys

import pytest

import jobs_common
from convert import worker as convert_worker
from labels import store as labels_store
from qc import jobs as qc_jobs
from qc import worker as qc_worker


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


def test_worker_pid_guard_matches_a_complete_argv_item(monkeypatch) -> None:
    monkeypatch.setattr(jobs_common.os, "kill", lambda _pid, _signal: None)
    monkeypatch.setattr(
        jobs_common.Path,
        "read_bytes",
        lambda _path: b"python\0worker.py\0--job-id\0job-1234\0",
    )

    assert jobs_common.worker_alive({"jobId": "job-1234", "pid": 42}) is True
    assert jobs_common.worker_alive({"jobId": "job-12", "pid": 42}) is False


def test_queued_liveness_uses_latest_transition_timestamp() -> None:
    job = {
        "jobId": "relaunched",
        "status": "queued",
        "pid": None,
        "createdAt": "2000-01-01T00:00:00+00:00",
        "updatedAt": jobs_common.now_iso(),
    }

    assert jobs_common.refresh_job_liveness(job)["status"] == "queued"


@pytest.mark.parametrize("job_id", ["../escape", "a.b", "a/b", "合法", "", "x" * 129])
def test_job_ids_are_rejected_instead_of_silently_rewritten(tmp_path: Path, job_id: str) -> None:
    with pytest.raises(ValueError, match="非法 job id"):
        jobs_common.job_path(tmp_path, job_id)


def test_parallel_job_updates_do_not_drop_independent_fields(tmp_path: Path) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_common.write_job(
        jobs_dir,
        {"jobId": "parallel", "status": "running", "createdAt": jobs_common.now_iso()},
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        futures = [
            executor.submit(jobs_common.update_job, jobs_dir, "parallel", **{f"field{index}": index})
            for index in range(16)
        ]
        for future in futures:
            future.result()

    stored = jobs_common.read_job(jobs_dir, "parallel")
    assert stored is not None
    assert {key: stored[key] for key in (f"field{index}" for index in range(16))} == {
        f"field{index}": index for index in range(16)
    }


def test_update_job_returns_the_timestamp_that_was_persisted(tmp_path: Path) -> None:
    jobs_common.write_job(
        tmp_path,
        {"jobId": "timestamp", "status": "queued", "createdAt": jobs_common.now_iso()},
    )
    updated = jobs_common.update_job(tmp_path, "timestamp", status="running")
    stored = jobs_common.read_job(tmp_path, "timestamp")

    assert stored is not None
    assert stored["updatedAt"] == updated["updatedAt"]


def test_conditional_job_update_cannot_overwrite_pause(tmp_path: Path) -> None:
    jobs_common.write_job(
        tmp_path,
        {"jobId": "paused", "status": "paused", "createdAt": jobs_common.now_iso()},
    )

    unchanged = jobs_common.update_job_if_status(
        tmp_path,
        "paused",
        {"queued", "running"},
        status="running",
        progress=0.5,
    )

    assert unchanged["status"] == "paused"
    assert "progress" not in unchanged


def test_only_one_caller_can_claim_a_queued_worker_launch(tmp_path: Path) -> None:
    jobs_common.write_job(
        tmp_path,
        {"jobId": "launch", "status": "queued", "createdAt": jobs_common.now_iso()},
    )

    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(lambda _index: jobs_common.claim_job_launch(tmp_path, "launch"), range(16))
        )

    assert sum(claimed for claimed, _job in results) == 1
    stored = jobs_common.read_job(tmp_path, "launch")
    assert stored is not None and stored["launching"] is True


def test_job_listing_skips_payloads_with_mismatched_identity(tmp_path: Path) -> None:
    jobs_common.write_job(
        tmp_path,
        {"jobId": "valid", "status": "completed", "createdAt": jobs_common.now_iso()},
    )
    (tmp_path / "wrong.json").write_text('{"jobId":"another","status":"running"}', encoding="utf-8")

    assert [job["jobId"] for job in jobs_common.list_jobs(tmp_path)] == ["valid"]


def test_qc_launcher_keeps_pid_when_pause_races_with_spawn(tmp_path: Path, monkeypatch) -> None:
    jobs_dir = tmp_path / "jobs"
    jobs_common.write_job(
        jobs_dir,
        {
            "jobId": "qc-launch",
            "kind": "qc",
            "status": "queued",
            "createdAt": jobs_common.now_iso(),
            "logPath": str(jobs_dir / "qc-launch.log"),
        },
    )

    def _spawn(*_args, **_kwargs):
        jobs_common.update_job(jobs_dir, "qc-launch", status="paused")
        return type("Process", (), {"pid": 12345})()

    monkeypatch.setattr(qc_jobs.subprocess, "Popen", _spawn)
    launched = qc_jobs.launch_detached_worker("qc-launch", jobs_dir)

    assert launched["status"] == "paused"
    assert launched["pid"] == 12345
    assert launched["launching"] is False


def test_qc_worker_retries_completion_when_pause_wins_cas(tmp_path: Path, monkeypatch) -> None:
    jobs_dir = tmp_path / "jobs"
    job_id = "qc-finalize"
    report_path = tmp_path / "report.qc.sqlite3"
    jobs_common.write_job(
        jobs_dir,
        {
            "jobId": job_id,
            "kind": "qc",
            "status": "queued",
            "pid": None,
            "createdAt": jobs_common.now_iso(),
            "dataset": str(tmp_path / "dataset"),
            "reportPath": str(report_path),
            "config": {},
            "useCache": False,
        },
    )

    def _scan(*_args, **_kwargs):
        return {"cached": False, "totalEpisodes": 1, "reportPath": str(report_path)}

    original_update = qc_worker.update_job_if_status
    completion_attempts = 0

    def _pause_before_first_completion(root, target_job_id, expected_statuses, **fields):
        nonlocal completion_attempts
        if fields.get("status") == "completed":
            completion_attempts += 1
            if completion_attempts == 1:
                jobs_common.update_job(root, target_job_id, status="paused")
        return original_update(root, target_job_id, expected_statuses, **fields)

    def _resume_during_wait(_seconds: float) -> None:
        jobs_common.update_job(jobs_dir, job_id, status="running")

    monkeypatch.setattr(qc_worker, "run_scan", _scan)
    monkeypatch.setattr(qc_worker, "update_job_if_status", _pause_before_first_completion)
    monkeypatch.setattr(qc_worker.time, "sleep", _resume_during_wait)
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "--job-id", job_id, "--jobs-dir", str(jobs_dir)],
    )

    assert qc_worker.main() == 0
    completed = jobs_common.read_job(jobs_dir, job_id)
    assert completion_attempts == 2
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["progress"] == 1.0


def test_convert_worker_progress_completes_before_result_exists(
    tmp_path: Path, monkeypatch
) -> None:
    """A progress callback must not reference the eventual conversion result."""
    jobs_dir = jobs_common.ensure_jobs_dir(tmp_path / "jobs")
    job = {
        "jobId": "convert-progress",
        "kind": "convert",
        "dataset": str(tmp_path / "source"),
        "output": str(tmp_path / "output"),
        "targetFormat": "lerobot_v3",
        "status": "queued",
        "createdAt": jobs_common.now_iso(),
    }
    jobs_common.write_job(jobs_dir, job)

    def fake_convert(*_args, progress_callback, **_kwargs):
        progress_callback({"current": 1, "total": 2, "progress": 0.5, "message": "half"})
        return {"episodes": 2, "output": str(tmp_path / "output")}

    monkeypatch.setattr(convert_worker, "convert_dataset", fake_convert)
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "--job-id", job["jobId"], "--jobs-dir", str(jobs_dir)],
    )

    assert convert_worker.main() == 0
    completed = jobs_common.read_job(jobs_dir, job["jobId"])
    assert completed is not None
    assert completed["status"] == "completed"
    assert completed["current"] == completed["total"] == 2


def test_convert_worker_sigterm_preserves_cancelled_status(
    tmp_path: Path, monkeypatch
) -> None:
    jobs_dir = jobs_common.ensure_jobs_dir(tmp_path / "jobs")
    job = {
        "jobId": "convert-cancel",
        "kind": "convert",
        "dataset": str(tmp_path / "source"),
        "output": str(tmp_path / "output"),
        "targetFormat": "mcap",
        "status": "queued",
        "createdAt": jobs_common.now_iso(),
    }
    jobs_common.write_job(jobs_dir, job)
    previous_sigterm = signal.getsignal(signal.SIGTERM)

    def fake_convert(*_args, **_kwargs):
        jobs_common.update_job(jobs_dir, job["jobId"], status="cancelled")
        handler = signal.getsignal(signal.SIGTERM)
        assert callable(handler)
        handler(signal.SIGTERM, None)

    monkeypatch.setattr(convert_worker, "convert_dataset", fake_convert)
    monkeypatch.setattr(
        sys,
        "argv",
        ["worker", "--job-id", job["jobId"], "--jobs-dir", str(jobs_dir)],
    )

    assert convert_worker.main() == 0
    cancelled = jobs_common.read_job(jobs_dir, job["jobId"])
    assert cancelled is not None
    assert cancelled["status"] == "cancelled"
    assert signal.getsignal(signal.SIGTERM) == previous_sigterm


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


def test_parallel_label_saves_use_distinct_temporary_files(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"

    def _save(index: int) -> None:
        labels_store.save_labels(
            path,
            [{"target": "episode", "episode_index": index, "note": f"row-{index}"}],
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_save, range(24)))

    rows = labels_store.load_labels(path)
    assert len(rows) == 1
    assert rows[0]["note"].startswith("row-")
    assert not list(tmp_path.glob(".labels.jsonl.tmp-*"))
