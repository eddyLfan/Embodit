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
    preview = observation_preview_snapshot(observations, config)
    action_spec = (
        config.get("telemetry", {}).get("action")
        if isinstance(config.get("telemetry"), dict)
        else {}
    )
    action_spec = action_spec if isinstance(action_spec, dict) else {}
    action_names, action_units = _display_metadata(action_spec, len(actions[0]), "action")
    prompt = observations.get("prompt")
    return {
        "capturedMonotonicNs": time.monotonic_ns(),
        "input": {
            "cameras": preview["cameras"],
            "state": preview["state"],
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


def observation_preview_snapshot(
    observations: dict[str, Any],
    config: dict[str, Any],
) -> dict[str, Any]:
    """Build a camera/state preview independent from model inference cadence."""
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

    return {
        "capturedMonotonicNs": time.monotonic_ns(),
        "cameras": cameras,
        "state": state,
    }


class LivePreviewPublisher:
    """Publish recent observations without coupling UI video to inference calls."""

    def __init__(
        self,
        path: str,
        observe: Any,
        config: dict[str, Any],
    ) -> None:
        self.path = Path(path)
        self.observe = observe
        self.config = config
        telemetry = config.get("telemetry") if isinstance(config.get("telemetry"), dict) else {}
        self.rate_hz = min(15.0, max(0.5, float(telemetry.get("preview_rate_hz", 8))))
        self.stop_requested = threading.Event()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        if self.thread is not None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.thread = threading.Thread(
            target=self._run,
            daemon=True,
            name="embodit-live-preview",
        )
        self.thread.start()

    def stop(self) -> None:
        self.stop_requested.set()
        if self.thread is not None:
            self.thread.join(timeout=2)

    def _run(self) -> None:
        period = 1.0 / self.rate_hz
        deadline = time.monotonic()
        while not self.stop_requested.is_set():
            try:
                observations = self.observe()
                value = observation_preview_snapshot(observations, self.config)
                value["rateHz"] = self.rate_hz
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.path)
            except Exception as error:  # noqa: BLE001
                value = {
                    "capturedMonotonicNs": time.monotonic_ns(),
                    "error": str(error),
                    "rateHz": self.rate_hz,
                }
                temporary = self.path.with_suffix(self.path.suffix + ".tmp")
                temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
                temporary.replace(self.path)
            deadline += period
            remaining = deadline - time.monotonic()
            if remaining > 0:
                self.stop_requested.wait(remaining)
            else:
                deadline = time.monotonic()


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
            try:
                skipped = int(action.get("skippedPrefixSteps") or 0)
            except (TypeError, ValueError):
                skipped = 0
            skipped = min(len(chunk), max(0, skipped))
            self.sequence += 1
            for index, row in enumerate(chunk[skipped:], start=skipped):
                if isinstance(row, list):
                    self.planned.append(
                        {
                            "tNs": captured + (index - skipped) * self.period_ns,
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


def resolve_action_constraints(
    configured: dict[str, Any],
    adapter: Any | None,
    *,
    timeout_s: float | None = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Prefer absolute limits reported by the connected robot over copied values."""
    effective = dict(configured)
    metadata: dict[str, Any] = {"source": "config", "fields": []}
    getter = getattr(adapter, "get_action_limits", None) if adapter is not None else None
    if not callable(getter):
        return effective, metadata

    reported = (
        call_with_timeout(getter, timeout_s, "get_action_limits()")
        if timeout_s is not None
        else getter()
    )
    if not isinstance(reported, dict):
        raise TypeError("Robot Adapter get_action_limits() 必须返回对象")
    if "minimum" not in reported or "maximum" not in reported:
        raise ValueError("Robot Adapter get_action_limits() 必须包含 minimum 和 maximum")
    width = int(configured["width"])
    limits: dict[str, list[float]] = {}
    for key in ("minimum", "maximum"):
        values = reported[key]
        if (
            not isinstance(values, list)
            or len(values) != width
            or not all(_finite_number(item) for item in values)
        ):
            raise ValueError(f"Robot Adapter {key} 必须是 {width} 维有限数值数组")
        limits[key] = [float(item) for item in values]
    if any(low >= high for low, high in zip(limits["minimum"], limits["maximum"])):
        raise ValueError("Robot Adapter minimum 必须逐维小于 maximum")

    effective.update(limits)
    metadata = {
        "source": "adapter",
        "fields": ["minimum", "maximum"],
        **limits,
    }
    return effective, metadata


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


def align_async_action_chunk(
    actions: list[list[float]],
    *,
    elapsed_steps: int,
    maximum_steps: int,
) -> list[list[float]]:
    """Drop action rows that became stale while an asynchronous request ran."""
    if isinstance(elapsed_steps, bool) or not isinstance(elapsed_steps, int) or elapsed_steps < 0:
        raise ValueError("异步动作 elapsed_steps 必须是非负整数")
    if isinstance(maximum_steps, bool) or not isinstance(maximum_steps, int) or maximum_steps <= 0:
        raise ValueError("异步动作 maximum_steps 必须是正整数")
    available = min(len(actions), maximum_steps)
    if elapsed_steps >= available:
        raise RuntimeError(
            f"异步推理返回时整段动作已过期：elapsed_steps={elapsed_steps}, available={available}"
        )
    return [list(row) for row in actions[elapsed_steps:available]]


def resolve_runtime_action_scheduler(
    control: dict[str, Any],
    horizon: int,
    *,
    available_steps: int,
    skipped_steps: int = 0,
    inference_latency_ms: float | None = None,
    rate_hz: float | None = None,
) -> dict[str, Any]:
    """Resolve the scheduler for the non-stale suffix currently available."""
    configured = resolve_action_scheduler(
        control,
        horizon,
        inference_latency_ms=inference_latency_ms,
        rate_hz=rate_hz,
    )
    if configured["mode"] != "asynchronous":
        return configured
    if isinstance(available_steps, bool) or not isinstance(available_steps, int):
        raise ValueError("异步动作 available_steps 必须是整数")
    execution_steps = min(int(configured["actionSteps"]), available_steps)
    if execution_steps < 2:
        raise RuntimeError("异步推理没有足够的新鲜动作继续预取")

    runtime_control = dict(control)
    runtime_control["action_steps"] = execution_steps
    asynchronous = dict(runtime_control.get("asynchronous") or {})
    configured_request_after = asynchronous.get("request_after_steps", "auto")
    adjusted = False
    if isinstance(configured_request_after, int) and not isinstance(configured_request_after, bool):
        if configured_request_after >= execution_steps:
            asynchronous["request_after_steps"] = execution_steps - 1
            adjusted = True
    runtime_control["asynchronous"] = asynchronous
    runtime = resolve_action_scheduler(
        runtime_control,
        horizon,
        inference_latency_ms=inference_latency_ms,
        rate_hz=rate_hz,
    )
    runtime["configuredActionSteps"] = int(configured["actionSteps"])
    runtime["skippedPrefixSteps"] = int(skipped_steps)
    runtime["prefetchAdjusted"] = adjusted
    return runtime


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
        for key in (
            "trajectoryHistory",
            "scheduler",
            "runtimeTiming",
            "actionConstraints",
        ):
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
        sequence = self.sequence
        body = json.dumps(
            {
                "protocolVersion": 2,
                "sequence": sequence,
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
        if not isinstance(result, dict):
            raise RuntimeError("模型响应必须是对象")
        protocol_version = result.get("protocolVersion")
        if protocol_version not in {None, 2}:
            raise RuntimeError(f"模型响应协议版本不兼容：{protocol_version}")
        response_sequence = result.get("sequence")
        if response_sequence is not None and response_sequence != sequence:
            raise RuntimeError(
                f"模型响应序号不匹配：request={sequence}, response={response_sequence}"
            )
        server_metrics = result.get("metrics")
        if isinstance(server_metrics, dict):
            server_inference_ms = server_metrics.get("serverInferenceMs")
            if _finite_number(server_inference_ms):
                self.last_metrics["serverInferenceMs"] = float(server_inference_ms)
                self.last_metrics["networkAndProtocolMs"] = max(
                    0.0, latency_ms - float(server_inference_ms)
                )
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
    if mode not in {"observe", "dry_run", "live"}:
        raise ValueError("EMBODIT_DEPLOYMENT_MODE 必须是 observe、dry_run 或 live")
    prompt = str(config.get("task_prompt") or config.get("default_prompt") or "").strip()
    runtime_control_path = config.get("runtime_control_path")
    runtime_control_file = Path(runtime_control_path) if isinstance(runtime_control_path, str) else None
    runtime_control_mtime_ns: int | None = None

    def refresh_prompt() -> str:
        nonlocal prompt, runtime_control_mtime_ns
        if runtime_control_file is None:
            return prompt
        try:
            stat = runtime_control_file.stat()
            if stat.st_mtime_ns == runtime_control_mtime_ns:
                return prompt
            value = json.loads(runtime_control_file.read_text(encoding="utf-8"))
            next_prompt = value.get("task_prompt") if isinstance(value, dict) else None
            if isinstance(next_prompt, str) and next_prompt.strip():
                prompt = next_prompt.strip()
                runtime_control_mtime_ns = stat.st_mtime_ns
        except (OSError, ValueError, json.JSONDecodeError):
            # A writer may briefly be replacing the small control file. Keep the
            # last valid prompt and retry on the next observation/inference.
            pass
        return prompt
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
    preview_publisher: LivePreviewPublisher | None = None
    observation_lock = threading.Lock()
    dry_run_source = str(config.get("dry_run_observation_source", "synthetic"))
    observation_latency_ms = 0.0

    def observe(current_adapter: Any) -> tuple[dict[str, Any], float]:
        started = time.perf_counter()
        with observation_lock:
            result = call_with_timeout(current_adapter.observe, watchdog_timeout_s, "observe()")
        latency = (time.perf_counter() - started) * 1000
        if not isinstance(result, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        return result, latency

    def observe_preview(current_adapter: Any) -> dict[str, Any]:
        with observation_lock:
            result = current_adapter.observe()
        if not isinstance(result, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        return mapped_observations(result, config.get("observation_map"))

    try:
        if mode == "observe":
            adapter = resolve_adapter(config["adapter"])
            observation_starter = getattr(adapter, "start_observation", None)
            if callable(observation_starter):
                observation_starter()
            observations, observation_latency_ms = observe(adapter)
        elif mode == "dry_run":
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
        if adapter is not None and config.get("preview_status_path"):
            preview_publisher = LivePreviewPublisher(
                str(config["preview_status_path"]),
                lambda: observe_preview(adapter),
                config,
            )
            preview_publisher.start()
        if mode == "observe":
            status.write(
                "ready",
                mode=mode,
                hardwareActive=False,
                observationOnly=True,
                observationLatencyMs=observation_latency_ms,
            )
            while not stopping["value"]:
                time.sleep(0.25)
            return
        effective_action, action_constraints = resolve_action_constraints(
            config["action"], adapter, timeout_s=watchdog_timeout_s
        )
        config = {**config, "action": effective_action}
        active_prompt = refresh_prompt()
        if active_prompt:
            observations["prompt"] = active_prompt
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
        scheduler = (
            resolve_runtime_action_scheduler(
                control,
                int(config["action"]["horizon"]),
                available_steps=min(
                    len(actions), int(control.get("action_steps", len(actions)))
                ),
                inference_latency_ms=latency_ms,
                rate_hz=float(control.get("rate_hz", 10)),
            )
            if mode == "live"
            else None
        )
        if scheduler is not None:
            actions = actions[: int(scheduler["actionSteps"])]
        safety_rejections = 0
        last_safety_error = None
        safety_error = None
        safety_config = dict(config["action"])
        safety_config["horizon"] = len(actions)
        try:
            actions = validate_action(actions, safety_config, observations.get(baseline_key))
        except ActionSafetyError as error:
            safety_error = str(error)
            last_safety_error = safety_error
            safety_rejections = 1
            actions = hold_action_chunk(safety_config, observations.get(baseline_key))
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
            actionConstraints=action_constraints,
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
                active_prompt = refresh_prompt()
                if active_prompt:
                    observations["prompt"] = active_prompt
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
        configured_action_steps = int(control.get("action_steps", config["action"]["horizon"]))
        inference_mode = str(scheduler["mode"])
        request_after_steps = scheduler.get("requestAfterSteps")
        actions = actions[:action_steps]
        next_action_at = time.monotonic()
        last_applied_action: list[float] | None = None
        apply_latencies_ms: list[float] = []
        schedule_lags_ms: list[float] = []
        while not stopping["value"] and (maximum_steps is None or steps < maximum_steps):
            pending: tuple[threading.Event, dict[str, Any]] | None = None
            pending_capture_step: int | None = None
            for chunk_step, action in enumerate(actions, start=1):
                if stopping["value"] or (maximum_steps is not None and steps >= maximum_steps):
                    break
                if inference_mode == "asynchronous" and pending is not None and pending[0].is_set():
                    break
                remaining = next_action_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if inference_mode == "asynchronous" and pending is not None and pending[0].is_set():
                    break
                applied_at = time.monotonic()
                schedule_lags_ms.append(max(0.0, (applied_at - next_action_at) * 1000))
                apply_started = time.perf_counter()
                call_with_timeout(adapter.apply_action, watchdog_timeout_s, "apply_action()", action)
                last_applied_action = list(action)
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
                    active_prompt = refresh_prompt()
                    if active_prompt:
                        next_observations["prompt"] = active_prompt
                    pending = start_async_inference(
                        model, next_observations, config, next_observation_latency_ms
                    )
                    pending_capture_step = steps

            if stopping["value"] or (maximum_steps is not None and steps >= maximum_steps):
                break

            if inference_mode == "asynchronous":
                if pending is None:
                    raise RuntimeError("异步推理未在配置的动作步触发")
                if pending_capture_step is None:
                    raise RuntimeError("异步推理缺少观测对应的执行步")
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
                skipped_prefix_steps = steps - pending_capture_step
                next_actions = align_async_action_chunk(
                    next_actions,
                    elapsed_steps=skipped_prefix_steps,
                    maximum_steps=configured_action_steps,
                )
                model_io["output"]["action"]["skippedPrefixSteps"] = skipped_prefix_steps
                # If inference exceeded the remaining chunk time, resume from now;
                # never burst actions to catch up with stale deadlines.
                next_action_at = max(next_action_at, time.monotonic())
            else:
                observations, observation_latency_ms = observe(adapter)
                observations = mapped_observations(observations, config.get("observation_map"))
                active_prompt = refresh_prompt()
                if active_prompt:
                    observations["prompt"] = active_prompt
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
                skipped_prefix_steps = 0

            if inference_mode == "synchronous":
                next_actions = next_actions[:configured_action_steps]

            safety_error = None
            safety_baseline = observations.get(baseline_key)
            safety_config = dict(config["action"])
            safety_config["horizon"] = len(next_actions)
            if inference_mode == "asynchronous":
                if last_applied_action is None:
                    raise RuntimeError("异步动作连续性检查缺少最后已执行动作")
                safety_baseline = last_applied_action
                safety_config["initial_max_step"] = list(safety_config["max_step"])
            try:
                next_actions = validate_action(
                    next_actions, safety_config, safety_baseline
                )
            except ActionSafetyError as error:
                # Reject only this chunk and hold the latest measured model state.
                # The resident Client/model stay alive so a later valid inference
                # can resume without a deployment restart.
                safety_error = str(error)
                last_safety_error = safety_error
                safety_rejections += 1
                next_actions = hold_action_chunk(
                    safety_config, safety_baseline
                )
            status.remember_model_io(model_io)
            trajectory.record_inference(model_io)
            actions = next_actions
            scheduler = resolve_runtime_action_scheduler(
                control,
                int(config["action"]["horizon"]),
                available_steps=len(actions),
                skipped_steps=skipped_prefix_steps,
                inference_latency_ms=latency_ms,
                rate_hz=rate_hz,
            )
            action_steps = int(scheduler["actionSteps"])
            actions = actions[:action_steps]
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
        if preview_publisher is not None:
            preview_publisher.stop()
        if adapter is not None:
            stopper_name = "stop_observation" if mode in {"observe", "dry_run"} else "stop"
            stopper = getattr(adapter, stopper_name, None)
            if callable(stopper):
                try:
                    call_with_timeout(stopper, watchdog_timeout_s, f"{stopper_name}()")
                except Exception as error:
                    status.write("fault", mode=mode, error=f"Robot Adapter {stopper_name}() 失败：{error}")


def replay_actions(config: dict[str, Any], replay: dict[str, Any]) -> dict[str, Any]:
    """Replay an absolute joint-action trajectory through the generic adapter."""

    raw_actions = replay.get("actions")
    width = int(config["action"]["width"])
    if (
        not isinstance(raw_actions, list)
        or not raw_actions
        or len(raw_actions) > 100_000
        or any(
            not isinstance(row, list)
            or len(row) != width
            or not all(_finite_number(value) for value in row)
            for row in raw_actions
        )
    ):
        raise ValueError(f"Replay 动作必须是非空且不超过 100000 帧的 [时间, {width}] 数组")
    actions = [[float(value) for value in row] for row in raw_actions]
    fps = float(replay.get("fps", 0))
    move_duration_s = float(replay.get("move_to_start_duration_s", 3.0))
    control = config.get("control", {})
    control = control if isinstance(control, dict) else {}
    rate_hz = float(control.get("rate_hz", 10))
    watchdog_timeout_s = float(control.get("watchdog_timeout_s", 1))
    if not math.isfinite(fps) or fps <= 0 or fps > rate_hz * 1.01:
        raise ValueError(
            f"Replay FPS 必须大于 0 且不高于本体控制频率 {rate_hz:g} Hz"
        )
    if not math.isfinite(move_duration_s) or not 0 < move_duration_s <= 60:
        raise ValueError("move_to_start_duration_s 必须在 0 到 60 秒之间")

    status = StatusWriter(config["status_path"])
    adapter = resolve_adapter(config["adapter"])
    stopping = {"value": False}
    signal.signal(signal.SIGTERM, lambda *_args: stopping.__setitem__("value", True))
    signal.signal(signal.SIGINT, lambda *_args: stopping.__setitem__("value", True))
    started = time.monotonic()
    frames_applied = 0
    commands_sent = 0
    frames_skipped = 0
    replay_elapsed_s = 0.0
    try:
        status.write("starting", mode="replay", hardwareActive=False)
        starter = getattr(adapter, "start", None)
        if callable(starter):
            call_with_timeout(starter, watchdog_timeout_s, "start()")
        observations = call_with_timeout(
            adapter.observe, watchdog_timeout_s, "observe()"
        )
        if not isinstance(observations, dict):
            raise TypeError("Robot Adapter observe() 必须返回对象")
        observations = mapped_observations(observations, config.get("observation_map"))
        effective_action, constraints = resolve_action_constraints(
            config["action"], adapter, timeout_s=watchdog_timeout_s
        )
        baseline_key = str(effective_action["baseline_observation"])
        current = observations.get(baseline_key)
        if (
            not isinstance(current, list)
            or len(current) != width
            or not all(_finite_number(value) for value in current)
        ):
            raise ValueError("当前本体受控关节状态不是正确维度的有限数值数组")
        current = [float(value) for value in current]

        replay_check = dict(effective_action)
        replay_check["horizon"] = len(actions)
        replay_interval_scale = max(1.0, rate_hz / fps)
        replay_check["max_step"] = [
            float(step) * replay_interval_scale
            for step in effective_action["max_step"]
        ]
        actions = validate_action(actions, replay_check, actions[0])
        target_check = dict(effective_action)
        target_check["horizon"] = 1
        target_check["max_step"] = [
            max(float(step), float(high) - float(low))
            for step, low, high in zip(
                effective_action["max_step"],
                effective_action["minimum"],
                effective_action["maximum"],
            )
        ]
        start_target = validate_action([actions[0]], target_check, current)[0]
        max_step = [float(value) for value in effective_action["max_step"]]
        transition_steps = max(
            1,
            math.ceil(move_duration_s * rate_hz),
            max(
                math.ceil(abs(target - actual) / step)
                for target, actual, step in zip(start_target, current, max_step)
            ),
        )
        transition_check = dict(effective_action)
        transition_check["horizon"] = transition_steps
        transition_rows = [
            [
                actual + (target - actual) * index / transition_steps
                for actual, target in zip(current, start_target)
            ]
            for index in range(1, transition_steps + 1)
        ]
        transition_rows = validate_action(
            transition_rows, transition_check, current
        )
        transition_started = time.monotonic()
        transition_index = 0
        status.write(
            "moving_to_start",
            mode="replay",
            hardwareActive=True,
            totalFrames=len(actions),
            framesApplied=0,
            actionConstraints=constraints,
        )
        while transition_index < transition_steps and not stopping["value"]:
            scheduled_at = transition_started + transition_index / rate_hz
            remaining = scheduled_at - time.monotonic()
            if remaining > 0:
                time.sleep(remaining)
            if stopping["value"]:
                break
            due_index = min(
                transition_steps - 1,
                max(
                    transition_index,
                    int((time.monotonic() - transition_started) * rate_hz),
                ),
            )
            call_with_timeout(
                adapter.apply_action,
                watchdog_timeout_s,
                "apply_action()",
                transition_rows[due_index],
            )
            transition_index = due_index + 1
        transition_remaining = (
            transition_started + transition_steps / rate_hz - time.monotonic()
        )
        if transition_remaining > 0 and not stopping["value"]:

            time.sleep(transition_remaining)
        if not stopping["value"]:
            status.write(
                "replaying",
                mode="replay",
                hardwareActive=True,
                totalFrames=len(actions),
                framesApplied=0,
                fps=fps,
                startFrame=int(replay.get("start_frame", 0)),
                actionConstraints=constraints,
            )
            replay_started = time.monotonic()
            replay_index = 0
            last_reported = 0
            report_every = max(1, round(fps / 5))
            while replay_index < len(actions) and not stopping["value"]:
                scheduled_at = replay_started + replay_index / fps
                remaining = scheduled_at - time.monotonic()
                if remaining > 0:
                    time.sleep(remaining)
                if stopping["value"]:
                    break
                due_index = min(
                    len(actions) - 1,
                    max(
                        replay_index,
                        int((time.monotonic() - replay_started) * fps),
                    ),
                )
                frames_skipped += due_index - replay_index
                call_with_timeout(
                    adapter.apply_action,
                    watchdog_timeout_s,
                    "apply_action()",
                    actions[due_index],
                )
                commands_sent += 1
                frames_applied = due_index + 1
                replay_index = due_index + 1
                if (
                    frames_applied == 1
                    or frames_applied - last_reported >= report_every
                    or frames_applied == len(actions)
                ):
                    last_reported = frames_applied
                    status.write(
                        "replaying",
                        mode="replay",
                        hardwareActive=True,
                        totalFrames=len(actions),
                        framesApplied=frames_applied,
                        commandsSent=commands_sent,
                        framesSkipped=frames_skipped,
                        fps=fps,
                        startFrame=int(replay.get("start_frame", 0)),
                    )
            replay_remaining = (
                replay_started + len(actions) / fps - time.monotonic()
            )
            if replay_remaining > 0 and not stopping["value"]:
                time.sleep(replay_remaining)
            replay_elapsed_s = time.monotonic() - replay_started
        final_status = "stopped" if stopping["value"] else "finished"
        result = {
            "status": final_status,
            "framesApplied": frames_applied,
            "totalFrames": len(actions),
            "fps": fps,
            "durationS": time.monotonic() - started,
            "commandsSent": commands_sent,
            "framesSkipped": frames_skipped,
            "timingDegraded": frames_skipped > 0,
            "effectiveCommandHz": (
                commands_sent / replay_elapsed_s
                if replay_elapsed_s > 0
                else 0
            ),
            "replayDurationS": replay_elapsed_s,
        }
        status.write(final_status, mode="replay", hardwareActive=False, **{key: value for key, value in result.items() if key != "status"})
        return result
    except Exception as error:
        status.write(
            "fault",
            mode="replay",
            hardwareActive=False,
            framesApplied=frames_applied,
            totalFrames=len(actions),
            error=str(error),
        )
        raise
    finally:
        stopper = getattr(adapter, "stop", None)
        if callable(stopper):
            try:
                call_with_timeout(stopper, watchdog_timeout_s, "stop()")
            except Exception:
                pass


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
    control_config = config.get("control", {})
    control_config = control_config if isinstance(control_config, dict) else {}
    watchdog_timeout_s = float(control_config.get("watchdog_timeout_s", 5.0))
    pose_return = control_config.get("pose_return", {})
    pose_return = pose_return if isinstance(pose_return, dict) else {}
    rate_hz = float(control_config.get("rate_hz", 10))
    if not math.isfinite(rate_hz) or rate_hz <= 0:
        raise ValueError("control.rate_hz 必须大于 0")
    final_hold_s = float(pose_return.get("final_hold_s", max(0.1, 2.0 / rate_hz)))
    settle_timeout_s = float(pose_return.get("settle_timeout_s", 5.0))
    sample_interval_s = float(pose_return.get("sample_interval_s", max(0.05, 1.0 / rate_hz)))
    stable_samples = pose_return.get("stable_samples", 2)
    if not math.isfinite(final_hold_s) or not 0 <= final_hold_s <= 5:
        raise ValueError("control.pose_return.final_hold_s 必须在 0 到 5 秒之间")
    if not math.isfinite(settle_timeout_s) or not 0 < settle_timeout_s <= 60:
        raise ValueError("control.pose_return.settle_timeout_s 必须在 0 到 60 秒之间")
    if not math.isfinite(sample_interval_s) or not 0 < sample_interval_s <= 2:
        raise ValueError("control.pose_return.sample_interval_s 必须在 0 到 2 秒之间")
    if isinstance(stable_samples, bool) or not isinstance(stable_samples, int) or not 1 <= stable_samples <= 20:
        raise ValueError("control.pose_return.stable_samples 必须在 1 到 20 之间")
    tolerance_value = pose_return.get("tolerance")
    if tolerance_value is None:
        tolerances = None
    elif _finite_number(tolerance_value):
        tolerances = [float(tolerance_value)] * width
    elif isinstance(tolerance_value, list) and len(tolerance_value) == width and all(
        _finite_number(item) for item in tolerance_value
    ):
        tolerances = [float(item) for item in tolerance_value]
    else:
        raise ValueError(f"control.pose_return.tolerance 必须是正数或 {width} 维正数数组")
    if tolerances is not None and any(value <= 0 for value in tolerances):
        raise ValueError("control.pose_return.tolerance 必须逐维大于 0")
    starter = getattr(adapter, "start", None)
    stopper = getattr(adapter, "stop", None)
    move_started = time.monotonic()
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

        # Some adapters (including filtered vendor controllers) send from a
        # background thread. Keep the final setpoint alive long enough to be
        # transmitted repeatedly before reading feedback or stopping.
        call_with_timeout(adapter.apply_action, watchdog_timeout_s, "apply_action()", normalized_target)
        if final_hold_s > 0:
            time.sleep(final_hold_s)

        def measured_state() -> list[float]:
            feedback = call_with_timeout(adapter.observe, watchdog_timeout_s, "observe()")
            if not isinstance(feedback, dict):
                raise TypeError("Robot Adapter observe() 必须返回对象")
            feedback = mapped_observations(feedback, config.get("observation_map"))
            values = feedback.get(baseline_key)
            if not isinstance(values, list) or len(values) != width or not all(
                _finite_number(item) for item in values
            ):
                raise ValueError("回位后的模型关节状态不是正确维度的有限数值数组")
            return [float(item) for item in values]

        observed = measured_state()
        errors = [abs(actual - expected) for actual, expected in zip(observed, normalized_target)]
        verified = False
        if tolerances is not None:
            stable = 1 if all(error <= tolerance for error, tolerance in zip(errors, tolerances)) else 0
            settle_deadline = time.monotonic() + settle_timeout_s
            while stable < stable_samples:
                remaining = settle_deadline - time.monotonic()
                if remaining <= 0:
                    worst_index = max(range(width), key=errors.__getitem__)
                    names = ((config.get("telemetry") or {}).get("action") or {}).get("names") or []
                    label = names[worst_index] if worst_index < len(names) else f"joint_{worst_index}"
                    raise RuntimeError(
                        f"回位未达到配置精度：{label} 误差 {errors[worst_index]:.6g}，"
                        f"容差 {tolerances[worst_index]:.6g}"
                    )
                time.sleep(min(sample_interval_s, remaining))
                observed = measured_state()
                errors = [abs(actual - expected) for actual, expected in zip(observed, normalized_target)]
                if all(error <= tolerance for error, tolerance in zip(errors, tolerances)):
                    stable += 1
                else:
                    stable = 0
            verified = True
        worst_index = max(range(width), key=errors.__getitem__)
        return {
            "values": normalized_target,
            "observedValues": observed,
            "errors": errors,
            "maxError": errors[worst_index],
            "worstIndex": worst_index,
            "verified": verified,
            "steps": required_steps,
            "duration_s": required_steps / rate_hz,
            "totalDurationS": time.monotonic() - move_started,
        }
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
    parser.add_argument("--replay-actions")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if args.move_pose:
        pose = json.loads(Path(args.move_pose).read_text(encoding="utf-8"))
        print(json.dumps(move_to_pose(config, pose), ensure_ascii=False))
    elif args.replay_actions:
        replay = json.loads(Path(args.replay_actions).read_text(encoding="utf-8"))
        print(json.dumps(replay_actions(config, replay), ensure_ascii=False))
    else:
        run(config)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
