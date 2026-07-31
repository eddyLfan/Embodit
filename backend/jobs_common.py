"""Shared persistent job store for detached workers (convert / augment).

Both job families keep one JSON file per job under a jobs dir and run the
actual work in a detached worker process. This module holds all common
mechanics: atomic reads/writes, listing, liveness (with PID-reuse guard),
cancellation (SIGTERM to the worker's process group) and cleanup.
"""

from __future__ import annotations

import json
import os
import signal
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def ensure_jobs_dir(jobs_dir: Path) -> Path:
    root = jobs_dir.expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


def job_path(jobs_dir: Path, job_id: str) -> Path:
    safe = "".join(ch for ch in job_id if ch.isalnum() or ch in "-_")
    if not safe:
        raise ValueError("非法 job id")
    return jobs_dir / f"{safe}.json"


def read_job(jobs_dir: Path, job_id: str) -> dict[str, Any] | None:
    path = job_path(jobs_dir, job_id)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def write_job(jobs_dir: Path, job: dict[str, Any]) -> Path:
    jobs_dir = ensure_jobs_dir(jobs_dir)
    job = dict(job)
    job["updatedAt"] = now_iso()
    path = job_path(jobs_dir, str(job["jobId"]))
    temporary = path.with_name(f".{path.name}.tmp-{os.getpid()}")
    temporary.write_text(json.dumps(job, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return path


def update_job(jobs_dir: Path, job_id: str, **fields: Any) -> dict[str, Any]:
    job = read_job(jobs_dir, job_id)
    if job is None:
        raise FileNotFoundError(f"job not found: {job_id}")
    job.update(fields)
    write_job(jobs_dir, job)
    return job


def list_jobs(jobs_dir: Path, limit: int = 50) -> list[dict[str, Any]]:
    jobs_dir = ensure_jobs_dir(jobs_dir)
    rows: list[dict[str, Any]] = []
    for path in jobs_dir.glob("*.json"):
        if path.name.startswith("."):
            continue
        try:
            rows.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:  # noqa: BLE001
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
    return job_id.encode() in raw


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
        created = job.get("createdAt") or job.get("updatedAt") or ""
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


def cancel_job(jobs_dir: Path, job_id: str) -> dict[str, Any]:
    """Cancel a queued/running job by SIGTERM-ing the worker's process group."""
    job = read_job(jobs_dir, job_id)
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
    return update_job(jobs_dir, job_id, status="cancelled", message="任务已取消")


def delete_job(jobs_dir: Path, job_id: str, extra_paths: list[Path] | None = None) -> bool:
    """Remove the job file, its log, and any associated artifact dirs."""
    path = job_path(jobs_dir, job_id)
    log_path = jobs_dir / f"{job_id}.log"
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
