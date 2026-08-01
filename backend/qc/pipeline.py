"""Streaming automatic QC scan pipeline."""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from threading import local
from typing import Any, Callable

from datasets.registry import open_dataset
from qc.config import config_hash, merge_config
from qc.detectors import BUILTIN_DETECTORS
from qc.detectors.base import EpisodeContext, ScanCancelled
from qc.fingerprint import dataset_fingerprint, dataset_id
from qc.schema import DetectorOutcome, Finding
from qc.scoring import score_episode
from qc.store import (
    initialize_report,
    now_iso,
    scan_info,
    update_scan,
    upsert_detector_run,
    write_episode,
)

ProgressCallback = Callable[[dict[str, Any]], None]
ControlCallback = Callable[[], bool]


def _cached_report(
    report_dir: Path,
    fingerprint: str,
    configuration_hash: str,
) -> Path | None:
    if not report_dir.is_dir():
        return None
    candidates = sorted(report_dir.glob("*.qc.sqlite3"), key=lambda path: path.stat().st_mtime, reverse=True)
    for candidate in candidates:
        try:
            info = scan_info(candidate)
        except Exception:  # noqa: BLE001
            continue
        if (
            info.get("status") == "completed"
            and info.get("dataset_fingerprint") == fingerprint
            and info.get("config_hash") == configuration_hash
        ):
            return candidate
    return None


def _analyze_episode(
    adapter: Any,
    view: Any,
    episode: Any,
    merged: dict[str, Any],
    enabled_detectors: list[Any],
    cancelled: ControlCallback | None,
    paused: ControlCallback | None,
) -> dict[str, Any]:
    """Analyze one episode without touching SQLite (safe for worker threads)."""
    _cooperative_wait(cancelled, paused)
    signals: dict[str, Any] = {}
    initial_findings: list[Finding] = []
    try:
        signals = adapter.get_timeseries(episode.episode_index)
    except Exception as error:  # noqa: BLE001
        signal_read_error = f"{type(error).__name__}: {error}"
        initial_findings.append(
            Finding(
                episode_index=episode.episode_index,
                issue_code="integrity/signal_read_error",
                category="integrity",
                severity="fatal",
                detector_id="signal_integrity",
                explanation=f"无法读取 action/state：{signal_read_error}",
                metrics={"error": signal_read_error},
                suggested_decision="quarantine",
                hard_invalid=True,
            )
        )
    context = EpisodeContext(
        adapter=adapter,
        view=view,
        episode=episode,
        signals=signals,
        config=merged,
        cancelled=cancelled,
    )
    outcomes: list[DetectorOutcome] = []
    for detector in enabled_detectors:
        _cooperative_wait(cancelled, paused)
        try:
            outcome = detector.run(context)
        except ScanCancelled:
            raise
        except Exception as error:  # noqa: BLE001
            outcome = DetectorOutcome(
                detector_id=detector.detector_id,
                version=detector.version,
                status="failed",
                skip_reason=f"{type(error).__name__}: {error}",
                coverage_weight=detector.coverage_weight,
            )
        outcomes.append(outcome)

    finding_rows = [item.to_dict() for item in initial_findings]
    detector_rows = []
    for outcome in outcomes:
        finding_rows.extend(item.to_dict() for item in outcome.findings)
        detector_rows.append(
            {
                "detectorId": outcome.detector_id,
                "version": outcome.version,
                "status": outcome.status,
                "skipReason": outcome.skip_reason,
                "coverageWeight": outcome.coverage_weight,
                "applicable": True,
            }
        )
    return {
        "episode": episode,
        "findings": finding_rows,
        "detectors": detector_rows,
        "score": score_episode(episode.duration, finding_rows, detector_rows, merged),
        "taskText": " · ".join(str(item) for item in episode.tasks if item),
    }


def run_scan(
    dataset: Path,
    report_path: Path,
    *,
    scan_id: str,
    config: dict[str, Any] | None = None,
    use_cache: bool = True,
    progress: ProgressCallback | None = None,
    cancelled: ControlCallback | None = None,
    paused: ControlCallback | None = None,
) -> dict[str, Any]:
    dataset = dataset.expanduser().resolve()
    merged = merge_config(config)
    configuration_hash = config_hash(merged)
    adapter = open_dataset(dataset)
    view = adapter.inspect()
    fingerprint = dataset_fingerprint(dataset, view)
    if use_cache:
        cached = _cached_report(report_path.parent, fingerprint, configuration_hash)
        if cached is not None and cached.resolve() != report_path.resolve():
            info = scan_info(cached)
            return {
                "scanId": info.get("scan_id") or info.get("scanId"),
                "reportPath": str(cached),
                "cached": True,
                "totalEpisodes": int(info.get("total_episodes") or info.get("totalEpisodes") or 0),
            }

    initialize_report(
        report_path,
        {
            "scanId": scan_id,
            "datasetPath": str(dataset),
            "datasetFormat": view.format_id,
            "datasetId": dataset_id(dataset),
            "datasetFingerprint": fingerprint,
            "config": merged,
            "configHash": configuration_hash,
            "status": "running",
            "phase": "episodes",
            "startedAt": now_iso(),
            "totalEpisodes": len(view.episodes),
            "message": "正在扫描 episode",
        },
    )
    enabled_detectors = []
    for detector in BUILTIN_DETECTORS:
        detector_config = merged["detectors"].get(detector.config_key, {})
        enabled = bool(detector_config.get("enabled", True))
        upsert_detector_run(
            report_path,
            {
                "detectorId": detector.detector_id,
                "version": detector.version,
                "enabled": enabled,
                "applicable": True,
                "status": "running" if enabled else "disabled",
                "coverageWeight": detector.coverage_weight,
                "config": detector_config,
            },
        )
        if enabled:
            enabled_detectors.append(detector)

    detector_counts = {
        detector.detector_id: {"processed": 0, "failed": 0, "skipped": 0}
        for detector in enabled_detectors
    }
    runtime = merged.get("runtime", {})
    requested_episode_workers = int(runtime.get("episodeWorkers", 1))
    requested_camera_workers = int(runtime.get("cameraWorkers", 1))
    cpu_budget = max(1, int(os.cpu_count() or 1))
    maximum_cameras = max((len(episode.cameras) for episode in view.episodes), default=1)
    camera_workers = min(maximum_cameras, max(1, requested_camera_workers), cpu_budget)
    episode_workers = min(
        max(1, len(view.episodes)),
        max(1, requested_episode_workers),
        max(1, cpu_budget // max(1, camera_workers)),
    )
    # Preserve the requested config/hash while ensuring nested episode × camera
    # concurrency never exceeds the detected CPU budget.
    runtime["activeCameraWorkers"] = camera_workers
    worker_state = local()

    def adapter_for_thread() -> Any:
        if episode_workers == 1:
            return adapter
        if not hasattr(worker_state, "adapter"):
            worker_state.adapter = open_dataset(dataset)
        return worker_state.adapter

    def analyze(episode: Any) -> dict[str, Any]:
        return _analyze_episode(
            adapter_for_thread(), view, episode, merged, enabled_detectors, cancelled, paused
        )

    def analyzed_results():
        if episode_workers == 1:
            for episode in view.episodes:
                yield analyze(episode)
            return
        with ThreadPoolExecutor(max_workers=episode_workers, thread_name_prefix="qc-episode") as executor:
            futures = [executor.submit(analyze, episode) for episode in view.episodes]
            for future in as_completed(futures):
                yield future.result()

    total_episodes = len(view.episodes)
    started_monotonic = time.monotonic()
    if progress:
        progress(
            {
                "current": 0,
                "total": total_episodes,
                "progress": 0.0,
                "message": f"并行扫描：{episode_workers} episode × {camera_workers} camera workers",
                "reportPath": str(report_path),
                "episodeWorkers": episode_workers,
                "cameraWorkers": camera_workers,
            }
        )
    try:
        for ordinal, result in enumerate(analyzed_results(), start=1):
            episode = result["episode"]
            finding_rows = result["findings"]
            detector_rows = result["detectors"]
            score = result["score"]
            task_text = result["taskText"]
            for detector_row in detector_rows:
                counts = detector_counts[detector_row["detectorId"]]
                counts["processed"] += 1
                if detector_row["status"] == "failed":
                    counts["failed"] += 1
                if detector_row["status"] == "skipped":
                    counts["skipped"] += 1
            write_episode(
                report_path,
                {
                    "episodeIndex": episode.episode_index,
                    "taskText": task_text,
                    "duration": episode.duration,
                    "frameCount": episode.length,
                    **score,
                },
                finding_rows,
                detector_rows,
            )
            update_scan(
                report_path,
                processedEpisodes=ordinal,
                phase="episodes",
                message=f"Episode {episode.episode_index} 扫描完成",
            )
            for detector in enabled_detectors:
                counts = detector_counts[detector.detector_id]
                status = "failed" if counts["failed"] == ordinal else "running"
                upsert_detector_run(
                    report_path,
                    {
                        "detectorId": detector.detector_id,
                        "version": detector.version,
                        "enabled": True,
                        "applicable": True,
                        "status": status,
                        "processedCount": counts["processed"],
                        "failedCount": counts["failed"],
                        "coverageWeight": detector.coverage_weight,
                        "config": merged["detectors"].get(detector.config_key, {}),
                    },
                )
            if progress:
                elapsed = time.monotonic() - started_monotonic
                eta = elapsed / max(ordinal, 1) * max(0, total_episodes - ordinal)
                progress(
                    {
                        "current": ordinal,
                        "total": total_episodes,
                        "progress": ordinal / max(total_episodes, 1),
                        "message": f"Episode {episode.episode_index} 扫描完成 · ETA {int(eta)}s",
                        "reportPath": str(report_path),
                        "elapsedSeconds": round(elapsed, 3),
                        "etaSeconds": round(eta, 3),
                        "episodeWorkers": episode_workers,
                        "cameraWorkers": camera_workers,
                    }
                )
            delay = max(0.0, float(merged.get("runtime", {}).get("sleepBetweenEpisodes", 0.0)))
            if delay:
                time.sleep(delay)
    except ScanCancelled:
        update_scan(
            report_path,
            status="cancelled",
            completedAt=now_iso(),
            message="扫描已取消，已保留部分报告",
        )
        raise
    except Exception as error:
        update_scan(
            report_path,
            status="failed",
            completedAt=now_iso(),
            message=f"{type(error).__name__}: {error}",
            error=f"{type(error).__name__}: {error}",
        )
        raise

    for detector in enabled_detectors:
        counts = detector_counts[detector.detector_id]
        upsert_detector_run(
            report_path,
            {
                "detectorId": detector.detector_id,
                "version": detector.version,
                "enabled": True,
                "applicable": True,
                "status": "completed" if counts["failed"] < len(view.episodes) else "failed",
                "processedCount": counts["processed"],
                "failedCount": counts["failed"],
                "coverageWeight": detector.coverage_weight,
                "config": merged["detectors"].get(detector.config_key, {}),
            },
        )
    update_scan(
        report_path,
        status="completed",
        phase="completed",
        completedAt=now_iso(),
        processedEpisodes=len(view.episodes),
        message="自动质检完成",
    )
    return {
        "scanId": scan_id,
        "reportPath": str(report_path),
        "cached": False,
        "totalEpisodes": len(view.episodes),
        "datasetFingerprint": fingerprint,
        "configHash": configuration_hash,
        "episodeWorkers": episode_workers,
        "cameraWorkers": camera_workers,
    }


def _cooperative_wait(cancelled: ControlCallback | None, paused: ControlCallback | None) -> None:
    while paused and paused():
        if cancelled and cancelled():
            raise ScanCancelled("QC 扫描已取消")
        time.sleep(0.25)
    if cancelled and cancelled():
        raise ScanCancelled("QC 扫描已取消")
