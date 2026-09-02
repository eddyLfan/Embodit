"""Single-frame offline evaluation against an embodied dataset trajectory."""

from __future__ import annotations

import base64
import io
import math
import re
from typing import Any, Callable

import numpy as np
from PIL import Image

from datasets.frames import episode_frame_source


Predictor = Callable[[dict[str, Any]], dict[str, Any]]


def _episode(view: Any, episode_index: int) -> Any:
    for episode in view.episodes:
        if int(episode.episode_index) == int(episode_index):
            return episode
    raise ValueError(f"episode {episode_index} 不存在")


def _matrix(value: Any, label: str) -> np.ndarray:
    array = np.asarray(value, dtype=np.float64)
    if array.ndim == 1:
        array = array[:, None]
    elif array.ndim > 2:
        array = array.reshape(array.shape[0], -1)
    if array.ndim != 2 or not array.shape[0] or not array.shape[1]:
        raise ValueError(f"{label} 必须是非空的 [时间, 维度] 数组")
    if not np.all(np.isfinite(array)):
        raise ValueError(f"{label} 包含 NaN 或 Inf")
    return array


def _series_parts(
    series: dict[str, np.ndarray],
    preferred: str,
) -> list[tuple[str, np.ndarray]]:
    if preferred in series:
        return [(preferred, _matrix(series[preferred], preferred))]
    token = "action" if preferred == "action" else "state"
    matches = sorted(key for key in series if token in key.lower())
    if matches:
        matrices: list[tuple[str, np.ndarray]] = []
        for key in matches:
            matrices.append((key, _matrix(series[key], key)))
        if matrices:
            length = min(matrix.shape[0] for _key, matrix in matrices)
            return [(key, matrix[:length]) for key, matrix in matrices]
    raise ValueError(f"该 episode 缺少 {preferred} 轨迹")


def _pick_series(series: dict[str, np.ndarray], preferred: str) -> tuple[str, np.ndarray]:
    parts = _series_parts(series, preferred)
    if len(parts) == 1:
        return parts[0]
    return "+".join(key for key, _matrix_value in parts), np.concatenate(
        [matrix for _key, matrix in parts],
        axis=1,
    )


def _image_at(source: Any, frame_index: int) -> np.ndarray:
    for index, frame in source.iter_rgb_samples(1):
        if index == frame_index and frame is not None:
            return np.asarray(frame, dtype=np.uint8)
        if index > frame_index:
            break
    raise ValueError(f"相机轨迹没有第 {frame_index} 帧")


def _encoded_image(frame: np.ndarray) -> tuple[dict[str, Any], str]:
    image = Image.fromarray(frame[..., :3], mode="RGB")
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=90)
    encoded = base64.b64encode(buffer.getvalue()).decode("ascii")
    payload = {
        "encoding": "jpeg",
        "height": int(frame.shape[0]),
        "width": int(frame.shape[1]),
        "$binary": encoded,
    }
    return payload, f"data:image/jpeg;base64,{encoded}"


def _action_values(result: dict[str, Any]) -> np.ndarray:
    if not isinstance(result, dict):
        raise ValueError("模型推理结果必须是对象")
    value: Any = result.get("action")
    if isinstance(value, dict):
        value = value.get("values", value.get("chunk", value.get("actions")))
    if value is None:
        value = result.get("actions")
    array = np.asarray(value, dtype=np.float64)
    if array.ndim == 3:
        if array.shape[0] != 1:
            raise ValueError("模型动作 batch 必须为 1")
        array = array[0]
    if array.ndim == 1:
        array = array[None, :]
    elif array.ndim > 2:
        raise ValueError("模型动作必须是 [维度]、[horizon, 维度] 或 [1, horizon, 维度] 数组")
    if array.ndim != 2 or not array.shape[0] or not array.shape[1]:
        raise ValueError("模型动作必须是 [horizon, 维度] 数组")
    if not np.all(np.isfinite(array)):
        raise ValueError("模型动作包含 NaN 或 Inf")
    return array


def _feature_names(view: Any, key: str, width: int) -> list[str]:
    features = view.features if isinstance(view.features, dict) else {}
    feature = features.get(key, {})
    candidates = feature.get("names") if isinstance(feature, dict) else None
    if isinstance(candidates, list) and len(candidates) == width:
        names = [str(item).strip() for item in candidates]
        if all(names) and len(set(names)) == width:
            return names
    return []


def _synthetic_names(names: list[str]) -> bool:
    if not names:
        return True
    return all(
        re.fullmatch(
            r"(?:action|actions|state|states|joint|command|merge_pose|dimension)[._-]?\d+",
            name.strip().lower(),
        )
        is not None
        for name in names
    )


def _configured_names(configured: list[str] | None, label: str) -> list[str]:
    if configured is None:
        return []
    if not isinstance(configured, list):
        raise ValueError(f"{label}配置 names 必须是字符串数组")
    names = [str(item).strip() for item in configured]
    if not names or not all(names) or len(set(names)) != len(names):
        raise ValueError(f"{label}配置 names 不能为空或重复")
    return names


def _project_dimensions(
    matrix: np.ndarray,
    *,
    source_names: list[str],
    target_names: list[str] | None,
    label: str,
) -> tuple[np.ndarray, list[str], dict[str, Any]]:
    """Select and reorder recorded columns by the robot's configured names."""
    width = int(matrix.shape[1])
    source = source_names if len(source_names) == width else []
    target = _configured_names(target_names, label)
    if not target:
        names = source or [f"{label}_{index + 1}" for index in range(width)]
        indices = list(range(width))
    elif source and all(name in source for name in target):
        lookup = {name: index for index, name in enumerate(source)}
        indices = [lookup[name] for name in target]
        names = target
    elif width == len(target) and _synthetic_names(source):
        # Positional fallback is only safe when widths already agree and the
        # dataset has no meaningful names. Wider anonymous arrays are rejected.
        indices = list(range(width))
        names = target
    else:
        missing = [name for name in target if name not in source]
        suffix = f"；缺少 {', '.join(missing[:6])}" if missing else ""
        raise ValueError(
            f"无法将数据集 {label} {width} 维映射到本体配置的 {len(target)} 维"
            f"{suffix}。请在数据中保存维度名称或增加对应数据格式适配器"
        )
    projected = matrix[:, indices]
    return projected, names, {
        "sourceWidth": width,
        "sourceNames": source,
        "sourceIndices": indices,
        "droppedDimensions": width - len(indices),
    }


def _part_names(view: Any, parts: list[tuple[str, np.ndarray]]) -> list[str]:
    names: list[str] = []
    for key, matrix in parts:
        part_names = _feature_names(view, key, int(matrix.shape[1]))
        if not part_names:
            return []
        names.extend(part_names)
    return names


def load_dataset_action_replay(
    adapter: Any,
    *,
    episode_index: int,
    start_frame: int = 0,
    end_frame: int | None = None,
    action_names: list[str] | None = None,
) -> dict[str, Any]:
    """Load an undownsampled recorded action segment for robot replay."""

    view = adapter.inspect()
    episode = _episode(view, episode_index)
    action_key, raw_actions = _pick_series(
        adapter.get_timeseries(episode_index), "action"
    )
    actions = _matrix(raw_actions, "真实动作")
    actions, names, mapping = _project_dimensions(
        actions,
        source_names=_feature_names(view, "action", int(actions.shape[1])),
        target_names=action_names,
        label="action",
    )
    available = min(int(episode.length or actions.shape[0]), actions.shape[0])
    stop = available if end_frame is None else min(int(end_frame), available)
    if start_frame < 0 or start_frame >= available:
        raise ValueError(f"起始帧必须在 0 到 {max(0, available - 1)} 之间")
    if stop <= start_frame:
        raise ValueError("结束帧必须晚于起始帧")
    segment = actions[start_frame:stop]
    if segment.shape[0] > 100_000:
        raise ValueError("单次真机 Replay 不能超过 100000 帧")
    fps = float(view.fps)
    if not math.isfinite(fps) or fps <= 0 or fps > 200:
        raise ValueError("数据集 FPS 必须在 0 到 200 之间")
    return {
        "dataset": view.path,
        "datasetName": view.name,
        "format": view.format_id,
        "episodeIndex": int(episode_index),
        "startFrame": int(start_frame),
        "endFrame": int(stop),
        "fps": fps,
        "action": {
            "key": action_key,
            "names": names,
            "values": segment.tolist(),
            **mapping,
        },
    }


def evaluate_dataset_frame(
    adapter: Any,
    *,
    episode_index: int,
    frame_index: int,
    predictor: Predictor,
    prompt: str | None = None,
    action_names: list[str] | None = None,
    state_names: list[str] | None = None,
) -> dict[str, Any]:
    """Run one dataset frame through a resident model and compare its action chunk."""

    view = adapter.inspect()
    episode = _episode(view, episode_index)
    series = adapter.get_timeseries(episode_index)
    action_key, raw_truth = _pick_series(series, "action")
    truth = _matrix(raw_truth, "真实动作")
    state_parts = _series_parts(series, "observation.state")
    state_key = "+".join(key for key, _matrix_value in state_parts)
    truth, names, action_mapping = _project_dimensions(
        truth,
        source_names=_feature_names(view, "action", int(truth.shape[1])),
        target_names=action_names,
        label="action",
    )
    states = np.concatenate([matrix for _key, matrix in state_parts], axis=1)
    available = min(int(episode.length or truth.shape[0]), truth.shape[0], states.shape[0])
    if frame_index < 0 or frame_index >= available:
        raise ValueError(f"帧索引必须在 0 到 {max(0, available - 1)} 之间")
    states, projected_state_names, state_mapping = _project_dimensions(
        states,
        source_names=_part_names(view, state_parts),
        target_names=state_names,
        label="state",
    )

    if state_names is not None:
        observation_state_key = state_parts[0][0] if len(state_parts) == 1 else "observation.state"
        observations: dict[str, Any] = {
            observation_state_key: states[frame_index].tolist()
        }
    else:
        observations = {key: matrix[frame_index].tolist() for key, matrix in state_parts}
    images: list[dict[str, Any]] = []
    for camera_key in sorted(episode.cameras):
        source = episode_frame_source(adapter, view, episode, camera_key)
        if source is None:
            raise ValueError(f"相机 {camera_key} 不支持读取单帧")
        image_payload, data_url = _encoded_image(_image_at(source, frame_index))
        observations[camera_key] = image_payload
        images.append(
            {
                "key": camera_key,
                "label": camera_key,
                "width": image_payload["width"],
                "height": image_payload["height"],
                "dataUrl": data_url,
            }
        )
    effective_prompt = (prompt or "").strip() or next(
        (str(item).strip() for item in episode.tasks if str(item).strip()),
        "",
    )
    if effective_prompt:
        observations["prompt"] = effective_prompt

    predicted = _action_values(predictor(observations))
    requested_horizon = int(predicted.shape[0])
    end = min(frame_index + predicted.shape[0], available)
    aligned_truth = truth[frame_index:end]
    predicted = predicted[: aligned_truth.shape[0]]
    if not predicted.shape[0]:
        raise ValueError("所选帧之后没有可对比的真实动作")
    if predicted.shape[1] != aligned_truth.shape[1]:
        raise ValueError(
            f"模型动作维度 {predicted.shape[1]} 与真实动作维度 {aligned_truth.shape[1]} 不一致"
        )

    error = predicted - aligned_truth
    dimensions = []
    for index, name in enumerate(names):
        values = error[:, index]
        dimensions.append(
            {
                "index": index,
                "name": name,
                "mae": float(np.mean(np.abs(values))),
                "rmse": float(math.sqrt(float(np.mean(np.square(values))))),
                "maxAbsError": float(np.max(np.abs(values))),
            }
        )

    state_result: dict[str, Any] = {
        "key": state_key,
        "values": states[frame_index].tolist(),
    }
    if state_names is not None:
        state_result.update({"names": projected_state_names, **state_mapping})
    return {
        "dataset": view.path,
        "datasetName": view.name,
        "format": view.format_id,
        "episodeIndex": int(episode_index),
        "frameIndex": int(frame_index),
        "fps": float(view.fps),
        "prompt": effective_prompt,
        "state": state_result,
        "images": images,
        "action": {
            "key": action_key,
            "names": names,
            "predicted": predicted.tolist(),
            "groundTruth": aligned_truth.tolist(),
            "error": error.tolist(),
            "requestedHorizon": requested_horizon,
            "comparedHorizon": int(predicted.shape[0]),
            "dimensions": dimensions,
            "overallMae": float(np.mean(np.abs(error))),
            "overallRmse": float(math.sqrt(float(np.mean(np.square(error))))),
            **action_mapping,
        },
    }
