"""Single-frame offline evaluation against an embodied dataset trajectory."""

from __future__ import annotations

import base64
import io
import math
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


def _names(view: Any, width: int, configured: list[str] | None) -> list[str]:
    feature = view.features.get("action", {}) if isinstance(view.features, dict) else {}
    candidates = feature.get("names") if isinstance(feature, dict) else None
    candidates = candidates if isinstance(candidates, list) else configured
    if isinstance(candidates, list) and len(candidates) == width:
        return [str(item) for item in candidates]
    return [f"action_{index + 1}" for index in range(width)]


def evaluate_dataset_frame(
    adapter: Any,
    *,
    episode_index: int,
    frame_index: int,
    predictor: Predictor,
    prompt: str | None = None,
    action_names: list[str] | None = None,
) -> dict[str, Any]:
    """Run one dataset frame through a resident model and compare its action chunk."""

    view = adapter.inspect()
    episode = _episode(view, episode_index)
    series = adapter.get_timeseries(episode_index)
    action_key, raw_truth = _pick_series(series, "action")
    truth = _matrix(raw_truth, "真实动作")
    state_parts = _series_parts(series, "observation.state")
    state_key = "+".join(key for key, _matrix_value in state_parts)
    states = np.concatenate([matrix for _key, matrix in state_parts], axis=1)
    available = min(int(episode.length or truth.shape[0]), truth.shape[0], states.shape[0])
    if frame_index < 0 or frame_index >= available:
        raise ValueError(f"帧索引必须在 0 到 {max(0, available - 1)} 之间")

    observations: dict[str, Any] = {
        key: matrix[frame_index].tolist()
        for key, matrix in state_parts
    }
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
    names = _names(view, predicted.shape[1], action_names)
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

    return {
        "dataset": view.path,
        "datasetName": view.name,
        "format": view.format_id,
        "episodeIndex": int(episode_index),
        "frameIndex": int(frame_index),
        "fps": float(view.fps),
        "prompt": effective_prompt,
        "state": {"key": state_key, "values": states[frame_index].tolist()},
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
        },
    }
