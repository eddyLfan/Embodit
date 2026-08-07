#!/usr/bin/env python3
"""Generic Embodit-managed runtime for thin Python robot adapters.

The vendor-facing adapter only implements ``observe()``, ``apply_action(row)``
and optional lifecycle hooks. ``start_observation()``/``stop_observation()``
are read-only hooks used by adapter-backed Dry Run; ``start()``/``stop()`` are
reserved for Live mode. Embodit owns transport, dry-run, action validation,
timing, readiness and fault reporting.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import json
import math
import os
import signal
import struct
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def _finite_number(value: Any) -> bool:
    return not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(float(value))


def expand_synthetic(value: Any) -> Any:
    if isinstance(value, list):
        return [expand_synthetic(item) for item in value]
    if not isinstance(value, dict):
        return value
    kind = value.get("$synthetic")
    if kind == "vector":
        length = int(value["length"])
        fill = float(value.get("value", 0))
        if length <= 0 or not math.isfinite(fill):
            raise ValueError("synthetic vector 参数非法")
        return [fill] * length
    if kind == "image":
        width = int(value["width"])
        height = int(value["height"])
        channels = int(value.get("channels", 3))
        fill = int(value.get("value", 0))
        if width <= 0 or height <= 0 or channels not in {1, 3, 4} or not 0 <= fill <= 255:
            raise ValueError("synthetic image 参数非法")
        encoding = {1: "mono8", 3: "rgb8", 4: "rgba8"}[channels]
        payload = bytes([fill]) * width * height * channels
        return {
            "encoding": encoding,
            "width": width,
            "height": height,
            "$binary": base64.b64encode(payload).decode("ascii"),
        }
    return {key: expand_synthetic(item) for key, item in value.items()}


def transport_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("观测包含 NaN 或 Inf")
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$binary": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, dict):
        if isinstance(value.get("data"), (bytes, bytearray, memoryview)):
            result = {key: transport_safe(item) for key, item in value.items() if key != "data"}
            result["$binary"] = base64.b64encode(bytes(value["data"])).decode("ascii")
            return result
        return {str(key): transport_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [transport_safe(item) for item in value]
    detached = value.detach() if callable(getattr(value, "detach", None)) else value
    cpu_value = detached.cpu() if callable(getattr(detached, "cpu", None)) else detached
    if callable(getattr(cpu_value, "tolist", None)):
        return transport_safe(cpu_value.tolist())
    if callable(getattr(value, "item", None)):
        return transport_safe(value.item())
    raise TypeError(f"观测无法转换为 Embodit 协议：{type(value).__name__}")


def _binary_payload(value: dict[str, Any]) -> bytes | None:
    payload = value.get("data")
    if isinstance(payload, (bytes, bytearray, memoryview)):
        return bytes(payload)
    encoded = value.get("$binary")
    if isinstance(encoded, str):
        try:
            return base64.b64decode(encoded, validate=True)
        except ValueError:
            return None
    return None


def _raw_image_bmp(payload: bytes, width: int, height: int, encoding: str) -> bytes | None:
    channels = {"mono8": 1, "rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4}.get(encoding)
    if channels is None or width <= 0 or height <= 0 or len(payload) != width * height * channels:
        return None
    row_size = (width * 3 + 3) & ~3
    pixels = bytearray(row_size * height)
    for output_row, source_row in enumerate(range(height - 1, -1, -1)):
        source_offset = source_row * width * channels
        output_offset = output_row * row_size
        for column in range(width):
            offset = source_offset + column * channels
            if channels == 1:
                red = green = blue = payload[offset]
            elif encoding.startswith("rgb"):
                red, green, blue = payload[offset : offset + 3]
            else:
                blue, green, red = payload[offset : offset + 3]
            target = output_offset + column * 3
            pixels[target : target + 3] = bytes((blue, green, red))
    header_size = 14 + 40
    header = struct.pack("<2sIHHI", b"BM", header_size + len(pixels), 0, 0, header_size)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + info + bytes(pixels)


def _image_preview(value: Any, maximum_bytes: int) -> tuple[str, int | None, int | None] | None:
    if not isinstance(value, dict):
        return None
    payload = _binary_payload(value)
    if payload is None:
        return None
    encoding = str(value.get("encoding") or "").lower()
    mime_type = {"jpeg": "image/jpeg", "jpg": "image/jpeg", "png": "image/png", "webp": "image/webp"}.get(encoding)
    width = value.get("width") if isinstance(value.get("width"), int) else None
    height = value.get("height") if isinstance(value.get("height"), int) else None
    if mime_type is None:
        if width is None or height is None:
            return None
        payload = _raw_image_bmp(payload, width, height, encoding)
        if payload is None:
            return None
        mime_type = "image/bmp"
    if len(payload) > maximum_bytes:
        return None
    data_url = f"data:{mime_type};base64,{base64.b64encode(payload).decode('ascii')}"
    return data_url, width, height


def _numeric_vector(value: Any) -> list[float] | None:
    detached = value.detach() if callable(getattr(value, "detach", None)) else value
    cpu_value = detached.cpu() if callable(getattr(detached, "cpu", None)) else detached
    converted = cpu_value.tolist() if callable(getattr(cpu_value, "tolist", None)) else cpu_value
    if not isinstance(converted, (list, tuple)) or not all(_finite_number(item) for item in converted):
        return None
    return [float(item) for item in converted]


def _display_metadata(spec: dict[str, Any], length: int, prefix: str) -> tuple[list[str], list[str]]:
    configured_names = spec.get("names")
    names = [str(item) for item in configured_names] if isinstance(configured_names, list) else []
    names = (names + [f"{prefix}_{index + 1}" for index in range(len(names), length)])[:length]
    configured_units = spec.get("units")
    if isinstance(configured_units, str):
        units = [configured_units] * length
    elif isinstance(configured_units, list):
        units = [str(item) for item in configured_units]
        units = (units + [""] * length)[:length]
    else:
        units = [""] * length
    return names, units


def model_io_snapshot(
    observations: dict[str, Any],
    actions: list[list[float]],
    latency_ms: float,
    config: dict[str, Any],
    pipeline: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the bounded, model-facing input/output view exposed to the UI."""
    telemetry = config.get("telemetry") if isinstance(config.get("telemetry"), dict) else {}
    maximum_bytes = int(telemetry.get("max_image_bytes", 750_000))
    camera_specs = telemetry.get("cameras")
    if not isinstance(camera_specs, list):
        camera_specs = [
            {"key": key, "label": key}
            for key, value in observations.items()
            if isinstance(value, dict) and isinstance(value.get("encoding"), str)
        ]
    cameras = []
    for spec in camera_specs[:8]:
        if not isinstance(spec, dict) or not isinstance(spec.get("key"), str):
            continue
        preview = _image_preview(observations.get(spec["key"]), maximum_bytes)
        if preview is None:
            continue
        data_url, width, height = preview
        cameras.append(
            {
                "key": spec["key"],
                "label": str(spec.get("label") or spec["key"]),
                "dataUrl": data_url,
                "width": width,
                "height": height,
            }
        )

    state_spec = telemetry.get("state") if isinstance(telemetry.get("state"), dict) else {}
    state_key = str(state_spec.get("key") or config["action"]["baseline_observation"])
    state_values = _numeric_vector(observations.get(state_key))
    state = None
    if state_values is not None:
        state_names, state_units = _display_metadata(state_spec, len(state_values), "state")
        state = {
            "key": state_key,
            "label": str(state_spec.get("label") or state_key),
            "names": state_names,
            "units": state_units,
            "values": state_values,
        }

    action_spec = telemetry.get("action") if isinstance(telemetry.get("action"), dict) else {}
    action_names, action_units = _display_metadata(action_spec, len(actions[0]), "action")
    prompt = observations.get("prompt")
    return {
        "capturedMonotonicNs": time.monotonic_ns(),
        "input": {
            "cameras": cameras,
            "state": state,
            "prompt": prompt if isinstance(prompt, str) else None,
        },
        "output": {
            "action": {
                "names": action_names,
                "units": action_units,
                "chunk": actions,
            },
            "inferenceLatencyMs": float(latency_ms),
            "pipeline": dict(pipeline or {}),
        },
    }


class TrajectoryHistory:
    """Bounded, camera-free timeline of model state, plans and applied actions."""

    def __init__(self, config: dict[str, Any], rate_hz: float):
        telemetry = config.get("telemetry") if isinstance(config.get("telemetry"), dict) else {}
        self.window_s = min(120.0, max(5.0, float(telemetry.get("history_seconds", 20))))
        self.maximum_points = min(10_000, max(100, int(telemetry.get("history_max_points", 2_000))))
        self.period_ns = int(1_000_000_000 / max(rate_hz, 1e-6))
        self.state: list[dict[str, Any]] = []
        self.planned: list[dict[str, Any]] = []
        self.executed: list[dict[str, Any]] = []
        self.names: list[str] = []
        self.units: list[str] = []
        self.state_names: list[str] = []
        self.state_units: list[str] = []
        self.sequence = 0

    def _prune(self) -> None:
        cutoff = time.monotonic_ns() - int(self.window_s * 1_000_000_000)
        for values in (self.state, self.planned, self.executed):
            first = next((index for index, item in enumerate(values) if int(item["tNs"]) >= cutoff), len(values))
            if first:
                del values[:first]
            if len(values) > self.maximum_points:
                del values[:-self.maximum_points]

    def record_inference(self, model_io: dict[str, Any]) -> None:
        captured = int(model_io.get("capturedMonotonicNs") or time.monotonic_ns())
        input_state = (model_io.get("input") or {}).get("state") or {}
        state_values = input_state.get("values")
        if isinstance(state_values, list):
            self.state.append({"tNs": captured, "values": list(state_values)})
            self.state_names = list(input_state.get("names") or self.state_names)
            self.state_units = list(input_state.get("units") or self.state_units)
        action = (model_io.get("output") or {}).get("action") or {}
        chunk = action.get("chunk")
        self.names = list(action.get("names") or self.names)
        self.units = list(action.get("units") or self.units)
        if isinstance(chunk, list):
            self.sequence += 1
            for index, row in enumerate(chunk):
                if isinstance(row, list):
                    self.planned.append(
                        {
                            "tNs": captured + index * self.period_ns,
                            "values": list(row),
                            "chunk": self.sequence,
                            "step": index,
                        }
                    )
        self._prune()

    def record_execution(self, row: list[float], timestamp_ns: int | None = None) -> None:
        self.executed.append({"tNs": timestamp_ns or time.monotonic_ns(), "values": list(row)})
        self._prune()

    def snapshot(self) -> dict[str, Any]:
        self._prune()
        return {
            "windowSeconds": self.window_s,
            "names": list(self.names),
            "units": list(self.units),
            "stateNames": list(self.state_names),
            "stateUnits": list(self.state_units),
            "state": list(self.state),
            "planned": list(self.planned),
            "executed": list(self.executed),
        }


def normalize_action(values: Any, config: dict[str, Any]) -> list[list[float]]:
    width = int(config["width"])
    horizon = int(config.get("horizon", 1))
    if not isinstance(values, list) or len(values) != horizon:
        raise ValueError(f"动作 horizon 必须是 {horizon}")
    normalized: list[list[float]] = []
    for row in values:
        if not isinstance(row, list) or len(row) != width or not all(_finite_number(item) for item in row):
            raise ValueError(f"每帧动作必须是 {width} 维有限数值数组")
        normalized.append([float(item) for item in row])
    return normalized


class ActionSafetyError(ValueError):
    """A well-formed model action was rejected by configured robot safety limits."""


def numerical_tolerances(config: dict[str, Any], width: int) -> list[float]:
    configured = config.get("numerical_tolerance", 1e-6)
    values = configured if isinstance(configured, list) else [configured] * width
    if len(values) != width or any(
        isinstance(value, bool) or not _finite_number(value) or float(value) < 0
        for value in values
    ):
        raise ValueError("动作 numerical_tolerance 必须是非负有限数值或与 width 等长的数组")
    return [float(value) for value in values]


def validate_action(values: Any, config: dict[str, Any], baseline: Any) -> list[list[float]]:
    normalized = normalize_action(values, config)
    width = int(config["width"])
    minimum = config["minimum"]
    maximum = config["maximum"]
    max_step = config["max_step"]
    initial_max_step = config.get("initial_max_step", max_step)
    tolerances = numerical_tolerances(config, width)
    if not all(
        isinstance(row, list) and len(row) == width
        for row in (minimum, maximum, max_step, initial_max_step)
    ):
        raise ValueError("动作限位维度错误")
    if not all(
        _finite_number(item)
        for row in (minimum, maximum, max_step, initial_max_step)
        for item in row
    ):
        raise ValueError("动作限位包含 NaN 或 Inf")
    if any(float(low) >= float(high) for low, high in zip(minimum, maximum)):
        raise ValueError("动作 minimum 必须逐维小于 maximum")
    if any(float(step) <= 0 for row in (max_step, initial_max_step) for step in row):
        raise ValueError("动作 max_step/initial_max_step 必须逐维大于 0")
    if not isinstance(baseline, list) or len(baseline) != width or not all(_finite_number(item) for item in baseline):
        raise ValueError("动作 baseline 不是正确维度的有限数值数组")
    previous = [float(item) for item in baseline]
    for row_index, row in enumerate(normalized):
        for index, value in enumerate(row):
            lower = float(minimum[index])
            upper = float(maximum[index])
            numerical_tolerance = tolerances[index]
            if value < lower - numerical_tolerance or value > upper + numerical_tolerance:
                raise ActionSafetyError(
                    f"动作第 {row_index + 1} 帧第 {index + 1} 维数值 {value:g} "
                    f"超过绝对限位 [{lower:g}, {upper:g}]"
                )
            # Models can produce tiny floating-point overshoots at an exact limit
            # (for example -1e-8 for a gripper whose lower bound is zero). Clamp
            # only values already proven to be within the configured tolerance.
            value = min(max(value, lower), upper)
            row[index] = value
            step_limit = float(initial_max_step[index] if row_index == 0 else max_step[index])
            if abs(value - previous[index]) > step_limit + numerical_tolerance:
                raise ActionSafetyError(
                    f"动作第 {row_index + 1} 帧第 {index + 1} 维从 {previous[index]:g} "
                    f"变化到 {value:g}，超过 "
                    f"{'initial_max_step' if row_index == 0 else 'max_step'}={step_limit:g}"
                )
        previous = row
    return normalized


def resolve_action_scheduler(
    control: dict[str, Any],
    horizon: int,
    *,
    inference_latency_ms: float | None = None,
    rate_hz: float | None = None,
) -> dict[str, Any]:
    """Resolve the Live action scheduler without coupling it to a robot vendor."""
    mode = str(control.get("inference_mode", "synchronous"))
    if mode not in {"synchronous", "asynchronous"}:
        raise ValueError("control.inference_mode 必须是 synchronous 或 asynchronous")
    action_steps = control.get("action_steps", horizon)
    if isinstance(action_steps, bool) or not isinstance(action_steps, int):
        raise ValueError("control.action_steps 必须是整数")
    if not 1 <= action_steps <= horizon:
        raise ValueError("control.action_steps 必须在 1 到 action.horizon 之间")

    asynchronous = control.get("asynchronous", {})
    if not isinstance(asynchronous, dict):
        raise ValueError("control.asynchronous 必须是对象")
    default_request_after: int | str = "auto"
    request_after_steps = asynchronous.get("request_after_steps", default_request_after)
    if request_after_steps != "auto" and (
        isinstance(request_after_steps, bool) or not isinstance(request_after_steps, int)
    ):
        raise ValueError("control.asynchronous.request_after_steps 必须是整数或 auto")
    if mode == "asynchronous" and request_after_steps != "auto" and not 1 <= request_after_steps < action_steps:
        raise ValueError(
            "异步推理要求 control.asynchronous.request_after_steps 在 1 到 action_steps-1 之间"
        )
    latency_margin_ms = float(asynchronous.get("latency_margin_ms", 30))
    if not math.isfinite(latency_margin_ms) or latency_margin_ms < 0:
        raise ValueError("control.asynchronous.latency_margin_ms 必须是非负有限数值")
    prefetch_policy = "auto" if request_after_steps == "auto" else "fixed"
    if request_after_steps == "auto":
        if inference_latency_ms is not None and rate_hz is not None and rate_hz > 0:
            reserved = math.ceil((max(0.0, inference_latency_ms) + latency_margin_ms) * rate_hz / 1000)
            request_after_steps = max(1, min(action_steps - 1, action_steps - max(1, reserved)))
        else:
            request_after_steps = max(1, min(action_steps - 1, math.floor(action_steps * 0.6)))
    return {
        "mode": mode,
        "outputSteps": horizon,
        "actionSteps": action_steps,
        "requestAfterSteps": request_after_steps if mode == "asynchronous" else None,
        "prefetchPolicy": prefetch_policy if mode == "asynchronous" else None,
        "latencyMarginMs": latency_margin_ms if mode == "asynchronous" else None,
    }


def start_async_inference(
    model: "ModelClient",
    observations: dict[str, Any],
    config: dict[str, Any],
    observation_latency_ms: float = 0.0,
) -> tuple[threading.Event, dict[str, Any]]:
    """Start one daemon inference request; the Live loop remains the sole action writer."""
    done = threading.Event()
    result: dict[str, Any] = {}

    def infer() -> None:
        try:
            values, latency_ms = model.infer(observations)
            actions = normalize_action(values, config["action"])
            pipeline = {
                "observationMs": observation_latency_ms,
                **model.last_metrics,
                "endToEndMs": observation_latency_ms
                + float(model.last_metrics.get("requestSerializationMs") or 0)
                + latency_ms,
            }
            result.update(
                {
                    "actions": actions,
                    "latencyMs": latency_ms,
                    "observations": observations,
                    "observationLatencyMs": observation_latency_ms,
                    "modelIo": model_io_snapshot(observations, actions, latency_ms, config, pipeline),
                }
            )
        except Exception as error:  # noqa: BLE001
            result["error"] = error
        finally:
            done.set()

    threading.Thread(target=infer, daemon=True, name="embodit-async-inference").start()
    return done, result


class StatusWriter:
    def __init__(self, path: str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.model_io: dict[str, Any] | None = None
        self.sticky: dict[str, Any] = {}

    def remember_model_io(self, value: dict[str, Any]) -> None:
        self.model_io = value

    def write(self, status: str, **values: Any) -> None:
        if isinstance(values.get("modelIo"), dict):
            self.model_io = values["modelIo"]
        elif self.model_io is not None:
            values["modelIo"] = self.model_io
        for key in ("trajectoryHistory", "scheduler", "runtimeTiming"):
            if key in values:
                self.sticky[key] = values[key]
            elif key in self.sticky:
                values[key] = self.sticky[key]
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"status": status, "updatedMonotonicNs": time.monotonic_ns(), **values}, ensure_ascii=False),
            encoding="utf-8",
        )
        temporary.replace(self.path)


class ModelClient:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.sequence = 0
        self.last_metrics: dict[str, Any] = {}

    def infer(self, observations: dict[str, Any]) -> tuple[Any, float]:
        endpoint = self.config["endpoint"].rstrip("/") + self.config.get("infer_path", "/infer")
        serialization_started = time.perf_counter()
        body = json.dumps(
            {
                "protocolVersion": 2,
                "sequence": self.sequence,
                "capturedMonotonicNs": time.monotonic_ns(),
                "observations": transport_safe(observations),
            },
            separators=(",", ":"),
        ).encode("utf-8")
        serialization_ms = (time.perf_counter() - serialization_started) * 1000
        self.sequence += 1
        request = urllib.request.Request(
            endpoint,
            data=body,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        started = time.perf_counter()
        try:
            with urllib.request.urlopen(request, timeout=float(self.config.get("timeout_s", 120))) as response:
                maximum = int(self.config.get("maximum_response_bytes", 10_000_000))
                payload = response.read(maximum + 1)
                if len(payload) > maximum:
                    raise ValueError("模型响应超过 maximum_response_bytes")
                decode_started = time.perf_counter()
                result = json.loads(payload)
                response_decode_ms = (time.perf_counter() - decode_started) * 1000
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"模型 HTTP {error.code}: {detail}") from error
        latency_ms = (time.perf_counter() - started) * 1000
        self.last_metrics = {
            "requestSerializationMs": serialization_ms,
            "modelRoundTripMs": latency_ms,
            "responseDecodeMs": response_decode_ms,
            "requestBytes": len(body),
            "responseBytes": len(payload),
        }
        action = result.get("action", result) if isinstance(result, dict) else result
        return action.get("values") if isinstance(action, dict) else None, latency_ms


def resolve_adapter(config: dict[str, Any]) -> Any:
    module_search_paths = config.get("module_search_paths", [])
    if not isinstance(module_search_paths, list) or any(not isinstance(path, str) for path in module_search_paths):
        raise ValueError("adapter.module_search_paths 必须是路径字符串数组")
    for path in reversed(module_search_paths):
        sys.path.insert(0, str(Path(path).expanduser()))
    source_path = config.get("source_path")
    if source_path:
        sys.path.insert(0, str(Path(source_path).expanduser()))
    entrypoint = str(config["entrypoint"])
    module_name, separator, attribute_path = entrypoint.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ValueError("adapter.entrypoint 必须使用 module:ClassName 格式")
    target: Any = importlib.import_module(module_name)
    for part in attribute_path.split("."):
        target = getattr(target, part)
    options = config.get("config") or {}
    adapter = target(options) if callable(target) else target
    if not callable(getattr(adapter, "observe", None)) or not callable(getattr(adapter, "apply_action", None)):
        raise TypeError("Python Robot Adapter 必须实现 observe() 和 apply_action(row)")
    return adapter


def mapped_observations(values: dict[str, Any], mapping: dict[str, str] | None) -> dict[str, Any]:
    if not mapping:
        return values
    missing = sorted(source for source in mapping.values() if source not in values)
    if missing:
        raise ValueError("observation_map 引用了缺失观测：" + ", ".join(missing))
    return {target: values[source] for target, source in mapping.items()}


def call_with_timeout(function: Any, timeout_s: float, label: str, *args: Any) -> Any:
    if timeout_s <= 0:
        raise ValueError("watchdog_timeout_s 必须大于 0")

    def expired(_signum: int, _frame: Any) -> None:
        raise TimeoutError(f"Robot Adapter {label} 超过 watchdog_timeout_s={timeout_s:g}")

    previous = signal.signal(signal.SIGALRM, expired)
    signal.setitimer(signal.ITIMER_REAL, timeout_s)
    try:
        return function(*args)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def hold_action_chunk(config: dict[str, Any], baseline: Any) -> list[list[float]]:
    """Build a zero-motion chunk from the measured state after an unsafe output."""
    width = int(config["width"])
    horizon = int(config.get("horizon", 1))
    if not isinstance(baseline, list) or len(baseline) != width or not all(
        _finite_number(item) for item in baseline
    ):
        raise ValueError("无法为不安全动作生成保持指令：baseline 无效")
    # The limits describe valid model outputs, not whether the robot's measured
    # current state exists. Re-validating the baseline here made a safe hold
    # fault whenever the robot started outside the checkpoint's training range.
    row = [float(item) for item in baseline]
    return [list(row) for _ in range(horizon)]


def run(config: dict[str, Any]) -> None:
    mode = os.environ.get("EMBODIT_DEPLOYMENT_MODE", "dry_run")
    if mode not in {"dry_run", "live"}:
        raise ValueError("EMBODIT_DEPLOYMENT_MODE 必须是 dry_run 或 live")
    prompt = str(config.get("task_prompt") or config.get("default_prompt") or "").strip()
    status = StatusWriter(config["status_path"])
    model = ModelClient(config["model"])
    control = config.get("control", {})
    watchdog_timeout_s = float(control.get("watchdog_timeout_s", 1))
    if watchdog_timeout_s <= 0:
        raise ValueError("control.watchdog_timeout_s 必须大于 0")
    stopping = {"value": False}
    signal.signal(signal.SIGTERM, lambda *_args: stopping.__setitem__("value", True))
    signal.signal(signal.SIGINT, lambda *_args: stopping.__setitem__("value", True))
    status.write("starting", mode=mode)
    adapter = None
    dry_run_source = str(config.get("dry_run_observation_source", "synthetic"))
    observation_latency_ms = 0.0

    def observe(current_adapter: Any) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        result = call_with_timeout(current_adapter.observe, watchdog_timeout_s, "observe()")
        latency = (time.perf_counter() - started) * 1000
        if not isinstance(result, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        return result, latency

    try:
        if mode == "dry_run":
            if dry_run_source == "synthetic":
                observations = expand_synthetic(config["dry_run_observations"])
            elif dry_run_source == "adapter":
                adapter = resolve_adapter(config["adapter"])
                observation_starter = getattr(adapter, "start_observation", None)
                if callable(observation_starter):
                    observation_starter()
                observations, observation_latency_ms = observe(adapter)
            else:
                raise ValueError("dry_run_observation_source 必须是 synthetic 或 adapter")
        else:
            adapter = resolve_adapter(config["adapter"])
            starter = getattr(adapter, "start", None)
            if callable(starter):
                starter()
            observations, observation_latency_ms = observe(adapter)
        if not isinstance(observations, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        observations = mapped_observations(observations, config.get("observation_map"))
        if prompt:
            observations["prompt"] = prompt
        baseline_key = str(config["action"]["baseline_observation"])
        values, latency_ms = model.infer(observations)
        actions = normalize_action(values, config["action"])
        pipeline = {
            "observationMs": observation_latency_ms,
            **model.last_metrics,
            "endToEndMs": observation_latency_ms
            + float(model.last_metrics.get("requestSerializationMs") or 0)
            + latency_ms,
        }
        model_io = model_io_snapshot(observations, actions, latency_ms, config, pipeline)
        status.remember_model_io(model_io)
        telemetry_rate_hz = float(
            control.get("dry_run_rate_hz", control.get("rate_hz", 10))
            if mode == "dry_run"
            else control.get("rate_hz", 10)
        )
        trajectory = TrajectoryHistory(config, telemetry_rate_hz)
        trajectory.record_inference(model_io)
        safety_rejections = 0
        last_safety_error = None
        safety_error = None
        try:
            actions = validate_action(actions, config["action"], observations.get(baseline_key))
        except ActionSafetyError as error:
            safety_error = str(error)
            last_safety_error = safety_error
            safety_rejections = 1
            actions = hold_action_chunk(config["action"], observations.get(baseline_key))
        scheduler = (
            resolve_action_scheduler(
                control,
                int(config["action"]["horizon"]),
                inference_latency_ms=latency_ms,
                rate_hz=float(control.get("rate_hz", 10)),
            )
            if mode == "live"
            else None
        )
        status.write(
            "ready",
            mode=mode,
            hardwareActive=mode == "live",
            actionShape=[len(actions), len(actions[0])],
            inferenceLatencyMs=latency_ms,
            scheduler=scheduler,
            safetyPassed=safety_error is None,
            safetyError=last_safety_error,
            safetyRejections=safety_rejections,
            modelIo=model_io,
            trajectoryHistory=trajectory.snapshot(),
        )
        if mode == "dry_run":
            if dry_run_source == "synthetic":
                while not stopping["value"]:
                    time.sleep(0.25)
                return

            assert adapter is not None
            dry_run_rate_hz = float(control.get("dry_run_rate_hz", control.get("rate_hz", 10)))
            if dry_run_rate_hz <= 0:
                raise ValueError("control.dry_run_rate_hz 必须大于 0")
            period = 1.0 / dry_run_rate_hz
            samples = 1
            while not stopping["value"]:
                started = time.monotonic()
                observations, observation_latency_ms = observe(adapter)
                observations = mapped_observations(observations, config.get("observation_map"))
                if prompt:
                    observations["prompt"] = prompt
                values, latency_ms = model.infer(observations)
                actions = normalize_action(values, config["action"])
                pipeline = {
                    "observationMs": observation_latency_ms,
                    **model.last_metrics,
                    "endToEndMs": observation_latency_ms
                    + float(model.last_metrics.get("requestSerializationMs") or 0)
                    + latency_ms,
                }
                model_io = model_io_snapshot(observations, actions, latency_ms, config, pipeline)
                status.remember_model_io(model_io)
                trajectory.record_inference(model_io)
                safety_error = None
                try:
                    actions = validate_action(
                        actions, config["action"], observations.get(baseline_key)
                    )
                except ActionSafetyError as error:
                    # Dry Run is a continuous read-only evaluator. An unsafe model
                    # sample must block Live promotion, but it must not tear down
                    # the observation/inference stream or the resident model.
                    safety_error = str(error)
                    last_safety_error = safety_error
                    safety_rejections += 1
                samples += 1
                status.write(
                    "ready",
                    mode=mode,
                    hardwareActive=False,
                    samples=samples,
                    actionShape=[len(actions), len(actions[0])],
                    inferenceLatencyMs=latency_ms,
                    safetyPassed=safety_error is None,
                    safetyError=last_safety_error,
                    safetyRejections=safety_rejections,
                    modelIo=model_io,
                    trajectoryHistory=trajectory.snapshot(),
                )
                remaining = period - (time.monotonic() - started)
                if remaining > 0:
                    time.sleep(remaining)
            return

        assert adapter is not None
        rate_hz = float(control.get("rate_hz", 10))
        configured_maximum_steps = control.get("max_episode_steps")
        maximum_steps = int(configured_maximum_steps) if configured_maximum_steps is not None else None
        if rate_hz <= 0 or (maximum_steps is not None and maximum_steps <= 0):
            raise ValueError("control.rate_hz 必须大于 0，max_episode_steps 配置时必须大于 0")
        assert scheduler is not None
        period = 1.0 / rate_hz
        steps = 0
        action_steps = int(scheduler["actionSteps"])
        inference_mode = str(scheduler["mode"])
        request_after_steps = scheduler.get("requestAfterSteps")
        actions = actions[:action_steps]
        next_action_at = time.monotonic()
        apply_latencies_ms: list[float] = []
        schedule_lags_ms: list[float] = []
        while not stopping["value"] and (maximum_steps is None or steps < maximum_steps):
            pending: tuple[threading.Event, dict[str, Any]] | None = None
            for chunk_step, action in enumerate(actions, start=1):
                if stopping["value"] or (maximum_steps is not None and steps >= maximum_steps):
                    break
                remaining = next_action_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                applied_at = time.monotonic()
                schedule_lags_ms.append(max(0.0, (applied_at - next_action_at) * 1000))
                apply_started = time.perf_counter()
                call_with_timeout(adapter.apply_action, watchdog_timeout_s, "apply_action()", action)
                apply_latencies_ms.append((time.perf_counter() - apply_started) * 1000)
                del apply_latencies_ms[:-200]
                del schedule_lags_ms[:-200]
                trajectory.record_execution(action, time.monotonic_ns())
                steps += 1
                next_action_at = applied_at + period

                if (
                    inference_mode == "asynchronous"
                    and pending is None
                    and chunk_step == request_after_steps
                    and not stopping["value"]
                    and (maximum_steps is None or steps < maximum_steps)
                ):
                    next_observations, next_observation_latency_ms = observe(adapter)
                    next_observations = mapped_observations(
                        next_observations, config.get("observation_map")
                    )
                    if prompt:
                        next_observations["prompt"] = prompt
                    pending = start_async_inference(
                        model, next_observations, config, next_observation_latency_ms
                    )

            if stopping["value"] or (maximum_steps is not None and steps >= maximum_steps):
                break

            if inference_mode == "asynchronous":
                if pending is None:
                    raise RuntimeError("异步推理未在配置的动作步触发")
                done, inference = pending
                while not done.wait(0.01):
                    if stopping["value"]:
                        break
                if stopping["value"]:
                    break
                if inference.get("error") is not None:
                    raise inference["error"]
                observations = inference["observations"]
                observation_latency_ms = float(inference["observationLatencyMs"])
                next_actions = inference["actions"]
                latency_ms = float(inference["latencyMs"])
                model_io = inference["modelIo"]
                # If inference exceeded the remaining chunk time, resume from now;
                # never burst actions to catch up with stale deadlines.
                next_action_at = max(next_action_at, time.monotonic())
            else:
                observations, observation_latency_ms = observe(adapter)
                observations = mapped_observations(observations, config.get("observation_map"))
                if prompt:
                    observations["prompt"] = prompt
                values, latency_ms = model.infer(observations)
                next_actions = normalize_action(values, config["action"])
                pipeline = {
                    "observationMs": observation_latency_ms,
                    **model.last_metrics,
                    "endToEndMs": observation_latency_ms
                    + float(model.last_metrics.get("requestSerializationMs") or 0)
                    + latency_ms,
                }
                model_io = model_io_snapshot(observations, next_actions, latency_ms, config, pipeline)
                # Synchronous inference intentionally pauses action output. Resume
                # from a fresh clock instead of catching up with an unsafe burst.
                next_action_at = time.monotonic()

            safety_error = None
            try:
                next_actions = validate_action(
                    next_actions, config["action"], observations.get(baseline_key)
                )
            except ActionSafetyError as error:
                # Reject only this chunk and hold the latest measured model state.
                # The resident Client/model stay alive so a later valid inference
                # can resume without a deployment restart.
                safety_error = str(error)
                last_safety_error = safety_error
                safety_rejections += 1
                next_actions = hold_action_chunk(
                    config["action"], observations.get(baseline_key)
                )
            status.remember_model_io(model_io)
            trajectory.record_inference(model_io)
            actions = next_actions[:action_steps]
            scheduler = resolve_action_scheduler(
                control,
                int(config["action"]["horizon"]),
                inference_latency_ms=latency_ms,
                rate_hz=rate_hz,
            )
            request_after_steps = scheduler.get("requestAfterSteps")
            runtime_timing = {
                "targetRateHz": rate_hz,
                "applyMeanMs": sum(apply_latencies_ms) / len(apply_latencies_ms),
                "applyMaxMs": max(apply_latencies_ms),
                "scheduleLagMeanMs": sum(schedule_lags_ms) / len(schedule_lags_ms),
                "scheduleLagMaxMs": max(schedule_lags_ms),
            }
            status.write(
                "ready",
                mode=mode,
                hardwareActive=True,
                steps=steps,
                actionShape=[len(next_actions), len(next_actions[0])],
                inferenceLatencyMs=latency_ms,
                scheduler=scheduler,
                safetyPassed=safety_error is None,
                safetyError=last_safety_error,
                safetyRejections=safety_rejections,
                modelIo=model_io,
                runtimeTiming=runtime_timing,
                trajectoryHistory=trajectory.snapshot(),
            )
        status.write(
            "finished",
            mode=mode,
            hardwareActive=True,
            steps=steps,
            safetyPassed=last_safety_error is None,
            safetyError=last_safety_error,
            safetyRejections=safety_rejections,
            scheduler=scheduler,
            runtimeTiming={
                "targetRateHz": rate_hz,
                "applyMeanMs": sum(apply_latencies_ms) / len(apply_latencies_ms) if apply_latencies_ms else 0,
                "applyMaxMs": max(apply_latencies_ms) if apply_latencies_ms else 0,
                "scheduleLagMeanMs": sum(schedule_lags_ms) / len(schedule_lags_ms) if schedule_lags_ms else 0,
                "scheduleLagMaxMs": max(schedule_lags_ms) if schedule_lags_ms else 0,
            },
            trajectoryHistory=trajectory.snapshot(),
        )
    except Exception as error:
        status.write("fault", mode=mode, error=str(error))
        raise
    finally:
        if adapter is not None:
            stopper_name = "stop_observation" if mode == "dry_run" else "stop"
            stopper = getattr(adapter, stopper_name, None)
            if callable(stopper):
                try:
                    call_with_timeout(stopper, watchdog_timeout_s, f"{stopper_name}()")
                except Exception as error:
                    status.write("fault", mode=mode, error=f"Robot Adapter {stopper_name}() 失败：{error}")


def move_to_pose(config: dict[str, Any], pose: dict[str, Any]) -> dict[str, Any]:
    """Move only the action vector controlled by the configured generic adapter."""
    action_config = dict(config["action"])
    width = int(action_config["width"])
    target = pose.get("values")
    if not isinstance(target, list) or len(target) != width or not all(_finite_number(item) for item in target):
        raise ValueError(f"记录位姿必须包含 {width} 维有限数值")
    duration_s = float(pose.get("duration_s", 3.0))
    if not math.isfinite(duration_s) or duration_s <= 0 or duration_s > 60:
        raise ValueError("位姿移动 duration_s 必须在 0 到 60 秒之间")

    adapter = resolve_adapter(config["adapter"])
    watchdog_timeout_s = float(config.get("control", {}).get("watchdog_timeout_s", 5.0))
    starter = getattr(adapter, "start", None)
    stopper = getattr(adapter, "stop", None)
    try:
        if callable(starter):
            call_with_timeout(starter, watchdog_timeout_s, "start()")
        observations = call_with_timeout(adapter.observe, watchdog_timeout_s, "observe()")
        if not isinstance(observations, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        observations = mapped_observations(observations, config.get("observation_map"))
        baseline_key = str(action_config["baseline_observation"])
        current = observations.get(baseline_key)
        if not isinstance(current, list) or len(current) != width or not all(
            _finite_number(item) for item in current
        ):
            raise ValueError("当前模型关节状态不是正确维度的有限数值数组")
        current = [float(item) for item in current]

        target_check = dict(action_config)
        target_check["horizon"] = 1
        target_check["max_step"] = [
            max(float(step), float(high) - float(low))
            for step, low, high in zip(
                action_config["max_step"], action_config["minimum"], action_config["maximum"]
            )
        ]
        normalized_target = validate_action([target], target_check, current)[0]
        rate_hz = float(config.get("control", {}).get("rate_hz", 10))
        if not math.isfinite(rate_hz) or rate_hz <= 0:
            raise ValueError("control.rate_hz 必须大于 0")
        max_step = [float(item) for item in action_config["max_step"]]
        required_steps = max(
            1,
            max(
                math.ceil(abs(target_value - current_value) / step)
                for target_value, current_value, step in zip(normalized_target, current, max_step)
            ),
            math.ceil(duration_s * rate_hz),
        )
        step_config = dict(action_config)
        step_config["horizon"] = 1
        previous = current
        deadline = time.monotonic()
        for index in range(1, required_steps + 1):
            alpha = index / required_steps
            row = [
                start + (finish - start) * alpha
                for start, finish in zip(current, normalized_target)
            ]
            row = validate_action([row], step_config, previous)[0]
            call_with_timeout(adapter.apply_action, watchdog_timeout_s, "apply_action()", row)
            previous = row
            deadline += 1.0 / rate_hz
            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
        return {"values": normalized_target, "steps": required_steps, "duration_s": required_steps / rate_hz}
    finally:
        if callable(stopper):
            try:
                call_with_timeout(stopper, watchdog_timeout_s, "stop()")
            except Exception:
                pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Embodit generic Python Robot Adapter runtime")
    parser.add_argument("--config", required=True)
    parser.add_argument("--move-pose")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.move_pose:
        pose = json.loads(Path(args.move_pose).read_text(encoding="utf-8"))
        print(json.dumps(move_to_pose(config, pose), ensure_ascii=False))
    else:
        run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
