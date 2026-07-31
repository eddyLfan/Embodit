"""Persistent augment job store and detached worker launcher.

Storage / liveness / cancel mechanics live in ``jobs_common``; this module
only adds the augment-specific job payload and worker launch (GPU selection
and an optional SAM3 interpreter).
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

from augment.capabilities import config_fingerprint
from augment.paths import DEFAULT_JOBS_DIR, DEFAULT_PREVIEW_DIR, SAM3_CHECKPOINT
from jobs_common import (  # noqa: F401  (re-exported for callers)
    ensure_jobs_dir,
    job_path,
    list_jobs,
    now_iso,
    read_job,
    refresh_job_liveness,
    update_job,
    worker_alive,
    write_job,
)
from jobs_common import delete_job as _delete_job


def _cleanup_augment_artifacts(job: dict[str, Any]) -> list[str]:
    """Remove incomplete output / staging dirs for a cancelled augment job."""
    cleaned: list[str] = []
    output = job.get("output")
    if not output:
        return cleaned
    out = Path(output).expanduser().resolve()
    job_id = str(job.get("jobId") or "")
    candidates = [
        out.parent / f".{out.name}.augment-building",
    ]
    if job_id:
        candidates.append(out.parent / f".{out.name}.augment-building-{job_id}")
    for path in candidates:
        try:
            if path.is_dir():
                shutil.rmtree(path, ignore_errors=True)
                cleaned.append(str(path))
        except OSError:
            continue
    # Incomplete final output (no info.json) — safe to remove.
    try:
        if out.exists() and out.is_dir() and not (out / "meta" / "info.json").is_file():
            shutil.rmtree(out, ignore_errors=True)
            cleaned.append(str(out))
    except OSError:
        pass
    return cleaned


def cancel_job(jobs_dir: Path, job_id: str) -> dict[str, Any]:
    """Cancel augment job, SIGTERM(+KILL) the worker, and clean staging products."""
    job = read_job(jobs_dir, job_id)
    if job is None:
        raise FileNotFoundError(f"job not found: {job_id}")
    if job.get("status") not in {"queued", "running"}:
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
        time.sleep(0.8)
        live = dict(job)
        live["pid"] = pid
        if worker_alive(live):
            try:
                os.killpg(int(pid), signal.SIGKILL)
            except OSError:
                try:
                    os.kill(int(pid), signal.SIGKILL)
                except OSError:
                    pass

    cleaned = _cleanup_augment_artifacts(job)
    message = "任务已取消"
    if cleaned:
        message = f"任务已取消，已清理未完成产物（{len(cleaned)}）"
    return update_job(
        jobs_dir,
        job_id,
        status="cancelled",
        message=message,
        pid=None,
        result={"cleaned": cleaned, "cancelled": True},
    )


def default_jobs_dir() -> Path:
    return DEFAULT_JOBS_DIR


def create_job(*, config: dict[str, Any], jobs_dir: Path | None = None) -> dict[str, Any]:
    jobs_dir = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    job_id = uuid.uuid4().hex
    log_path = jobs_dir / f"{job_id}.log"
    mode = str(config.get("mode") or "batch")
    job = {
        "jobId": job_id,
        "kind": "augment",
        "mode": mode,
        "dataset": str(Path(config["dataset"]).expanduser().resolve()),
        "output": str(Path(config["output"]).expanduser().resolve()) if config.get("output") else None,
        "augType": config.get("augType") or "brightness",
        "applyMode": config.get("applyMode") or "object_recolor",
        "samPrompts": list(config.get("samPrompts") or []),
        "colorMode": config.get("colorMode") or "random",
        "colorName": config.get("colorName"),
        "colorRgb": config.get("colorRgb"),
        "brightnessMode": config.get("brightnessMode") or "auto",
        "brightnessGain": config.get("brightnessGain"),
        "brightnessGamma": config.get("brightnessGamma"),
        "gpuId": int(config.get("gpuId") or 0),
        "episodes": config.get("episodes"),
        "sampleCount": config.get("sampleCount"),
        "previewEpisode": config.get("previewEpisode"),
        "previewDir": config.get("previewDir"),
        "targetFormat": config.get("targetFormat") or "lerobot_v3",
        "cameraPolicy": config.get("cameraPolicy") or "strict",
        "configFingerprint": config_fingerprint(config),
        "previewJobId": config.get("previewJobId"),
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


def resolve_worker_python(aug_type: str) -> str:
    """Use an optional SAM3 Python for color; otherwise the core interpreter."""
    override = os.environ.get("AUGMENT_PYTHON", "").strip()
    if aug_type == "color" and override and Path(override).is_file():
        return str(Path(override).expanduser().absolute())
    return sys.executable


def launch_detached_worker(job_id: str, jobs_dir: Path | None = None) -> dict[str, Any]:
    jobs_dir = ensure_jobs_dir(jobs_dir or default_jobs_dir())
    job = read_job(jobs_dir, job_id)
    if job is None:
        raise FileNotFoundError(job_id)

    backend_root = Path(__file__).resolve().parents[1]
    worker = backend_root / "augment" / "worker.py"
    log_path = Path(job.get("logPath") or (jobs_dir / f"{job_id}.log"))
    log_path.parent.mkdir(parents=True, exist_ok=True)

    python_bin = resolve_worker_python(str(job.get("augType") or "brightness"))
    env = os.environ.copy()
    path_parts = [str(backend_root)]
    env["PYTHONPATH"] = os.pathsep.join(path_parts + [env.get("PYTHONPATH", "")])
    env["AUGMENT_SAM3_CHECKPOINT"] = os.environ.get("AUGMENT_SAM3_CHECKPOINT", str(SAM3_CHECKPOINT))
    env["CUDA_VISIBLE_DEVICES"] = str(int(job.get("gpuId") or 0))

    with log_path.open("a", encoding="utf-8") as log_handle:
        log_handle.write(f"\n==== launch {now_iso()} python={python_bin} ====\n")
        log_handle.flush()
        process = subprocess.Popen(  # noqa: S603
            [
                python_bin,
                str(worker),
                "--job-id",
                job_id,
                "--jobs-dir",
                str(jobs_dir),
            ],
            cwd="/tmp",
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
            close_fds=True,
        )

    return update_job(
        jobs_dir,
        job_id,
        status="queued",
        message="后台 worker 已启动",
        pid=process.pid,
    )


def delete_job(jobs_dir: Path, job_id: str) -> bool:
    """Remove the job plus its preview directory (if any)."""
    job = read_job(jobs_dir, job_id)
    extra: list[Path] = []
    if job:
        preview_dir = job.get("previewDir")
        if preview_dir:
            extra.append(Path(preview_dir))
        else:
            extra.append(DEFAULT_PREVIEW_DIR / job_id)
    return _delete_job(jobs_dir, job_id, extra_paths=extra)
