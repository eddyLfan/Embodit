"""Format conversion via streamed normalized episode payloads.

Episodes are extracted lazily (generator) and consumed by the target writer as
they arrive, so peak memory stays bounded by a single episode instead of the
whole dataset.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Iterator

from datasets.payload import EpisodePayload
from datasets.registry import get_writer, open_dataset
from datasets.view import FORMAT_LABELS, FORMAT_MCAP, DatasetView
from convert.report import ConversionReport

ProgressCallback = Callable[[dict[str, Any]], None]

# Exact key names accepted when the user did not map state/action explicitly.
# Substring guessing is deliberately avoided: it used to match columns like
# ``action_is_pad`` and silently convert the wrong tensor.
ACTION_KEYS = ("action", "actions")
STATE_KEYS = ("observation.state", "state", "states", "obs.state")


def _emit(progress_callback: ProgressCallback | None, **payload: Any) -> None:
    if progress_callback is None:
        return
    try:
        progress_callback(payload)
    except Exception:  # noqa: BLE001
        import logging

        logging.getLogger(__name__).exception("progress callback failed")


def _pick_series_key(
    series: dict[str, Any],
    explicit: str | None,
    candidates: tuple[str, ...],
    warnings: list[str],
    episode_index: int,
    kind: str,
) -> str | None:
    if explicit:
        if explicit in series:
            return explicit
        warnings.append(f"episode {episode_index}: 指定的 {kind} 列不存在（{explicit}）")
        return None
    for name in candidates:
        if name in series:
            return name
    return None


def iter_episode_payloads(
    adapter,
    view: DatasetView,
    episode_indices: list[int] | None,
    mapping: dict[str, Any],
    warnings: list[str],
    progress_callback: ProgressCallback | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield normalized episode dicts one at a time."""
    indices = episode_indices if episode_indices is not None else [ep.episode_index for ep in view.episodes]
    index_set = set(indices)
    selected = [ep for ep in view.episodes if ep.episode_index in index_set]
    total = len(selected)

    state_key = mapping.get("state_key")
    action_key = mapping.get("action_key")
    on_error = str(mapping.get("on_error") or "fail")
    allow_camera_loss = bool(mapping.get("allow_camera_loss"))

    for offset, ep in enumerate(selected, start=1):
        _emit(
            progress_callback,
            stage="extract",
            current=offset,
            total=total,
            progress=0.05 + 0.85 * ((offset - 1) / max(1, total)),
            message=f"转换 episode {ep.episode_index}（{offset}/{total}）",
        )
        payload = EpisodePayload(
            episode_index=ep.episode_index,
            length=int(ep.length or 0),
            tasks=list(ep.tasks),
        )
        for cam_key, cam in ep.cameras.items():
            if cam.kind == "video" and cam.path:
                payload.video_paths[cam_key] = str(Path(view.path) / cam.path)
            elif cam.kind == "topic" and cam.topic and hasattr(adapter, "materialize_topic_video"):
                try:
                    mp4 = adapter.materialize_topic_video(ep.episode_index, cam.topic)
                    payload.video_paths[cam_key] = str(mp4)
                except Exception as error:  # noqa: BLE001
                    if not allow_camera_loss:
                        raise RuntimeError(
                            f"episode {ep.episode_index} 相机 {cam_key} 无法提取（{error}）；"
                            "如接受丢失相机数据，请在 mapping 中设置 allow_camera_loss=true"
                        ) from error
                    warnings.append(f"episode {ep.episode_index}: 相机 {cam_key} 提取失败（{error}），已丢弃")
            elif cam.kind == "frames" and hasattr(adapter, "get_frames"):
                # Lazy generator: frames are decoded only when the writer consumes them.
                payload.images[cam_key] = adapter.get_frames(ep.episode_index, cam_key)
            else:
                if not allow_camera_loss:
                    raise RuntimeError(
                        f"episode {ep.episode_index} 相机 {cam_key}（kind={cam.kind}）无法保留到目标格式；"
                        "如接受丢失相机数据，请在 mapping 中设置 allow_camera_loss=true"
                    )
                warnings.append(f"episode {ep.episode_index}: 相机 {cam_key}（kind={cam.kind}）无法保留，已丢弃")

        try:
            series = adapter.get_timeseries(ep.episode_index)
        except Exception as error:  # noqa: BLE001
            if on_error == "skip":
                warnings.append(f"episode {ep.episode_index}: timeseries 读取失败（{error}），已跳过该 episode")
                continue
            raise RuntimeError(
                f"episode {ep.episode_index} 的 state/action 读取失败：{error}；"
                "如需跳过失败 episode，请在 mapping 中设置 on_error=skip"
            ) from error

        picked_action = _pick_series_key(series, action_key, ACTION_KEYS, warnings, ep.episode_index, "action")
        if picked_action:
            payload.action = series[picked_action]
        picked_state = _pick_series_key(series, state_key, STATE_KEYS, warnings, ep.episode_index, "state")
        if picked_state:
            payload.state = series[picked_state]

        if payload.action is None and payload.state is None and not payload.video_paths and not payload.images:
            warnings.append(f"episode {ep.episode_index}: no state/action/video extracted")
        yield payload.validate().to_dict()


def convert_dataset(
    source: Path,
    output: Path,
    target_format: str,
    episode_indices: list[int] | None = None,
    mapping: dict[str, Any] | None = None,
    progress_callback: ProgressCallback | None = None,
) -> dict[str, Any]:
    mapping = dict(mapping or {})
    _emit(progress_callback, stage="open", progress=0.02, current=0, total=0, message="打开源数据集…")
    adapter = open_dataset(source)
    source_format = adapter.format_id
    media_mode = str(mapping.get("media_mode") or "hardlink")

    if source_format == target_format:
        # Same-format conversion always uses the adapter's lossless subset
        # export (never the lossy intermediate payload).
        view = adapter.inspect()
        indices = episode_indices if episode_indices is not None else [ep.episode_index for ep in view.episodes]
        _emit(progress_callback, stage="export", progress=0.2, message="同格式导出（保真路径）…")
        result = adapter.export_subset(output, indices, media_mode=media_mode, mapping=mapping)
        _emit(progress_callback, stage="done", progress=1.0, message="导出完成")
        return result

    view = adapter.inspect()
    fps = float(view.fps or 0) or mapping.get("fps")
    if not fps:
        raise ValueError("源数据集未提供 fps，且 mapping 中也未指定 fps；请在 mapping 中显式传入 fps")

    allow_camera_loss = bool(mapping.get("allow_camera_loss"))
    has_cameras = any(ep.cameras for ep in view.episodes)
    if target_format == FORMAT_MCAP and has_cameras and not allow_camera_loss:
        raise ValueError(
            "目标格式 MCAP 暂不支持写入相机图像/视频，转换会丢失所有相机数据；"
            "如接受丢失，请在 mapping 中设置 allow_camera_loss=true"
        )

    warnings: list[str] = []
    known_losses: list[str] = []
    known_losses.append("跨格式转换可能丢失未映射的 topic/特征/标定元数据")
    if target_format == FORMAT_MCAP and has_cameras:
        known_losses.append("MCAP 目标格式不保留相机图像/视频（已显式确认丢失）")

    meta = {
        "fps": float(fps),
        "robot_type": view.robot_type,
        "source_format": view.format_id,
        "features": view.features,
    }
    field_map = {
        "state": mapping.get("state_key") or "|".join(STATE_KEYS),
        "action": mapping.get("action_key") or "|".join(ACTION_KEYS),
        "videos": "camera keys from source view",
    }

    episodes_iter = iter_episode_payloads(
        adapter,
        view,
        episode_indices,
        mapping,
        warnings,
        progress_callback=progress_callback,
    )
    writer = get_writer(target_format)
    write_result = writer.write_from_episodes(output, episodes_iter, meta, mapping)
    total_episodes = int(write_result.get("totalEpisodes") or 0)
    total_frames = int(write_result.get("totalFrames") or 0)
    if total_episodes <= 0:
        raise ValueError("转换未提取到任何 episode")
    _emit(
        progress_callback,
        stage="report",
        current=total_episodes,
        total=total_episodes,
        progress=0.95,
        message="写入转换报告…",
    )

    report = ConversionReport(
        source_format=source_format,
        target_format=target_format,
        source_path=str(Path(source).expanduser().resolve()),
        output_path=str(write_result.get("output") or output),
        episodes=total_episodes,
        frames=total_frames,
        field_map=field_map,
        known_losses=known_losses,
        warnings=warnings,
    )
    out_path = Path(write_result.get("output") or output)
    if out_path.is_dir():
        report_path = out_path / "conversion_report.json"
    else:
        report_path = out_path.with_suffix(out_path.suffix + ".conversion_report.json")
        report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    result = dict(write_result)
    result["report"] = report.to_dict()
    result["reportPath"] = str(report_path)
    result["sourceFormat"] = source_format
    result["targetFormat"] = target_format
    result["sourceFormatLabel"] = FORMAT_LABELS.get(source_format, source_format)
    result["targetFormatLabel"] = FORMAT_LABELS.get(target_format, target_format)
    result["episodes"] = total_episodes
    _emit(
        progress_callback,
        stage="done",
        current=total_episodes,
        total=total_episodes,
        progress=1.0,
        message="转换完成",
    )
    return result
