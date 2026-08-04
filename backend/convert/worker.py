#!/usr/bin/env python3
"""Detached convert worker: reads a job file, runs conversion, writes progress."""

from __future__ import annotations

import argparse
import os
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from convert.jobs import read_job, update_job  # noqa: E402
from convert.pipeline import convert_dataset  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Embodit convert worker")
    parser.add_argument("--job-id", required=True)
    parser.add_argument("--jobs-dir", required=True)
    args = parser.parse_args()

    jobs_dir = Path(args.jobs_dir).expanduser().resolve()
    job_id = args.job_id
    job = read_job(jobs_dir, job_id)
    if job is None:
        print(f"job not found: {job_id}", file=sys.stderr)
        return 2

    job_kind = str(job.get("kind") or "convert")

    update_job(
        jobs_dir,
        job_id,
        status="running",
        message="合并中…" if job_kind == "merge" else "转换中…",
        progress=0.01,
        pid=os.getpid(),
    )

    # Throttle progress writes: each update rewrites the whole job JSON, so
    # cap at one write per 0.5s (episode-boundary updates always pass).
    last_write = {"t": 0.0, "current": -1}

    def on_progress(payload: dict) -> None:
        import time

        now = time.monotonic()
        current = int(payload.get("current") or 0)
        if now - last_write["t"] < 0.5 and current == last_write["current"]:
            return
        last_write["t"] = now
        last_write["current"] = current
        finished_count = int(
            result.get("episodes") or result.get("totalEpisodes") or job.get("total") or 0
        )
        update_job(
            jobs_dir,
            job_id,
            status="running",
            message=str(payload.get("message") or "转换中…"),
            progress=float(payload.get("progress") or 0.0),
            current=current,
            total=int(payload.get("total") or 0),
            pid=os.getpid(),
        )

    try:
        if job_kind == "export":
            from datasets.export import export_dataset

            labels_path = Path(job["labelsPath"]) if job.get("labelsPath") else None
            result = export_dataset(
                Path(job["dataset"]),
                Path(job["output"]),
                job.get("episodes") or [],
                target_format=job.get("targetFormat") or None,
                media_mode=str(job.get("mediaMode") or "hardlink"),
                mapping=job.get("mapping") or {},
                labels_path=labels_path if labels_path and labels_path.is_file() else None,
                progress_callback=on_progress,
            )
        elif job_kind == "merge":
            import signal

            from merge.pipeline import merge_datasets

            previous_handler = signal.getsignal(signal.SIGTERM)

            def cancel_merge(_signum, _frame):
                raise KeyboardInterrupt("merge cancelled")

            signal.signal(signal.SIGTERM, cancel_merge)
            try:
                result = merge_datasets(
                    [Path(path) for path in (job.get("sources") or [])],
                    Path(job["output"]),
                    media_mode=str(job.get("mediaMode") or "hardlink"),
                    copy_labels=bool(job.get("copyLabels", True)),
                    progress_callback=on_progress,
                )
            finally:
                signal.signal(signal.SIGTERM, previous_handler)
        else:
            result = convert_dataset(
                Path(job["dataset"]),
                Path(job["output"]),
                target_format=str(job["targetFormat"]),
                episode_indices=job.get("episodes"),
                mapping=job.get("mapping") or {},
                progress_callback=on_progress,
            )
        update_job(
            jobs_dir,
            job_id,
            status="completed",
            message="合并导出完成" if job_kind == "merge" else "转换完成",
            progress=1.0,
            current=finished_count,
            total=finished_count,
            result=result,
            pid=os.getpid(),
        )
        print("completed", job_id)
        return 0
    except Exception as error:  # noqa: BLE001
        traceback.print_exc()
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
