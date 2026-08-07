import base64
import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from deploy.assets.python_robot_client import (
    expand_synthetic,
    hold_action_chunk,
    model_io_snapshot,
    move_to_pose,
    resolve_action_scheduler,
    resolve_adapter,
    start_async_inference,
    StatusWriter,
    transport_safe,
    validate_action,
)


ASSET_PATH = Path(__file__).resolve().parents[1] / "backend" / "deploy" / "assets" / "python_robot_client.py"


def action_config():
    return {
        "width": 3,
        "horizon": 2,
        "baseline_observation": "state",
        "minimum": [-2.0, -2.0, 0.0],
        "maximum": [2.0, 2.0, 100.0],
        "max_step": [0.2, 0.2, 20.0],
    }


def test_synthetic_observations_expand_without_robot_dependencies() -> None:
    image = expand_synthetic({"$synthetic": "image", "width": 4, "height": 3, "channels": 3})
    vector = expand_synthetic({"$synthetic": "vector", "length": 3, "value": 1.5})
    assert image["encoding"] == "rgb8"
    assert len(base64.b64decode(image["$binary"])) == 4 * 3 * 3
    assert vector == [1.5, 1.5, 1.5]


def test_transport_safe_promotes_binary_data_to_protocol_field() -> None:
    value = transport_safe({"encoding": "jpeg", "data": b"jpeg-bytes"})
    assert value["encoding"] == "jpeg"
    assert base64.b64decode(value["$binary"]) == b"jpeg-bytes"
    assert "data" not in value


def test_resolve_adapter_prioritizes_configured_vendor_module_paths(tmp_path) -> None:
    shadow = tmp_path / "shadow"
    vendor = tmp_path / "vendor"
    source = tmp_path / "source"
    (shadow / "core").mkdir(parents=True)
    (vendor / "core" / "astribot_api").mkdir(parents=True)
    source.mkdir()
    (shadow / "core" / "__init__.py").write_text("", encoding="utf-8")
    (vendor / "core" / "__init__.py").write_text("", encoding="utf-8")
    (vendor / "core" / "astribot_api" / "__init__.py").write_text("VALUE = 'vendor'\n", encoding="utf-8")
    (source / "path_adapter.py").write_text(
        "from core.astribot_api import VALUE\n"
        "class Adapter:\n"
        "    def __init__(self, config): self.value = VALUE\n"
        "    def observe(self): return {}\n"
        "    def apply_action(self, row): pass\n",
        encoding="utf-8",
    )
    original_path = list(sys.path)
    sys.path.insert(0, str(shadow))
    try:
        adapter = resolve_adapter(
            {
                "entrypoint": "path_adapter:Adapter",
                "source_path": str(source),
                "module_search_paths": [str(vendor)],
            }
        )
        assert adapter.value == "vendor"
    finally:
        sys.path[:] = original_path
        for name in ("path_adapter", "core.astribot_api", "core"):
            sys.modules.pop(name, None)


def test_model_io_snapshot_contains_only_model_inputs_and_outputs() -> None:
    observations = {
        "camera": expand_synthetic({"$synthetic": "image", "width": 4, "height": 3, "channels": 3}),
        "state": [0.1, -0.2, 30.0],
        "prompt": "pick object",
        "not_exposed": {"temperature": 42},
    }
    config = {
        "action": action_config(),
        "telemetry": {
            "cameras": [{"key": "camera", "label": "Main camera"}],
            "state": {"key": "state", "names": ["j1", "j2", "gripper"], "units": ["rad", "rad", "%"]},
            "action": {"names": ["j1", "j2", "gripper"], "units": ["rad", "rad", "%"]},
        },
    }
    value = model_io_snapshot(observations, [[0.1, 0.0, 10.0], [0.2, 0.1, 20.0]], 12.5, config)
    assert value["input"]["cameras"][0]["dataUrl"].startswith("data:image/bmp;base64,")
    assert value["input"]["state"]["names"] == ["j1", "j2", "gripper"]
    assert value["input"]["state"]["values"] == [0.1, -0.2, 30.0]
    assert value["input"]["prompt"] == "pick object"
    assert "not_exposed" not in json.dumps(value)
    assert value["output"]["action"]["chunk"] == [[0.1, 0.0, 10.0], [0.2, 0.1, 20.0]]
    assert value["output"]["inferenceLatencyMs"] == 12.5


def test_fault_status_keeps_the_last_model_io_for_diagnosis(tmp_path) -> None:
    path = tmp_path / "status.json"
    writer = StatusWriter(str(path))
    model_io = {"input": {"state": {"values": [0.0]}}, "output": {"action": {"chunk": [[9.0]]}}}
    writer.remember_model_io(model_io)
    writer.write("fault", error="action exceeds max_step")
    status = json.loads(path.read_text(encoding="utf-8"))
    assert status["modelIo"] == model_io


def test_generic_action_validation_enforces_shape_limits_and_steps() -> None:
    config = action_config()
    result = validate_action([[0.1, 0.0, 10], [0.2, 0.1, 20]], config, [0.0, 0.0, 0.0])
    assert result == [[0.1, 0.0, 10.0], [0.2, 0.1, 20.0]]

    with pytest.raises(ValueError, match="horizon"):
        validate_action([[0.1, 0.0, 10]], config, [0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="max_step"):
        validate_action([[0.3, 0.0, 10], [0.3, 0.0, 10]], config, [0.0, 0.0, 0.0])
    with pytest.raises(ValueError, match="绝对限位"):
        validate_action([[0.1, 0.0, 101], [0.1, 0.0, 100]], config, [0.0, 0.0, 100.0])


def test_initial_target_limit_is_distinct_from_following_action_steps() -> None:
    config = action_config()
    config["initial_max_step"] = [1.0, 1.0, 100.0]
    result = validate_action([[0.8, 0.0, 10], [0.9, 0.0, 20]], config, [0.0, 0.0, 0.0])
    assert result[0][0] == 0.8
    with pytest.raises(ValueError, match="max_step"):
        validate_action([[0.8, 0.0, 10], [1.1, 0.0, 20]], config, [0.0, 0.0, 0.0])


def test_hold_chunk_accepts_measured_state_outside_model_output_envelope() -> None:
    config = action_config()
    assert hold_action_chunk(config, [2.5, 0.0, -0.1]) == [
        [2.5, 0.0, -0.1],
        [2.5, 0.0, -0.1],
    ]


def test_action_validation_clamps_only_numerical_noise_at_a_limit() -> None:
    config = action_config()
    lower_result = validate_action(
        [[0.0, 0.0, -1e-8], [0.0, 0.0, 0.0]],
        config,
        [0.0, 0.0, 0.0],
    )
    upper_result = validate_action(
        [[0.0, 0.0, 100.00000001], [0.0, 0.0, 100.0]],
        config,
        [0.0, 0.0, 100.0],
    )
    assert lower_result == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    assert upper_result == [[0.0, 0.0, 100.0], [0.0, 0.0, 100.0]]

    with pytest.raises(ValueError, match="绝对限位"):
        validate_action([[0.0, 0.0, -1e-4], [0.0, 0.0, 0.0]], config, [0.0, 0.0, 0.0])


def test_action_validation_supports_per_dimension_tolerance() -> None:
    config = action_config()
    config["numerical_tolerance"] = [1e-6, 1e-6, 0.5]
    lower_result = validate_action(
        [[0.0, 0.0, -0.24], [0.0, 0.0, 0.0]],
        config,
        [0.0, 0.0, 0.0],
    )
    upper_result = validate_action(
        [[0.0, 0.0, 100.2], [0.0, 0.0, 100.0]],
        config,
        [0.0, 0.0, 100.0],
    )
    assert lower_result == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
    assert upper_result == [[0.0, 0.0, 100.0], [0.0, 0.0, 100.0]]

    config["numerical_tolerance"] = [1e-6, 1e-6, 0.1]
    with pytest.raises(ValueError, match="绝对限位"):
        validate_action([[0.0, 0.0, -0.24], [0.0, 0.0, 0.0]], config, [0.0, 0.0, 0.0])


def test_recorded_pose_uses_generic_adapter_and_respects_step_limits(tmp_path) -> None:
    module_path = tmp_path / "pose_adapter.py"
    module_path.write_text(
        "APPLIED = []\n"
        "class Adapter:\n"
        "    def __init__(self, config): self.config = config\n"
        "    def start(self): pass\n"
        "    def observe(self): return {'state': [0.0, 0.0, 0.0]}\n"
        "    def apply_action(self, row): APPLIED.append(list(row))\n"
        "    def stop(self): pass\n",
        encoding="utf-8",
    )
    config = {
        "adapter": {
            "entrypoint": "pose_adapter:Adapter",
            "source_path": str(tmp_path),
        },
        "action": action_config(),
        "control": {"rate_hz": 20, "watchdog_timeout_s": 1},
    }
    config["action"]["numerical_tolerance"] = [1e-6, 1e-6, 0.5]
    result = move_to_pose(config, {"values": [0.5, -0.3, -0.2], "duration_s": 0.1})
    module = sys.modules["pose_adapter"]
    assert result["values"] == [0.5, -0.3, 0.0]
    assert len(module.APPLIED) >= 3
    assert module.APPLIED[-1] == [0.5, -0.3, 0.0]
    assert all(
        abs(current[index] - previous[index]) <= config["action"]["max_step"][index] + 1e-9
        for previous, current in zip([[0.0, 0.0, 0.0], *module.APPLIED], module.APPLIED)
        for index in range(3)
    )


def test_action_scheduler_supports_sync_and_configurable_async_prefetch() -> None:
    assert resolve_action_scheduler({}, 50) == {
        "mode": "synchronous",
        "outputSteps": 50,
        "actionSteps": 50,
        "requestAfterSteps": None,
        "prefetchPolicy": None,
        "latencyMarginMs": None,
    }
    assert resolve_action_scheduler(
        {
            "inference_mode": "asynchronous",
            "action_steps": 50,
            "asynchronous": {"request_after_steps": 30},
        },
        50,
    ) == {
        "mode": "asynchronous",
        "outputSteps": 50,
        "actionSteps": 50,
        "requestAfterSteps": 30,
        "prefetchPolicy": "fixed",
        "latencyMarginMs": 30.0,
    }
    assert resolve_action_scheduler(
        {
            "inference_mode": "asynchronous",
            "action_steps": 50,
            "asynchronous": {"request_after_steps": "auto", "latency_margin_ms": 30},
        },
        50,
        inference_latency_ms=160,
        rate_hz=50,
    )["requestAfterSteps"] == 40
    with pytest.raises(ValueError, match="action_steps-1"):
        resolve_action_scheduler(
            {
                "inference_mode": "asynchronous",
                "action_steps": 50,
                "asynchronous": {"request_after_steps": 50},
            },
            50,
        )


class ModelHandler(BaseHTTPRequestHandler):
    actions = [[0.1, 0.0, 10.0], [0.2, 0.1, 20.0]]

    def log_message(self, *_args):
        pass

    def do_POST(self):  # noqa: N802
        length = int(self.headers["Content-Length"])
        request = json.loads(self.rfile.read(length))
        assert "observations" in request
        payload = json.dumps({"action": {"values": self.actions}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class PrefetchModelHandler(ModelHandler):
    actions = [
        [0.02, 0.0, 1.0],
        [0.04, 0.0, 2.0],
        [0.06, 0.0, 3.0],
        [0.08, 0.0, 4.0],
        [0.10, 0.0, 5.0],
    ]
    request_times = []

    def do_POST(self):  # noqa: N802
        type(self).request_times.append(time.monotonic())
        time.sleep(0.03)
        super().do_POST()


class SafetyRejectingModelHandler(ModelHandler):
    requests = 0

    def do_POST(self):  # noqa: N802
        type(self).requests += 1
        self.actions = (
            ModelHandler.actions
            if type(self).requests == 1
            else [[0.1, 0.0, 101.0], [0.1, 0.0, 100.0]]
        )
        super().do_POST()


def runtime_config(tmp_path, endpoint):
    return {
        "status_path": str(tmp_path / "status.json"),
        "default_prompt": "test task",
        "adapter": {"entrypoint": "missing_vendor_module:Adapter"},
        "dry_run_observations": {
            "state": {"$synthetic": "vector", "length": 3},
            "camera": {"$synthetic": "image", "width": 4, "height": 3},
        },
        "action": action_config() | {"baseline_observation": "state"},
        "control": {"rate_hz": 100, "max_episode_steps": 2, "watchdog_timeout_s": 1},
        "model": {"endpoint": endpoint, "timeout_s": 5},
    }


def wait_status(path, expected, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            value = json.loads(path.read_text(encoding="utf-8"))
            if value.get("status") == expected:
                return value
            if value.get("status") == "fault":
                raise AssertionError(value)
        time.sleep(0.02)
    raise TimeoutError(f"status did not become {expected}")


@pytest.fixture
def model_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), ModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def prefetch_model_server():
    PrefetchModelHandler.request_times = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), PrefetchModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


@pytest.fixture
def safety_rejecting_model_server():
    SafetyRejectingModelHandler.requests = 0
    server = ThreadingHTTPServer(("127.0.0.1", 0), SafetyRejectingModelHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()


def test_dry_run_completes_inference_without_importing_vendor_adapter(tmp_path, model_server) -> None:
    config = runtime_config(tmp_path, model_server)
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "dry_run"}
    process = subprocess.Popen([sys.executable, str(ASSET_PATH), "--config", str(config_path)], env=environment)
    try:
        status = wait_status(tmp_path / "status.json", "ready")
        assert status["hardwareActive"] is False
        assert status["actionShape"] == [2, 3]
        assert status["modelIo"]["input"]["state"]["values"] == [0.0, 0.0, 0.0]
        assert status["modelIo"]["output"]["action"]["chunk"] == ModelHandler.actions
        assert process.poll() is None
    finally:
        process.terminate()
        assert process.wait(timeout=5) == 0


def test_adapter_dry_run_streams_real_observations_without_applying_actions(tmp_path, model_server) -> None:
    events_path = tmp_path / "events.jsonl"
    module_path = tmp_path / "observing_adapter.py"
    module_path.write_text(
        """
import json
class Adapter:
    def __init__(self, config): self.path = config['path']; self.samples = 0
    def record(self, value):
        with open(self.path, 'a') as handle: handle.write(json.dumps(value) + '\\n')
    def start_observation(self): self.record('start_observation')
    def observe(self):
        self.samples += 1
        self.record({'observe': self.samples})
        return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row): self.record({'apply_action': row})
    def stop_observation(self): self.record('stop_observation')
    def start(self): self.record('start')
    def stop(self): self.record('stop')
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, model_server)
    config["dry_run_observation_source"] = "adapter"
    config.pop("dry_run_observations")
    config["adapter"] = {
        "entrypoint": "observing_adapter:Adapter",
        "source_path": str(tmp_path),
        "config": {"path": str(events_path)},
    }
    config["control"]["dry_run_rate_hz"] = 100
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "dry_run"}
    process = subprocess.Popen([sys.executable, str(ASSET_PATH), "--config", str(config_path)], env=environment)
    try:
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            if (tmp_path / "status.json").exists():
                status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
                if status.get("samples", 0) >= 2:
                    break
            time.sleep(0.02)
        assert status is not None and status.get("samples", 0) >= 2
        assert status["hardwareActive"] is False
    finally:
        process.terminate()
        assert process.wait(timeout=5) == 0

    events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines()]
    assert events[0] == "start_observation"
    assert sum(isinstance(event, dict) and "observe" in event for event in events) >= 2
    assert not any(isinstance(event, dict) and "apply_action" in event for event in events)
    assert "start" not in events
    assert "stop" not in events
    assert events[-1] == "stop_observation"


def test_adapter_dry_run_keeps_streaming_after_action_safety_rejection(
    tmp_path, safety_rejecting_model_server
) -> None:
    module_path = tmp_path / "safety_observer.py"
    module_path.write_text(
        """
class Adapter:
    def __init__(self, config): pass
    def start_observation(self): pass
    def observe(self): return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row): raise AssertionError('Dry Run applied an action')
    def stop_observation(self): pass
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, safety_rejecting_model_server)
    config["dry_run_observation_source"] = "adapter"
    config.pop("dry_run_observations")
    config["adapter"] = {
        "entrypoint": "safety_observer:Adapter",
        "source_path": str(tmp_path),
    }
    config["control"]["dry_run_rate_hz"] = 100
    config_path = tmp_path / "safety-client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "dry_run"}
    process = subprocess.Popen([sys.executable, str(ASSET_PATH), "--config", str(config_path)], env=environment)
    try:
        deadline = time.monotonic() + 5
        status = None
        while time.monotonic() < deadline:
            if (tmp_path / "status.json").exists():
                status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
                if status.get("samples", 0) >= 2 and status.get("safetyPassed") is False:
                    break
            time.sleep(0.02)
        assert status is not None
        assert status["status"] == "ready"
        assert status["safetyPassed"] is False
        assert status["safetyRejections"] >= 1
        assert "101" in status["safetyError"]
        assert process.poll() is None
    finally:
        process.terminate()
        assert process.wait(timeout=5) == 0


def test_live_runtime_uses_only_the_generic_adapter_contract(tmp_path, model_server) -> None:
    applied_path = tmp_path / "applied.jsonl"
    module_path = tmp_path / "fake_adapter.py"
    module_path.write_text(
        """
import json
class Adapter:
    def __init__(self, config): self.path = config['path']
    def start(self): pass
    def observe(self): return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row):
        with open(self.path, 'a') as handle: handle.write(json.dumps(row) + '\\n')
    def stop(self): pass
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, model_server)
    config["adapter"] = {
        "entrypoint": "fake_adapter:Adapter",
        "source_path": str(tmp_path),
        "config": {"path": str(applied_path)},
    }
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "live"}
    completed = subprocess.run(
        [sys.executable, str(ASSET_PATH), "--config", str(config_path)],
        env=environment,
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    status = wait_status(tmp_path / "status.json", "finished")
    assert status["steps"] == 2
    rows = [json.loads(line) for line in applied_path.read_text(encoding="utf-8").splitlines()]
    assert rows == ModelHandler.actions


def test_live_runtime_holds_and_continues_after_unsafe_model_chunk(
    tmp_path, safety_rejecting_model_server
) -> None:
    applied_path = tmp_path / "safe-applied.jsonl"
    module_path = tmp_path / "safe_adapter.py"
    module_path.write_text(
        "import json\n"
        "class Adapter:\n"
        "    def __init__(self, config): self.path = config['path']\n"
        "    def start(self): pass\n"
        "    def observe(self): return {'state': [0.0, 0.0, 0.0]}\n"
        "    def apply_action(self, row):\n"
        "        with open(self.path, 'a') as handle: handle.write(json.dumps(row) + '\\n')\n"
        "    def stop(self): pass\n",
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, safety_rejecting_model_server)
    config["control"]["max_episode_steps"] = 4
    config["adapter"] = {
        "entrypoint": "safe_adapter:Adapter",
        "source_path": str(tmp_path),
        "config": {"path": str(applied_path)},
    }
    config_path = tmp_path / "safe-client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    completed = subprocess.run(
        [sys.executable, str(ASSET_PATH), "--config", str(config_path)],
        env={**os.environ, "EMBODIT_DEPLOYMENT_MODE": "live"},
        timeout=10,
        check=False,
    )
    assert completed.returncode == 0
    status = wait_status(tmp_path / "status.json", "finished")
    assert status["steps"] == 4
    assert status["safetyRejections"] >= 1
    assert "101" in status["safetyError"]
    rows = [json.loads(line) for line in applied_path.read_text(encoding="utf-8").splitlines()]
    assert rows[:2] == ModelHandler.actions
    assert rows[2:] == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]


def test_live_runtime_without_maximum_steps_runs_until_explicitly_stopped(tmp_path, model_server) -> None:
    applied_path = tmp_path / "unbounded-applied.jsonl"
    module_path = tmp_path / "unbounded_adapter.py"
    module_path.write_text(
        """
import json
class Adapter:
    def __init__(self, config): self.path = config['path']
    def start(self): pass
    def observe(self): return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row):
        with open(self.path, 'a') as handle: handle.write(json.dumps(row) + '\\n')
    def stop(self): pass
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, model_server)
    config["control"].pop("max_episode_steps")
    config["adapter"] = {
        "entrypoint": "unbounded_adapter:Adapter",
        "source_path": str(tmp_path),
        "config": {"path": str(applied_path)},
    }
    config_path = tmp_path / "unbounded-client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "live"}
    process = subprocess.Popen([sys.executable, str(ASSET_PATH), "--config", str(config_path)], env=environment)
    try:
        wait_status(tmp_path / "status.json", "ready")
        deadline = time.monotonic() + 5
        rows = []
        while time.monotonic() < deadline:
            if applied_path.exists():
                rows = applied_path.read_text(encoding="utf-8").splitlines()
                if len(rows) >= 6:
                    break
            time.sleep(0.02)
        assert len(rows) >= 6
        assert process.poll() is None
    finally:
        process.terminate()
        assert process.wait(timeout=5) == 0


def test_live_async_runtime_prefetches_before_current_chunk_finishes(
    tmp_path, prefetch_model_server
) -> None:
    applied_path = tmp_path / "async-applied.jsonl"
    module_path = tmp_path / "async_adapter.py"
    module_path.write_text(
        """
import json
import time
class Adapter:
    def __init__(self, config): self.path = config['path']
    def start(self): pass
    def observe(self): return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row):
        with open(self.path, 'a') as handle:
            handle.write(json.dumps({'time': time.monotonic(), 'row': row}) + '\\n')
    def stop(self): pass
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, prefetch_model_server)
    config["action"]["horizon"] = 5
    config["control"].update(
        {
            "rate_hz": 50,
            "max_episode_steps": 10,
            "inference_mode": "asynchronous",
            "action_steps": 5,
            "asynchronous": {"request_after_steps": 3},
        }
    )
    config["adapter"] = {
        "entrypoint": "async_adapter:Adapter",
        "source_path": str(tmp_path),
        "config": {"path": str(applied_path)},
    }
    config_path = tmp_path / "async-client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "live"}
    completed = subprocess.run(
        [sys.executable, str(ASSET_PATH), "--config", str(config_path)],
        env=environment,
        timeout=10,
        check=False,
    )

    assert completed.returncode == 0
    applied = [json.loads(line) for line in applied_path.read_text(encoding="utf-8").splitlines()]
    assert len(applied) == 10
    assert len(PrefetchModelHandler.request_times) >= 2
    # The second request starts after action 3 and before action 4, while actions
    # 4-5 continue to drain from the current chunk.
    assert applied[2]["time"] <= PrefetchModelHandler.request_times[1] < applied[3]["time"]
    status = wait_status(tmp_path / "status.json", "finished")
    assert status["steps"] == 10


def test_async_inference_keeps_the_observation_used_for_its_action_chunk() -> None:
    class Model:
        last_metrics = {}

        def infer(self, observations):
            return [[0.1, 0.0, 1.0], [0.2, 0.0, 2.0]], 5.0

    observations = {"state": [0.1, 0.2, 0.3]}
    done, result = start_async_inference(
        Model(), observations, {"action": action_config(), "telemetry": {}}, 2.5
    )
    assert done.wait(2)
    assert result["observations"] is observations
    assert result["observationLatencyMs"] == 2.5


def test_live_runtime_faults_when_adapter_call_exceeds_watchdog(tmp_path, model_server) -> None:
    module_path = tmp_path / "slow_adapter.py"
    module_path.write_text(
        """
import time
class Adapter:
    def __init__(self, config): pass
    def observe(self): return {'state': [0.0, 0.0, 0.0]}
    def apply_action(self, row): time.sleep(2)
    def stop(self): pass
""".strip(),
        encoding="utf-8",
    )
    config = runtime_config(tmp_path, model_server)
    config["adapter"] = {"entrypoint": "slow_adapter:Adapter", "source_path": str(tmp_path)}
    config["control"]["watchdog_timeout_s"] = 0.1
    config_path = tmp_path / "client.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    environment = {**os.environ, "EMBODIT_DEPLOYMENT_MODE": "live"}
    completed = subprocess.run(
        [sys.executable, str(ASSET_PATH), "--config", str(config_path)],
        env=environment,
        timeout=5,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode != 0
    status = json.loads((tmp_path / "status.json").read_text(encoding="utf-8"))
    assert status["status"] == "fault"
    assert "watchdog_timeout_s" in status["error"]
