#!/usr/bin/env python3
"""Detached augment worker."""

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

from augment.jobs import read_job, update_job_if_status  # noqa: E402
from augment.pipeline import JobCancelled, run_augment_job  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Embodit augment worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--jobs-dir", required=True)
    args = parser.parse_args()

    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    job_id = args.job_id
    job = read_job(jobs_dir, job_id)
    if job is None:
        print(f"job not found: {job_id}", file=sys.stderr)
        return 2

    # Honour cooperative cancel even mid-episode encode.
    cancel_flag = {"on": False}

    def _on_signal(_signum, _frame) -> None:
        cancel_flag["on"] = True

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)

    mode = str(job.get("mode") or "batch")
    # If already cancelled before we start, exit quietly.
    if job.get("status") == "cancelled":
        print("already cancelled", job_id)
        return 0

    claimed = update_job_if_status(
        jobs_dir,
        job_id,
        {"queued", "running"},
        status="running",
        message="测试预览生成中…" if mode == "preview" else "数据增强中…",
        progress=0.01,
        pid=os.getpid(),
        launching=False,
    )
    if claimed.get("status") != "running":
        print("cancelled before start", job_id)
        return 0

    last_write = {"t": 0.0, "current": -1}

    def on_progress(payload: dict) -> None:
        import time

        live = read_job(jobs_dir, job_id) or {}
        if live.get("status") == "cancelled" or cancel_flag["on"]:
            cancel_flag["on"] = True
            raise JobCancelled("任务已取消")

        now = time.monotonic()
        current = int(payload.get("current") or 0)
        if now - last_write["t"] < 0.5 and current == last_write["current"]:
            return
        last_write["t"] = now
        last_write["current"] = current
        updated = update_job_if_status(
            jobs_dir,
            job_id,
            {"running"},
            status="running",
            message=str(payload.get("message") or "处理中…"),
            progress=float(payload.get("progress") or 0.0),
            current=current,
            total=int(payload.get("total") or 0),
            pid=os.getpid(),
        )
        if updated.get("status") == "cancelled":
            raise JobCancelled("任务已取消")

    try:
        result = run_augment_job(
            job,
            progress_callback=on_progress,
            jobs_dir=jobs_dir,
            cancel_flag=cancel_flag,
        )
        # Race: cancel may have landed while finalize ran.
        live = read_job(jobs_dir, job_id) or {}
        if live.get("status") == "cancelled":
            print("cancelled", job_id)
            return 0
        updated = update_job_if_status(
            jobs_dir,
            job_id,
            {"running"},
            status="completed",
            message="预览完成" if mode == "preview" else "增强完成",
            progress=1.0,
            current=int(result.get("okEpisodes") or result.get("totalEpisodes") or 1),
            total=int(result.get("requestedEpisodes") or result.get("totalEpisodes") or 1),
            result=result,
            pid=os.getpid(),
        )
        if updated.get("status") == "cancelled":
            print("cancelled", job_id)
            return 0
        print("completed", job_id)
        return 0
    except JobCancelled as error:
        update_job_if_status(
            jobs_dir,
            job_id,
            {"queued", "running"},
            status="cancelled",
            message=str(error) or "任务已取消",
            pid=None,
        )
        print("cancelled", job_id)
        return 0
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
        update_job_if_status(
            jobs_dir,
            job_id,
            {"queued", "running"},
            status="failed",
            message=f"{type(error).__name__}: {error}",
            pid=os.getpid(),
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
