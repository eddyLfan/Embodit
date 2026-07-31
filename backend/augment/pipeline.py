"""Augment pipeline: load videos → transform → preview or batch write."""

from __future__ import annotations

import random
from pathlib import Path
from typing import Any, Callable

import numpy as np

from augment.algorithms import (
    apply_brightness_videos,
    apply_color_video,
    apply_color_videos,
    get_segmenter,
    parse_prompts,
)
from augment.colors import resolve_color
from augment.output_writer import AugmentDatasetWriter
from augment.paths import DEFAULT_PREVIEW_DIR, PREVIEW_FRAME_LIMIT
from augment.preview import write_preview
from augment.video_io import decode_video_rgb
from datasets.registry import open_dataset
from datasets.view import DatasetView

ProgressCallback = Callable[[dict[str, Any]], None]


class JobCancelled(Exception):
    """Raised when the user cancels an augment job."""


def _emit(progress_callback: ProgressCallback | None, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(payload)
    except JobCancelled:
        raise
    except Exception:  # noqa: BLE001
        pass


def _check_cancelled(
    *,
    jobs_dir: Path | None,
    job_id: str | None,
    cancel_flag: dict | None,
) -> None:
    if cancel_flag and cancel_flag.get("on"):
        raise JobCancelled("任务已取消")
    if jobs_dir is None or not job_id:
        return
    from augment.jobs import read_job

    live = read_job(jobs_dir, job_id)
    if live and live.get("status") == "cancelled":
        if cancel_flag is not None:
            cancel_flag["on"] = True
        raise JobCancelled("任务已取消")


def _load_episode_videos(
    adapter,
    view: DatasetView,
    episode_index: int,
    max_frames: int | None = None,
) -> tuple[dict[str, np.ndarray], dict[str, Any]]:
    ep = next((item for item in view.episodes if item.episode_index == episode_index), None)
    if ep is None:
        raise ValueError(f"episode {episode_index} 不存在")

    from datasets.frames import episode_frame_source

    videos: dict[str, np.ndarray] = {}
    for cam_key in ep.cameras:
        source = episode_frame_source(adapter, view, ep, cam_key)
        if source is None:
            continue
        if source.video_path is not None:
            videos[cam_key] = decode_video_rgb(source.video_path, max_frames=max_frames)
        else:
            frames = []
            for idx, frame in enumerate(source.iter_rgb()):
                if max_frames is not None and idx >= max_frames:
                    break
                frames.append(frame)
            if frames:
                videos[cam_key] = np.stack(frames, axis=0)
    if not videos:
        raise RuntimeError(f"episode {episode_index} 没有可解码的相机帧源")

    state, action, ts_error = _load_timeseries(adapter, episode_index)
    if max_frames is not None:
        n = min(len(next(iter(videos.values()))), max_frames)
        if state is not None:
            state = state[:n]
        if action is not None:
            action = action[:n]

    length = int(ep.length or 0)
    if videos:
        length = len(next(iter(videos.values())))
    meta = {
        "episode_index": episode_index,
        "length": length,
        "tasks": list(ep.tasks or []),
        "state": state,
        "action": action,
        "timeseriesError": ts_error,
    }
    return videos, meta


def _load_timeseries(adapter, episode_index: int) -> tuple[np.ndarray | None, np.ndarray | None, str | None]:
    """Load state/action; failure is reported instead of silently swallowed."""
    state = None
    action = None
    error: str | None = None
    try:
        series = adapter.get_timeseries(episode_index)
    except Exception as exc:  # noqa: BLE001
        series = {}
        error = f"{type(exc).__name__}: {exc}"
    if "action" in series:
        action = np.asarray(series["action"])
    if "observation.state" in series:
        state = np.asarray(series["observation.state"])
    return state, action, error


def _run_augment_on_videos(
    videos: dict[str, np.ndarray],
    job: dict[str, Any],
    *,
    seed_key: str,
) -> tuple[dict[str, np.ndarray], dict[str, np.ndarray] | None, dict[str, Any]]:
    aug_type = str(job.get("augType") or "brightness")
    if aug_type == "brightness":
        out, meta = apply_brightness_videos(
            videos,
            mode=str(job.get("brightnessMode") or "auto"),
            gain=job.get("brightnessGain"),
            gamma=job.get("brightnessGamma"),
        )
        return out, None, {"augType": "brightness", "brightness": meta, **{k: meta.get(k) for k in ("mode", "gain", "gamma")}}

    prompts = parse_prompts(job.get("samPrompts"))
    if not prompts:
        raise ValueError("颜色增强需要 SAM3 查询词")
    apply_mode = str(job.get("applyMode") or "object_recolor")
    color = resolve_color(
        apply_mode=apply_mode,
        color_mode=str(job.get("colorMode") or "random"),
        color_name=job.get("colorName"),
        color_rgb=job.get("colorRgb"),
        seed_key=seed_key,
    )
    out, masks, meta = apply_color_videos(
        videos,
        prompts=prompts,
        apply_mode=apply_mode,
        color_rgb=color["colorRgb"],
        gpu_id=int(job.get("gpuId") or 0),
    )
    meta = {**meta, "augType": "color", **color}
    return out, masks, meta


def run_augment_job(
    job: dict[str, Any],
    progress_callback: ProgressCallback | None = None,
    jobs_dir: Path | None = None,
    cancel_flag: dict | None = None,
) -> dict[str, Any]:
    mode = str(job.get("mode") or "batch")
    job_id = str(job.get("jobId") or "") or None
    dataset = Path(job["dataset"]).expanduser().resolve()
    _check_cancelled(jobs_dir=jobs_dir, job_id=job_id, cancel_flag=cancel_flag)
    _emit(progress_callback, progress=0.02, current=0, total=0, message="打开源数据集…")
    adapter = open_dataset(dataset)
    view: DatasetView = adapter.inspect()
    fps = float(view.fps or 30.0)
    target_format = str(job.get("targetFormat") or view.format_id or "lerobot_v3")
    if target_format not in {"lerobot_v3", "lerobot_v21"}:
        target_format = "lerobot_v3"

    if mode == "preview":
        return _run_preview(job, adapter, view, fps, progress_callback)

    episodes = job.get("episodes")
    if episodes is None:
        indices = [ep.episode_index for ep in view.episodes]
        sample_count = job.get("sampleCount")
        if sample_count is not None:
            try:
                n = max(1, int(sample_count))
            except (TypeError, ValueError) as error:
                raise ValueError("sampleCount 必须是正整数") from error
            if n < len(indices):
                seed = str(job.get("jobId") or "augment")
                rng = random.Random(seed)
                indices = rng.sample(indices, n)
            # else keep all when requested count >= total
    else:
        indices = [int(x) for x in episodes]
    if not indices:
        raise ValueError("没有可增强的 episode")

    output = Path(job["output"]).expanduser().resolve()
    writer = AugmentDatasetWriter(
        output,
        target_format=target_format,
        fps=fps,
        robot_type=view.robot_type,
        job_id=str(job.get("jobId") or "") or None,
    )
    aug_type = str(job.get("augType") or "brightness")
    camera_policy = str(job.get("cameraPolicy") or "strict")
    if camera_policy not in {"strict", "partial"}:
        camera_policy = "strict"
    # Load SAM3 weights once per job instead of once per episode.
    segmenter = get_segmenter() if aug_type == "color" else None

    ok = 0
    failed: list[dict[str, Any]] = []
    total = len(indices)
    try:
        for offset, ep_idx in enumerate(indices, start=1):
            _check_cancelled(jobs_dir=jobs_dir, job_id=job_id, cancel_flag=cancel_flag)
            _emit(
                progress_callback,
                progress=0.05 + 0.9 * ((offset - 1) / max(1, total)),
                current=offset - 1,
                total=total,
                message=f"增强 episode {ep_idx}（{offset}/{total}）",
            )
            try:
                _augment_episode_streaming(
                    adapter,
                    view,
                    ep_idx,
                    job,
                    writer,
                    segmenter=segmenter,
                    camera_policy=camera_policy,
                )
                ok += 1
            except JobCancelled:
                raise
            except Exception as error:  # noqa: BLE001
                failed.append({"episode": ep_idx, "error": f"{type(error).__name__}: {error}"})

        if ok == 0:
            writer.cleanup()
            raise RuntimeError("全部 episode 增强失败：" + "; ".join(item["error"] for item in failed[:5]))

        _check_cancelled(jobs_dir=jobs_dir, job_id=job_id, cancel_flag=cancel_flag)
        _emit(progress_callback, progress=0.96, current=ok, total=total, message="写入数据集元数据…")
        result = writer.finalize()
    except JobCancelled:
        writer.cleanup()
        raise
    except Exception:
        writer.cleanup()
        raise
    result["failed"] = failed
    result["okEpisodes"] = ok
    result["requestedEpisodes"] = total
    _emit(progress_callback, progress=1.0, current=ok, total=total, message="增强完成")
    return result


def _augment_episode_streaming(
    adapter,
    view: DatasetView,
    ep_idx: int,
    job: dict[str, Any],
    writer: AugmentDatasetWriter,
    *,
    segmenter: Any | None,
    camera_policy: str,
) -> dict[str, Any]:
    """Augment one episode camera-by-camera so at most one camera is in memory."""
    ep = next((item for item in view.episodes if item.episode_index == ep_idx), None)
    if ep is None:
        raise ValueError(f"episode {ep_idx} 不存在")
    # FrameSource abstraction: mp4, HDF5-embedded frames, and MCAP topics all
    # become uniform decoded-RGB sources.
    from datasets.frames import episode_frame_source

    cam_sources: dict[str, Any] = {}
    for cam_key in ep.cameras:
        source = episode_frame_source(adapter, view, ep, cam_key)
        if source is not None:
            cam_sources[cam_key] = source
    if not cam_sources:
        raise RuntimeError(f"episode {ep_idx} 没有可解码的相机帧源")

    aug_type = str(job.get("augType") or "brightness")
    seed_key = f"{job.get('jobId')}:{ep_idx}"
    episode_meta: dict[str, Any] = {"augType": aug_type, "cameras": {}, "cameraFailures": []}
    color: dict[str, Any] | None = None
    prompts: list[str] = []
    apply_mode = str(job.get("applyMode") or "object_recolor")
    if aug_type == "color":
        prompts = parse_prompts(job.get("samPrompts"))
        if not prompts:
            raise ValueError("颜色增强需要 SAM3 查询词")
        # One color decision per episode, shared by all cameras.
        color = resolve_color(
            apply_mode=apply_mode,
            color_mode=str(job.get("colorMode") or "random"),
            color_name=job.get("colorName"),
            color_rgb=job.get("colorRgb"),
            seed_key=seed_key,
        )
        episode_meta.update({"applyMode": apply_mode, "prompts": prompts, **color})

    state, action, ts_error = _load_timeseries(adapter, ep_idx)
    if ts_error:
        episode_meta["timeseriesError"] = ts_error

    ctx = writer.begin_episode(
        source_episode_index=ep_idx,
        length=int(ep.length or 0),
        tasks=list(ep.tasks or []),
    )
    try:
        for cam_key, source in cam_sources.items():
            # Prefer the threaded PyAV decoder for mp4-backed sources.
            if source.video_path is not None:
                frames = decode_video_rgb(source.video_path)
            else:
                frames = source.load_rgb()
            try:
                if aug_type == "brightness":
                    out, meta = apply_brightness_videos(
                        {cam_key: frames},
                        mode=str(job.get("brightnessMode") or "auto"),
                        gain=job.get("brightnessGain"),
                        gamma=job.get("brightnessGamma"),
                    )
                    aug = out[cam_key]
                    cam_meta: dict[str, Any] = {"brightness": meta}
                    episode_meta.setdefault("mode", meta.get("mode"))
                    episode_meta.setdefault("gain", meta.get("gain"))
                    episode_meta.setdefault("gamma", meta.get("gamma"))
                else:
                    assert color is not None and segmenter is not None
                    aug, _masks, cam_meta = apply_color_video(
                        frames,
                        prompts=prompts,
                        apply_mode=apply_mode,
                        color_rgb=color["colorRgb"],
                        segmenter=segmenter,
                    )
                    del _masks
            except Exception as exc:  # noqa: BLE001
                if camera_policy != "partial":
                    raise
                # partial: fall back to the original frames and record it.
                aug = frames
                cam_meta = {"fallback": True, "error": f"{type(exc).__name__}: {exc}"}
                episode_meta["cameraFailures"].append(f"{cam_key}: {exc}")
            writer.add_camera_video(ctx, cam_key, aug)
            episode_meta["cameras"][cam_key] = cam_meta
            del frames, aug

        writer.commit_episode(ctx, state=state, action=action, sidecar=episode_meta)
        return ctx
    except Exception:
        writer.abort_episode(ctx)
        raise


def _run_preview(
    job: dict[str, Any],
    adapter,
    view: DatasetView,
    fps: float,
    progress_callback: ProgressCallback | None,
) -> dict[str, Any]:
    ep_idx = job.get("previewEpisode")
    if ep_idx is None:
        ep_idx = view.episodes[0].episode_index if view.episodes else 0
    ep_idx = int(ep_idx)
    preview_dir = Path(job.get("previewDir") or (DEFAULT_PREVIEW_DIR / str(job["jobId"])))
    preview_dir.mkdir(parents=True, exist_ok=True)

    _emit(progress_callback, progress=0.1, current=0, total=1, message=f"解码测试 episode {ep_idx}…")
    videos, ep_meta = _load_episode_videos(
        adapter,
        view,
        ep_idx,
        max_frames=PREVIEW_FRAME_LIMIT,
    )
    _emit(progress_callback, progress=0.35, current=0, total=1, message="正在生成增强效果…")
    aug, masks, aug_meta = _run_augment_on_videos(
        videos,
        job,
        seed_key=f"{job.get('jobId')}:preview:{ep_idx}",
    )
    _emit(progress_callback, progress=0.85, current=0, total=1, message="写出预览视频…")
    payload = write_preview(
        preview_dir,
        source_videos=videos,
        augmented_videos=aug,
        masks=masks,
        meta={
            **aug_meta,
            "episodeIndex": ep_idx,
            "fps": fps,
            "length": ep_meta["length"],
            "previewFrameLimit": PREVIEW_FRAME_LIMIT,
        },
        fps=fps,
    )
    cameras = []
    for item in payload.get("cameras") or []:
        rel = item.get("relative") or {}
        cameras.append(
            {
                "camera": item["camera"],
                "frameIndex": item["frameIndex"],
                "originalUrl": f"/api/augment/preview-asset/{job['jobId']}/{rel.get('original')}",
                "resultUrl": f"/api/augment/preview-asset/{job['jobId']}/{rel.get('result')}",
                "originalVideoUrl": f"/api/augment/preview-asset/{job['jobId']}/{rel.get('originalVideo')}",
                "resultVideoUrl": f"/api/augment/preview-asset/{job['jobId']}/{rel.get('resultVideo')}",
                "compareVideoUrl": f"/api/augment/preview-asset/{job['jobId']}/{rel.get('compareVideo')}",
                "maskUrl": (
                    f"/api/augment/preview-asset/{job['jobId']}/{rel['mask']}"
                    if rel.get("mask")
                    else None
                ),
                "maskVideoUrl": (
                    f"/api/augment/preview-asset/{job['jobId']}/{rel['maskVideo']}"
                    if rel.get("maskVideo")
                    else None
                ),
            }
        )
    return {
        "mode": "preview",
        "previewDir": str(preview_dir),
        "episodeIndex": ep_idx,
        "colorName": aug_meta.get("colorName"),
        "colorRgb": aug_meta.get("colorRgb"),
        "colorMode": aug_meta.get("colorMode"),
        "brightnessMode": aug_meta.get("mode") if aug_meta.get("augType") == "brightness" else None,
        "brightnessGain": aug_meta.get("gain"),
        "brightnessGamma": aug_meta.get("gamma"),
        "augType": aug_meta.get("augType"),
        "applyMode": aug_meta.get("applyMode"),
        "cameras": cameras,
        "meta": aug_meta,
    }
