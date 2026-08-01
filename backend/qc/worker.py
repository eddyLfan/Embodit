#!/usr/bin/env python3
"""Detached automatic QC worker."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from qc.detectors.base import ScanCancelled  # noqa: E402
from qc.jobs import read_job, update_job  # noqa: E402
from qc.pipeline import run_scan  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Embodit QC worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--jobs-dir", required=True)
    args = parser.parse_args()
    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    job_id = args.job_id
    job = read_job(jobs_dir, job_id)
    if job is None:
        return 2
    cancel_flag = {"on": False}

    def on_signal(_signum, _frame) -> None:
        cancel_flag["on"] = True

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    if job.get("status") == "cancelled":
        return 0
    update_job(
        jobs_dir,
        job_id,
        status="running",
        message="自动质检中…",
        progress=0.01,
        pid=os.getpid(),
    )

    def cancelled() -> bool:
        live = read_job(jobs_dir, job_id) or {}
        return cancel_flag["on"] or live.get("status") == "cancelled"

    def paused() -> bool:
        live = read_job(jobs_dir, job_id) or {}
        return live.get("status") == "paused"

    def progress(payload: dict) -> None:
        if cancelled():
            raise ScanCancelled("扫描已取消")
        update_job(
            jobs_dir,
            job_id,
            status="running",
            message=str(payload.get("message") or "自动质检中…"),
            progress=float(payload.get("progress") or 0.0),
            current=int(payload.get("current") or 0),
            total=int(payload.get("total") or 0),
            elapsedSeconds=payload.get("elapsedSeconds"),
            etaSeconds=payload.get("etaSeconds"),
            episodeWorkers=payload.get("episodeWorkers"),
            cameraWorkers=payload.get("cameraWorkers"),
            reportPath=payload.get("reportPath") or job.get("reportPath"),
            pid=os.getpid(),
        )

    try:
        result = run_scan(
            Path(job["dataset"]),
            Path(job["reportPath"]),
            scan_id=job_id,
            config=job.get("config") or {},
            use_cache=bool(job.get("useCache", True)),
            progress=progress,
            cancelled=cancelled,
            paused=paused,
        )
        if cancelled():
            return 0
        update_job(
            jobs_dir,
            job_id,
            status="completed",
            message="自动质检完成" + ("（复用缓存）" if result.get("cached") else ""),
            progress=1.0,
            current=int(result.get("totalEpisodes") or 0),
            total=int(result.get("totalEpisodes") or 0),
            reportPath=result["reportPath"],
            result=result,
            pid=os.getpid(),
        )
        return 0
    except ScanCancelled:
        if (read_job(jobs_dir, job_id) or {}).get("status") != "cancelled":
            update_job(jobs_dir, job_id, status="cancelled", message="扫描已取消", pid=None)
        return 0
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        if (read_job(jobs_dir, job_id) or {}).get("status") != "cancelled":
            update_job(
                jobs_dir,
                job_id,
                status="failed",
                message=f"{type(error).__name__}: {error}",
                pid=os.getpid(),
            )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
