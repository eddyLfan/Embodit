"""Persistent convert job store and detached worker launcher.

Storage / liveness / cancel mechanics live in ``jobs_common``; this module
only adds the convert-specific job payload and worker launch.
"""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from jobs_common import (  # noqa: F401  (re-exported for callers)
    cancel_job,
    claim_job_launch,
    ensure_jobs_dir,
    job_path,
    list_jobs,
    now_iso,
    read_job,
    refresh_job_liveness,
    refresh_stored_job,
    update_job,
    update_job_if_status,
    write_job,
)
from jobs_common import delete_job as _delete_job


def default_jobs_dir() -> Path:
    from settings import CONVERT_JOBS_DIR

    return CONVERT_JOBS_DIR


def create_job(
    *,
    dataset: Path,
    output: Path,
    target_format: str,
    mapping: dict[str, Any] | None = None,
    episodes: list[int] | None = None,
    jobs_dir: Path | None = None,
    kind: str = "convert",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    jobs_dir = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    job_id = uuid.uuid4().hex
    log_path = jobs_dir / f"{job_id}.log"
    job = {
        "jobId": job_id,
        "kind": kind,
        "dataset": str(dataset.expanduser().resolve()),
        "output": str(output.expanduser().resolve()),
        "targetFormat": target_format,
        "mapping": mapping or {},
        "episodes": episodes,
        **(extra or {}),
        "status": "queued",
        "message": "等待后台 worker 启动",
        "progress": 0.0,
        "current": 0,
        "total": 0,
        "pid": None,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "result": None,
        "logPath": str(log_path),
        "detached": True,
    }
    write_job(jobs_dir, job)
    return job


def launch_detached_worker(job_id: str, jobs_dir: Path | None = None) -> dict[str, Any]:
    """Spawn an independent convert worker that survives parent / terminal exit."""
    jobs_dir = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    claimed, job = claim_job_launch(jobs_dir, job_id)
    if not claimed:
        return job

    backend_root = Path(__file__).resolve().parents[1]  # backend/
    worker = backend_root / "convert" / "worker.py"
    log_path = Path(job.get("logPath") or (jobs_dir / f"{job_id}.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    env = os.environ.copy()
    env["PYTHONPATH"] = str(backend_root) + os.pathsep + env.get("PYTHONPATH", "")

    try:
        with log_path.open("a", encoding="utf-8") as log_handle:
            log_handle.write(f"\n==== launch {now_iso()} ====\n")
            log_handle.flush()
            process = subprocess.Popen(  # noqa: S603
                [
                    sys.executable,
                    str(worker),
                    "--job-id",
                    job_id,
                    "--jobs-dir",
                    str(jobs_dir),
                ],
                cwd=str(backend_root),
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,  # detach from web server / terminal session
                close_fds=True,
            )
    except Exception as error:
        update_job_if_status(
            jobs_dir,
            job_id,
            {"queued"},
            status="failed",
            message=f"后台 worker 启动失败：{error}",
            launching=False,
        )
        raise

    return update_job_if_status(
        jobs_dir,
        job_id,
        {"queued", "running"},
        message="后台 worker 已启动",
        pid=process.pid,
        launching=False,
    )


def delete_job(jobs_dir: Path, job_id: str) -> bool:
    return _delete_job(jobs_dir, job_id)
