"""Persistent QC jobs and detached worker launcher."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

from jobs_common import (
    claim_job_launch,
    delete_job,
    ensure_jobs_dir,
    list_jobs,
    now_iso,
    read_job,
    refresh_job_liveness,
    refresh_stored_job,
    update_job_if_status,
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
    claimed, job = claim_job_launch(root, job_id)
    if not claimed:
        return job
    backend_root = Path(__file__).resolve().parents[1]
    worker = backend_root / "qc" / "worker.py"
    log_path = Path(job["logPath"])
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join([str(backend_root), env.get("PYTHONPATH", "")])
    try:
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
    except Exception as error:
        update_job_if_status(
            root,
            job_id,
            {"queued"},
            status="failed",
            message=f"QC worker 启动失败：{error}",
            launching=False,
        )
        raise
    return update_job_if_status(
        root,
        job_id,
        {"queued", "running", "paused"},
        message="QC worker 已启动",
        pid=process.pid,
        launching=False,
    )


def pause_job(root: Path, job_id: str) -> dict[str, Any]:
    return update_job_if_status(
        root,
        job_id,
        {"queued", "running"},
        status="paused",
        message="扫描已暂停",
    )


def resume_job(root: Path, job_id: str) -> dict[str, Any]:
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    if job.get("status") != "paused":
        return job
    if worker_alive(job):
        return update_job_if_status(
            root,
            job_id,
            {"paused"},
            status="running",
            message="扫描继续",
        )
    queued = update_job_if_status(
        root,
        job_id,
        {"paused"},
        status="queued",
        message="正在重启 QC worker",
        pid=None,
        launching=False,
    )
    if queued.get("status") != "queued":
        return queued
    return launch_detached_worker(job_id, root)


def cancel_job(root: Path, job_id: str) -> dict[str, Any]:
    job = read_job(root, job_id)
    if job is None:
        raise FileNotFoundError(job_id)
    if job.get("status") not in {"queued", "running", "paused"}:
        return job
    cancelled = update_job_if_status(
        root,
        job_id,
        {"queued", "running", "paused"},
        status="cancelled",
        message="扫描已取消",
        launching=False,
    )
    if cancelled.get("status") != "cancelled":
        return cancelled
    pid = cancelled.get("pid")
    live_job = {**cancelled, "pid": pid}
    if pid and worker_alive(live_job):
        try:
            os.killpg(int(pid), signal.SIGTERM)
        except OSError:
            try:
                os.kill(int(pid), signal.SIGTERM)
            except OSError:
                pass
    return update_job_if_status(root, job_id, {"cancelled"}, pid=None, launching=False)
