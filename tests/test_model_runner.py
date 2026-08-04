import importlib.util
import base64
import json
import sys
import threading
import urllib.request
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RUNNER_PATH = PROJECT_ROOT / "backend" / "deploy" / "assets" / "model_runner.py"
SPEC = importlib.util.spec_from_file_location("embodit_model_runner", RUNNER_PATH)
assert SPEC and SPEC.loader
model_runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(model_runner)


def _write_model(tmp_path: Path) -> None:
    (tmp_path / "test_user_model.py").write_text(
        """
class TestModel:
    def load(self, checkpoint, scale=1):
        self.checkpoint = checkpoint
        self.scale = scale

    def predict(self, observations, bias=0):
        return [[(value + bias) * self.scale for value in observations['joints']]]

class TensorLike:
    def __init__(self, value): self.value = value
    def detach(self): return self
    def cpu(self): return self
    def tolist(self): return self.value

def create_model(checkpoint, scale=1):
    model = TestModel()
    model.load(checkpoint, scale=scale)
    return model
""".strip()
        + "\n",
        encoding="utf-8",
    )


def test_model_provider_loads_user_class_and_normalizes_action(tmp_path: Path) -> None:
    _write_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        provider = model_runner.ModelProvider(
            {
                "entrypoint": "test_user_model:TestModel",
                "checkpoint": "/models/example.ckpt",
                "load_kwargs": {"scale": 2},
                "predict_kwargs": {"bias": 1},
            }
        )
        provider.load()
        assert provider.ready is True
        assert provider.predict({"joints": [1, 2]}) == {"action": {"values": [[4, 6]]}}
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("test_user_model", None)


def test_model_provider_accepts_factory_entrypoint(tmp_path: Path) -> None:
    _write_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    try:
        provider = model_runner.ModelProvider(
            {
                "entrypoint": "test_user_model:create_model",
                "checkpoint": "/models/example.ckpt",
                "load_kwargs": {"scale": 3},
            }
        )
        provider.load()
        assert provider.predict({"joints": [2]})["action"]["values"] == [[6]]
    finally:
        sys.path.remove(str(tmp_path))
        sys.modules.pop("test_user_model", None)


def test_transport_image_is_decoded_before_user_prediction() -> None:
    raw = b"jpeg-bytes"
    value = model_runner.decode_observation(
        {
            "wrist": {
                "encoding": "jpeg",
                "$binary": base64.b64encode(raw).decode("ascii"),
            },
            "joints": [0.1, 0.2],
        }
    )
    assert value["wrist"] == {"encoding": "jpeg", "data": raw}
    assert value["joints"] == [0.1, 0.2]


def test_managed_http_protocol_is_internal_and_operational(tmp_path: Path) -> None:
    _write_model(tmp_path)
    sys.path.insert(0, str(tmp_path))
    server = None
    try:
        provider = model_runner.ModelProvider(
            {"entrypoint": "test_user_model:TestModel", "checkpoint": "/models/example.ckpt"}
        )
        provider.load()
        try:
            server = model_runner.ModelHttpServer(("127.0.0.1", 0), provider, 100_000)
        except PermissionError:
            pytest.skip("sandbox does not permit local TCP sockets")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{server.server_address[1]}"

        with urllib.request.urlopen(base + "/health", timeout=2) as response:
            assert json.loads(response.read())["ready"] is True

        body = json.dumps({"observations": {"joints": [0.1, 0.2]}}).encode()
        request = urllib.request.Request(
            base + "/infer",
            data=body,
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=2) as response:
            assert json.loads(response.read())["action"]["values"] == [[0.1, 0.2]]
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        sys.path.remove(str(tmp_path))
        sys.modules.pop("test_user_model", None)
