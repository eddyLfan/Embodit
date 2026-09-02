"""Shared persistent job store for detached workers (convert / QC).

Both job families keep one JSON file per job under a jobs dir and run the
actual work in a detached worker process. This module holds all common
mechanics: atomic reads/writes, listing, liveness (with PID-reuse guard),
cancellation (SIGTERM to the worker's process group) and cleanup.
"""

from __future__ import annotations

import json
import os
import re
import signal
import shutil
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import fcntl


_JOB_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_jobs_dir(jobs_dir: Path) -> Path:
    root = jobs_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_path(jobs_dir: Path, job_id: str) -> Path:
    job_id = str(job_id)
    if _JOB_ID_PATTERN.fullmatch(job_id) is None:
        raise ValueError("非法 job id")
    return jobs_dir / f"{job_id}.json"


@contextmanager
def _jobs_lock(jobs_dir: Path, *, exclusive: bool):
    """Serialize job-file access across worker processes and request threads."""
    root = ensure_jobs_dir(jobs_dir)
    descriptor = os.open(root / ".jobs.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield root
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _read_job_unlocked(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    path = job_path(jobs_dir, job_id)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_job_unlocked(jobs_dir: Path, job: dict[str, Any]) -> tuple[Path, dict[str, Any]]:
    stored = dict(job)
    stored["updatedAt"] = now_iso()
    path = job_path(jobs_dir, str(stored["jobId"]))
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.tmp-", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(stored, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return path, stored


def read_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    with _jobs_lock(jobs_dir, exclusive=False) as root:
        return _read_job_unlocked(root, job_id)


def write_job(jobs_dir: Path, job: dict[str, Any]) -> Path:
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        path, _stored = _write_job_unlocked(root, job)
        return path


def update_job(jobs_dir: Path, job_id: str, **fields: Any) -> dict[str, Any]:
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        job = _read_job_unlocked(root, job_id)
        if job is None:
            raise FileNotFoundError(f"job not found: {job_id}")
        job.update(fields)
        _path, stored = _write_job_unlocked(root, job)
        return stored


def update_job_if_status(
    jobs_dir: Path,
    job_id: str,
    expected_statuses: set[str] | frozenset[str],
    **fields: Any,
) -> dict[str, Any]:
    """Atomically apply fields only while a job remains in an expected state."""
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        job = _read_job_unlocked(root, job_id)
        if job is None:
            raise FileNotFoundError(f"job not found: {job_id}")
        if job.get("status") not in expected_statuses:
            return job
        job.update(fields)
        _path, stored = _write_job_unlocked(root, job)
        return stored


def claim_job_launch(jobs_dir: Path, job_id: str) -> tuple[bool, dict[str, Any]]:
    """Atomically reserve a queued job so concurrent API calls spawn one worker."""
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        job = _read_job_unlocked(root, job_id)
        if job is None:
            raise FileNotFoundError(f"job not found: {job_id}")
        if job.get("status") != "queued" or job.get("launching") is True:
            return False, job
        job.update(launching=True, message="正在启动后台 worker")
        _path, stored = _write_job_unlocked(root, job)
        return True, stored


def list_jobs(jobs_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with _jobs_lock(jobs_dir, exclusive=False) as root:
        for path in root.glob("*.json"):
            if path.name.startswith("."):
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(payload, dict):
                    expected = job_path(root, str(payload.get("jobId") or ""))
                    if expected == path:
                        rows.append(payload)
            except (OSError, ValueError, json.JSONDecodeError):
                continue
    rows.sort(key=lambda item: item.get("createdAt") or "", reverse=True)
    return rows[: max(1, limit)]


def _pid_matches_job(pid: int, job_id: str) -> bool | None:
    """Check /proc cmdline to guard against PID reuse.

    Returns True/False when the check is possible, None when it is not
    (non-Linux or permission denied) — callers should fall back to a plain
    liveness signal in that case.
    """
    cmdline_path = Path(f"/proc/{int(pid)}/cmdline")
    try:
        raw = cmdline_path.read_bytes()
    except OSError:
        return None
    expected = job_id.encode()
    return any(argument == expected for argument in raw.split(b"\0") if argument)


def worker_alive(job: dict[str, Any]) -> bool:
    """True when the recorded PID is alive AND still runs this job's worker."""
    pid = job.get("pid")
    if not pid:
        return False
    try:
        os.kill(int(pid), 0)
    except OSError:
        return False
    match = _pid_matches_job(int(pid), str(job.get("jobId") or ""))
    if match is None:
        return True
    return match


def refresh_job_liveness(job: dict[str, Any]) -> dict[str, Any]:
    """Mark stale queued/running jobs as failed when the worker never started or died."""
    status = job.get("status")
    if status not in {"queued", "running"}:
        return job

    pid = job.get("pid")
    if not pid:
        # A resumed/reclaimed queued job may be old while its latest launch
        # attempt is fresh; judge staleness from the most recent transition.
        created = job.get("updatedAt") or job.get("createdAt") or ""
        stale = True
        try:
            created_dt = datetime.fromisoformat(str(created).replace("Z", "+00:00"))
            age = (datetime.now(timezone.utc) - created_dt).total_seconds()
            stale = age > 20
        except Exception:  # noqa: BLE001
            stale = True
        if not stale:
            return job
        patched = dict(job)
        patched["status"] = "failed"
        patched["message"] = "后台 worker 未启动或任务已失效"
        return patched

    if worker_alive(job):
        return job
    patched = dict(job)
    patched["status"] = "failed"
    patched["message"] = f"worker 进程已退出（pid={pid}），任务未正常收尾"
    return patched


def refresh_stored_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    """Refresh liveness and persist it in one transaction without clobbering completion."""
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        job = _read_job_unlocked(root, job_id)
        if job is None:
            return None
        refreshed = refresh_job_liveness(job)
        if refreshed.get("status") == job.get("status"):
            return job
        _path, stored = _write_job_unlocked(root, refreshed)
        return stored


def cancel_job(jobs_dir: Path, job_id: str) -> dict[str, Any]:
    """Cancel a queued/running job by SIGTERM-ing the worker's process group."""
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        job = _read_job_unlocked(root, job_id)
        if job is None:
            raise FileNotFoundError(f"job not found: {job_id}")
        if job.get("status") not in {"queued", "running"}:
            return job
        pid = job.get("pid")
        if pid and worker_alive(job):
            try:
                # Workers start with start_new_session=True, so pid == pgid and
                # the whole group (worker + ffmpeg children) gets the signal.
                os.killpg(int(pid), signal.SIGTERM)
            except OSError:
                try:
                    os.kill(int(pid), signal.SIGTERM)
                except OSError:
                    pass
        job.update(status="cancelled", message="任务已取消", launching=False)
        _path, stored = _write_job_unlocked(root, job)
        return stored


def delete_job(jobs_dir: Path, job_id: str, extra_paths: list[Path] | None = None) -> bool:
    """Remove the job file, its log, and any associated artifact dirs."""
    with _jobs_lock(jobs_dir, exclusive=True) as root:
        path = job_path(root, job_id)
        log_path = root / f"{job_id}.log"
        existed = path.is_file()
        if existed:
            path.unlink()
        if log_path.is_file():
            try:
                log_path.unlink()
            except OSError:
                pass
    for artifact in extra_paths or []:
        try:
            artifact = Path(artifact)
            if artifact.is_dir():
                shutil.rmtree(artifact, ignore_errors=True)
            elif artifact.is_file():
                artifact.unlink()
        except OSError:
            pass
    return existed
