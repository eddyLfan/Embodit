"""Persistent QC jobs and detached worker launcher."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from jobs_common import (
    delete_job,
    ensure_jobs_dir,
    list_jobs,
    now_iso,
    read_job,
    refresh_job_liveness,
    update_job,
    worker_alive,
    write_job,
)
from qc.config import config_hash, merge_config
from qc.paths import jobs_dir as default_jobs_dir
from qc.paths import report_path


def create_job(
    *,
    dataset: Path,
    config: dict[str, Any] | None = None,
    use_cache: bool = True,
    jobs_dir: Path | None = None,
) -> dict[str, Any]:
    root = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    job_id = uuid.uuid4().hex
    merged = merge_config(config)
    report = report_path(dataset, job_id, config_hash(merged))
    job = {
        "jobId": job_id,
        "kind": "qc",
        "dataset": str(dataset.expanduser().resolve()),
        "config": merged,
        "configHash": config_hash(merged),
        "useCache": bool(use_cache),
        "reportPath": str(report),
        "status": "queued",
        "message": "等待 QC worker 启动",
        "progress": 0.0,
        "current": 0,
        "total": 0,
        "pid": None,
        "createdAt": now_iso(),
        "updatedAt": now_iso(),
        "result": None,
        "logPath": str(root / f"{job_id}.log"),
        "detached": True,
    }
    write_job(root, job)
    return job


def launch_detached_worker(job_id: str, jobs_dir: Path | None = None) -> dict[str, Any]:
    root = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    backend_root = Path(__file__).resolve().parents[1]
    worker = backend_root / "qc" / "worker.py"
    log_path = Path(job["logPath"])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(backend_root), env.get("PYTHONPATH", "")])
    with log_path.open("a", encoding="utf-8") as log_handle:
        process = subprocess.Popen(
            [sys.executable, str(worker), "--job-id", job_id, "--jobs-dir", str(root)],
            cwd="/tmp",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )
    return update_job(root, job_id, status="queued", message="QC worker 已启动", pid=process.pid)


def pause_job(root: Path, job_id: str) -> dict[str, Any]:
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    if job.get("status") in {"queued", "running"}:
        return update_job(root, job_id, status="paused", message="扫描已暂停")
    return job


def resume_job(root: Path, job_id: str) -> dict[str, Any]:
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    if job.get("status") != "paused":
        return job
    if worker_alive(job):
        return update_job(root, job_id, status="running", message="扫描继续")
    return launch_detached_worker(job_id, root)


def cancel_job(root: Path, job_id: str) -> dict[str, Any]:
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    if job.get("status") not in {"queued", "running", "paused"}:
        return job
    pid = job.get("pid")
    if pid and worker_alive(job):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    return update_job(root, job_id, status="cancelled", message="扫描已取消", pid=None)
