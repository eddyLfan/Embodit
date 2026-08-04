#!/usr/bin/env python3
"""Embodit-managed model service runtime asset.

This file is uploaded to the inference host by the deployment orchestrator.
User model code stays transport-agnostic and only implements::

    class Model:
        def load(self, checkpoint: str, **kwargs): ...
        def predict(self, observations: dict, **kwargs): ...

The runner owns the private HTTP protocol used by the robot client.
"""

from __future__ import annotations

import argparse
import base64
import importlib
import inspect
import json
import math
import signal
import sys
import threading
import traceback
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


def resolve_entrypoint(value: str) -> Any:
    module_name, separator, attribute_path = value.partition(":")
    if not separator or not module_name or not attribute_path:
        raise ValueError("entrypoint 必须使用 module:ClassName 格式")
    target: Any = importlib.import_module(module_name)
    for part in attribute_path.split("."):
        target = getattr(target, part)
    return target


def jsonable(value: Any) -> Any:
    """Convert common tensor/array outputs without importing their libraries."""
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("模型输出包含 NaN 或 Inf")
        return value
    if isinstance(value, dict):
        return {str(key): jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [jsonable(item) for item in value]
    detached = value.detach() if callable(getattr(value, "detach", None)) else value
    cpu_value = detached.cpu() if callable(getattr(detached, "cpu", None)) else detached
    if callable(getattr(cpu_value, "tolist", None)):
        return jsonable(cpu_value.tolist())
    if hasattr(value, "item") and callable(value.item):
        return jsonable(value.item())
    raise TypeError(f"模型输出无法转换为 JSON：{type(value).__name__}")


def decode_observation(value: Any) -> Any:
    """Remove transport encoding before observations reach user model code."""
    if isinstance(value, list):
        return [decode_observation(item) for item in value]
    if not isinstance(value, dict):
        return value
    decoded = {key: decode_observation(item) for key, item in value.items() if key != "$binary"}
    if "$binary" in value:
        encoded = value["$binary"]
        if not isinstance(encoded, str):
            raise ValueError("观测 $binary 必须是 Base64 字符串")
        decoded["data"] = base64.b64decode(encoded, validate=True)
    return decoded


class ModelProvider:
    def __init__(self, config: dict[str, Any]):
        self.config = config
        self.model: Any = None
        self.ready = False
        self.error: str | None = None
        self.lock = threading.Lock()

    def load(self) -> None:
        target = resolve_entrypoint(self.config["entrypoint"])
        checkpoint = self.config["checkpoint"]
        kwargs = self.config.get("load_kwargs") or {}
        load_method = self.config.get("load_method", "load")

        if inspect.isclass(target):
            instance = target()
            loader = getattr(instance, load_method, None)
            if not callable(loader):
                raise TypeError(f"模型类必须实现 {load_method}(checkpoint, **kwargs)")
            loaded = loader(checkpoint, **kwargs)
            self.model = instance if loaded is None else loaded
        elif callable(target) and not callable(getattr(target, load_method, None)):
            # Factory form: create_model(checkpoint, **kwargs) -> model.
            self.model = target(checkpoint, **kwargs)
        else:
            loader = getattr(target, load_method, None)
            if not callable(loader):
                raise TypeError(f"模型实例必须实现 {load_method}(checkpoint, **kwargs)")
            loaded = loader(checkpoint, **kwargs)
            self.model = target if loaded is None else loaded

        predictor = getattr(self.model, self.config.get("predict_method", "predict"), None)
        if not callable(predictor) and not callable(self.model):
            raise TypeError("加载结果必须实现 predict(observations) 或自身可调用")
        self.ready = True

    def predict(self, observations: dict[str, Any]) -> dict[str, Any]:
        if not self.ready:
            raise RuntimeError(self.error or "模型尚未就绪")
        method_name = self.config.get("predict_method", "predict")
        predictor = getattr(self.model, method_name, None)
        if not callable(predictor):
            predictor = self.model
        with self.lock:
            output = predictor(observations, **(self.config.get("predict_kwargs") or {}))
        converted = jsonable(output)
        if isinstance(converted, dict) and "action" in converted:
            return converted
        if isinstance(converted, dict) and "values" in converted:
            return {"action": converted}
        return {"action": {"values": converted}}

    @property
    def specification(self) -> dict[str, Any] | None:
        value = getattr(self.model, "specification", None) if self.model is not None else None
        try:
            converted = jsonable(value) if value is not None else None
        except (TypeError, ValueError):
            return None
        return converted if isinstance(converted, dict) else None


class ModelHttpServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, address: tuple[str, int], provider: ModelProvider, maximum_request_bytes: int):
        super().__init__(address, ModelRequestHandler)
        self.provider = provider
        self.maximum_request_bytes = maximum_request_bytes


class ModelRequestHandler(BaseHTTPRequestHandler):
    server: ModelHttpServer

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        print(f"model-runner {self.address_string()} {format % args}", flush=True)

    def send_json(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/health":
            self.send_json(
                HTTPStatus.OK,
                {
                    "ready": self.server.provider.ready,
                    "error": self.server.provider.error,
                    "specification": self.server.provider.specification,
                },
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})

    def do_POST(self) -> None:  # noqa: N802
        if self.path != "/infer":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "not found"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > self.server.maximum_request_bytes:
                raise ValueError("推理请求为空或超过 maximum_request_bytes")
            request = json.loads(self.rfile.read(length))
            if not isinstance(request, dict) or not isinstance(request.get("observations"), dict):
                raise ValueError("请求必须包含 observations 对象")
            result = self.server.provider.predict(decode_observation(request["observations"]))
            self.send_json(HTTPStatus.OK, result)
        except (TypeError, ValueError) as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
        except Exception as error:  # noqa: BLE001
            traceback.print_exc()
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})


def main() -> int:
    parser = argparse.ArgumentParser(description="Embodit managed model runner")
    parser.add_argument("--config", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", required=True, type=int)
    args = parser.parse_args()

    config = json.loads(Path(args.config).read_text(encoding="utf-8"))
    if config.get("module_search_path"):
        sys.path.insert(0, config["module_search_path"])
    provider = ModelProvider(config)
    try:
        provider.load()
    except Exception as error:  # noqa: BLE001
        provider.error = str(error)
        traceback.print_exc()
        raise

    server = ModelHttpServer(
        (args.host, args.port),
        provider,
        int(config.get("maximum_request_bytes", 50_000_000)),
    )
    signal.signal(signal.SIGTERM, lambda *_args: threading.Thread(target=server.shutdown, daemon=True).start())
    signal.signal(signal.SIGINT, lambda *_args: threading.Thread(target=server.shutdown, daemon=True).start())
    print(f"Embodit model runner ready on http://{args.host}:{args.port}", flush=True)
    server.serve_forever(poll_interval=0.25)
    server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
