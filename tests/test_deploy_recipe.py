import json
import os
import time
from pathlib import Path

import pytest

from app import DeploymentComposeRequest, DeploymentConfigRequest, DeploymentRecipeRequest, build_app
from deploy.orchestrator import DeploymentOrchestration, OrchestrationState, RemoteServiceManager
from deploy.recipe import CommandSpec, compose_recipe, parse_model_config, parse_recipe, parse_robot_config, redact_recipe, split_recipe
from deploy.store import DeploymentConfigStore, RecipeStore
from deploy.transport import LocalCommandRunner, RecipeSshRunner, RemoteResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEPLOYMENT_CONFIG_DIR = PROJECT_ROOT / "config" / "deployment"
MODEL_CONFIG_DIR = DEPLOYMENT_CONFIG_DIR / "models"
RECIPE_PATH = DEPLOYMENT_CONFIG_DIR / "recipe.example.json"
ROBOT_CONFIG_PATH = DEPLOYMENT_CONFIG_DIR / "robot.example.json"
MODEL_CONFIG_PATH = MODEL_CONFIG_DIR / "python.example.json"
CHECKPOINT_MODEL_PATHS = {
    provider: MODEL_CONFIG_DIR / f"{provider}.example.json"
    for provider in ("openpi", "lerobot", "starvla")
}
PYTHON_ADAPTER_CLIENT_PATH = PROJECT_ROOT / "examples" / "deployment" / "python_robot_client.example.json"
PYTHON_ADAPTER_SOURCE_PATH = PROJECT_ROOT / "examples" / "deployment" / "python_robot_adapter.py"


def raw_recipe() -> dict:
    return json.loads(RECIPE_PATH.read_text(encoding="utf-8"))


def python_adapter_recipe() -> dict:
    raw = raw_recipe()
    raw["robot"]["client"] = {
        "host": "robot",
        "builtin": "python_adapter",
        "config": {
            "default_prompt": "pick up the object",
            "adapter": {
                "entrypoint": "vendor_adapter:RobotAdapter",
                "source_path": "/opt/robot_adapter",
                "python_executable": "/usr/bin/python3",
                "config": {"device": "robot0"},
            },
            "dry_run_observations": {
                "joint_position": {"$synthetic": "vector", "length": 6},
                "camera": {"$synthetic": "image", "width": 224, "height": 224, "channels": 3},
            },
            "telemetry": {
                "cameras": [{"key": "camera", "label": "Main camera"}],
                "state": {"key": "joint_position", "names": [f"joint_{index}" for index in range(6)]},
                "action": {"names": [f"joint_{index}" for index in range(6)]},
            },
            "action": {
                "width": 6,
                "horizon": 2,
                "baseline_observation": "joint_position",
                "minimum": [-2.8] * 6,
                "maximum": [2.8] * 6,
                "max_step": [0.2] * 6,
            },
            "control": {"rate_hz": 10, "max_episode_steps": 100},
        },
        "startup_timeout_s": 60,
        "restart": "no",
    }
    return raw


class FakeRunner:
    def run(self, args, **kwargs):
        return RemoteResult(0, "")


class CapturingManager:
    deployment_dir = "/root/.embodit/deployments/robot-a-vla"

    def __init__(self):
        self.files = {}
        self.started = None

    def write_file(self, path, payload, mode=0o700):
        self.files[path] = (payload, mode)

    def start(self, component, spec, environment=None):
        self.started = (component, spec, environment)
        return "embodit-client.service"


class SequenceStatusManager:
    def __init__(self, statuses=None):
        self.statuses = statuses or {}

    def status(self, component):
        sequence = self.statuses.get(
            component,
            [{"probeOk": True, "ActiveState": "active", "SubState": "running", "Result": "success"}],
        )
        if len(sequence) > 1:
            return sequence.pop(0)
        return sequence[0]


class HarnessOrchestration(DeploymentOrchestration):
    def __init__(self, *args, fail_at=None, **kwargs):
        self.calls = []
        self.fail_at = fail_at
        super().__init__(*args, **kwargs)

    def _called(self, name):
        self.calls.append(name)
        if self.fail_at == name:
            raise RuntimeError(f"failed at {name}")

    def _precheck(self): self._called("precheck")
    def _ensure_tunnel_credentials(self): self._called("tunnel_credentials")
    def _start_model(self): self._called("model")
    def _wait_model_health(self): self._called("model_health")
    def _start_tunnel(self): self._called("tunnel")
    def _wait_tunnel_health(self): self._called("tunnel_health")
    def _start_ros(self): self._called("ros")
    def _wait_ros_readiness(self): self._called("ros_readiness")
    def _run_operation(self, operation, label): self._called("power_on" if label == "上电" else label)
    def _move_initial_pose(self): self._called("initial_pose")
    def _start_client(self): self._called("client")
    def _wait_client_health(self): self._called("client_health")
    def _monitor(self): self._called("monitor")


def _endpoint(app, path: str, method: str = "POST"):
    return next(
        route.endpoint
        for route in app.routes
        if getattr(route, "path", None) == path and method in getattr(route, "methods", set())
    )


def test_recipe_example_is_valid_and_references_two_hosts() -> None:
    recipe = parse_recipe(raw_recipe())
    assert recipe.version == 2
    assert recipe.model.provider == "python"
    assert recipe.model.entrypoint == "my_vla:MyVLA"
    assert recipe.model.command == []
    assert recipe.model.host == "model"
    assert recipe.robot.host == "robot"
    assert recipe.tunnel.source_host == "robot"
    assert recipe.tunnel.destination_host == "model"
    assert recipe.runtime.monitor_interval_s == 2
    assert recipe.runtime.component_failure_threshold == 3


def test_runtime_monitor_policy_rejects_invalid_values() -> None:
    raw = raw_recipe()
    raw.setdefault("runtime", {})["component_failure_threshold"] = 0
    with pytest.raises(ValueError, match="component_failure_threshold"):
        parse_recipe(raw)

    raw = raw_recipe()
    raw.setdefault("runtime", {})["monitor_interval_s"] = 0
    with pytest.raises(ValueError, match="monitor_interval_s"):
        parse_recipe(raw)


def test_recipe_can_be_split_and_recomposed_from_independent_configs() -> None:
    robot, model = split_recipe(raw_recipe(), robot_config_id="robot-a", model_config_id="vla-a")
    assert parse_robot_config(robot.model_dump(mode="json")).kind == "robot"
    assert parse_model_config(model.model_dump(mode="json")).kind == "model"
    assert robot.robot.client.host == "robot"
    assert model.model.host is None

    recipe = compose_recipe(robot, model, deployment_id="robot-a--vla-a", name="Robot A + VLA A")
    assert recipe.deployment_id == "robot-a--vla-a"
    assert recipe.hosts["robot"].address == raw_recipe()["hosts"]["robot"]["address"]
    assert recipe.hosts["model"].address == raw_recipe()["hosts"]["model"]["address"]
    assert recipe.robot.host == "robot"
    assert recipe.robot.client.host == "robot"
    assert recipe.model.host == "model"
    assert recipe.tunnel.source_host == "robot"
    assert recipe.tunnel.destination_host == "model"


def test_model_action_horizon_overrides_generic_robot_chunk_during_composition() -> None:
    robot, model = split_recipe(
        python_adapter_recipe(), robot_config_id="robot-a", model_config_id="vla-50"
    )
    robot.robot.client.config["control"]["action_steps"] = 2
    model.model.action_horizon = 50

    recipe = compose_recipe(robot, model)

    assert recipe.model.action_horizon == 50
    assert recipe.robot.client.config["action"]["horizon"] == 50
    assert recipe.robot.client.config["control"]["action_steps"] == 50
    assert robot.robot.client.config["action"]["horizon"] == 2


def test_separate_component_examples_compose_without_recipe_internal_references() -> None:
    robot_raw = json.loads(ROBOT_CONFIG_PATH.read_text(encoding="utf-8"))
    model_raw = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    robot = parse_robot_config(robot_raw)
    model = parse_model_config(model_raw)
    assert "host" not in robot_raw["robot"]
    assert "source_host" not in robot_raw["tunnel"]
    assert "host" not in model_raw["model"]

    recipe = compose_recipe(robot, model)
    assert model.host.connection == "local"
    assert model.host.auth is None
    assert recipe.hosts["model"].connection == "local"
    assert recipe.deployment_id == "robot-a--my-vla"
    assert recipe.tunnel.local_port == 8000
    assert recipe.tunnel.remote_port == 8000


@pytest.mark.parametrize("provider", ["openpi", "lerobot", "starvla"])
def test_checkpoint_model_examples_require_no_user_entrypoint(provider: str) -> None:
    raw = json.loads(CHECKPOINT_MODEL_PATHS[provider].read_text(encoding="utf-8"))
    config = parse_model_config(raw)
    assert config.model.provider == provider
    assert config.model.checkpoint
    assert config.model.entrypoint is None
    assert config.model.command == []


@pytest.mark.parametrize("provider", ["openpi", "lerobot", "starvla"])
def test_checkpoint_model_provider_rejects_missing_checkpoint(provider: str) -> None:
    raw = json.loads(CHECKPOINT_MODEL_PATHS[provider].read_text(encoding="utf-8"))
    raw["model"].pop("checkpoint")
    with pytest.raises(ValueError, match="checkpoint"):
        parse_model_config(raw)


def test_robot_config_can_be_freely_paired_with_a_different_model() -> None:
    robot, first_model = split_recipe(raw_recipe(), robot_config_id="robot-a", model_config_id="vla-a")
    second_model = first_model.model_copy(deep=True)
    second_model.config_id = "vla-b"
    second_model.name = "VLA B"
    second_model.host.address = "10.40.1.199"
    second_model.model.checkpoint = "/root/checkpoints/vla-b"
    second_model.endpoint.port = 8100

    recipe = compose_recipe(robot, second_model)
    assert recipe.deployment_id == "robot-a--vla-b"
    assert recipe.hosts["robot"].address == robot.host.address
    assert recipe.hosts["model"].address == "10.40.1.199"
    assert recipe.model.checkpoint == "/root/checkpoints/vla-b"
    assert recipe.tunnel.local_port == robot.tunnel.local_port
    assert recipe.tunnel.remote_port == 8100


def test_recipe_rejects_tunnel_that_does_not_start_on_robot() -> None:
    raw = raw_recipe()
    raw["tunnel"]["source_host"] = "model"
    with pytest.raises(ValueError, match="source_host"):
        parse_recipe(raw)


def test_recipe_rejects_local_robot_host() -> None:
    raw = raw_recipe()
    raw["hosts"]["robot"]["connection"] = "local"
    raw["hosts"]["robot"].pop("auth")
    with pytest.raises(ValueError, match="本体主机.*SSH"):
        parse_recipe(raw)


def test_local_model_host_rejects_unused_ssh_auth() -> None:
    raw = json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8"))
    raw["host"]["auth"] = {"type": "password", "password": "unused"}
    with pytest.raises(ValueError, match="不能配置 SSH auth"):
        parse_model_config(raw)


def test_recipe_redaction_removes_plaintext_passwords() -> None:
    raw = raw_recipe()
    raw["model"]["environment"]["MODEL_API_TOKEN"] = "sensitive"
    raw["model"]["load_kwargs"]["access_token"] = "also-sensitive"
    redacted = redact_recipe(raw)
    assert redacted["hosts"]["model"]["auth"]["password"] == "********"
    assert redacted["hosts"]["robot"]["auth"]["password"] == "********"
    assert "CHANGE_ME" not in json.dumps(redacted)
    assert redacted["model"]["environment"]["MODEL_API_TOKEN"] == "********"
    assert redacted["model"]["load_kwargs"]["access_token"] == "********"


def test_python_model_rejects_user_managed_http_fields() -> None:
    raw = raw_recipe()
    raw["model"]["command"] = ["python3", "serve.py"]
    with pytest.raises(ValueError, match="自动启动"):
        parse_recipe(raw)

    raw = raw_recipe()
    raw["model"]["health"] = {"type": "http", "url": "http://127.0.0.1:8000/health"}
    with pytest.raises(ValueError, match="自动生成"):
        parse_recipe(raw)


def test_external_model_provider_is_supported() -> None:
    raw = raw_recipe()
    raw["model"] = {
        "host": "model",
        "workdir": "/root/vla",
        "command": ["python3", "serve.py"],
        "health": {"type": "http", "url": "http://127.0.0.1:8000/health"},
    }
    recipe = parse_recipe(raw)
    assert recipe.model.provider == "external"
    assert recipe.model.command == ["python3", "serve.py"]


def test_python_robot_adapter_is_vendor_neutral_and_config_driven() -> None:
    recipe = parse_recipe(python_adapter_recipe())
    client = recipe.robot.client
    assert client.builtin == "python_adapter"
    assert client.config["adapter"]["entrypoint"] == "vendor_adapter:RobotAdapter"
    assert client.config["telemetry"]["cameras"][0]["key"] == "camera"
    assert client.command == []


def test_python_robot_adapter_model_io_is_exposed_in_snapshot(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    model_io = {
        "input": {"cameras": [], "state": {"values": [0.1]}, "prompt": "test"},
        "output": {"action": {"chunk": [[0.2]]}, "inferenceLatencyMs": 5.0},
    }
    orchestration._ingest_python_adapter_status(json.dumps({"status": "ready", "modelIo": model_io}))
    assert orchestration.snapshot()["modelIo"] == model_io


def test_latest_dry_run_safety_rejection_blocks_live_challenge(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.state = OrchestrationState.DRY_RUN
    orchestration._ingest_python_adapter_status(
        json.dumps(
            {
                "status": "ready",
                "mode": "dry_run",
                "safetyPassed": True,
                "safetyError": "action exceeds limit",
                "safetyRejections": 1,
                "updatedMonotonicNs": 123,
            }
        )
    )
    assert orchestration.snapshot()["dryRunSafety"] == {
        "passed": True,
        "error": "action exceeds limit",
        "rejections": 1,
        "updatedMonotonicNs": 123,
    }
    with pytest.raises(ValueError, match="action exceeds limit"):
        orchestration.arm_challenge()


def test_component_monitor_debounces_transient_failure_and_records_recovery(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration._managers[orchestration.model_host_name] = SequenceStatusManager(
        {
            "model": [
                {
                    "probeOk": False,
                    "returnCode": 255,
                    "probeError": "temporary SSH transport error",
                },
                {"probeOk": True, "ActiveState": "active", "SubState": "running", "Result": "success"},
            ]
        }
    )
    orchestration._managers[orchestration.robot_host_name] = SequenceStatusManager()
    failures = {component: 0 for component in orchestration.COMPONENTS}

    orchestration._poll_managed_components(failures, threshold=3)
    assert failures["model"] == 1
    assert orchestration.events[-1]["event"] == "component_unhealthy_observed"
    assert orchestration.events[-1]["component"] == "model"

    orchestration._poll_managed_components(failures, threshold=3)
    assert failures["model"] == 0
    assert orchestration.events[-1]["event"] == "component_recovered"
    assert orchestration.events[-1]["failedChecks"] == 1


def test_component_monitor_faults_only_after_persistent_inactive_status(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration._managers[orchestration.model_host_name] = SequenceStatusManager()
    orchestration._managers[orchestration.robot_host_name] = SequenceStatusManager(
        {
            "client": [
                {
                    "probeOk": True,
                    "ActiveState": "failed",
                    "SubState": "failed",
                    "Result": "exit-code",
                }
            ]
        }
    )
    failures = {component: 0 for component in orchestration.COMPONENTS}

    orchestration._poll_managed_components(failures, threshold=3)
    orchestration._poll_managed_components(failures, threshold=3)
    with pytest.raises(RuntimeError, match=r"client.*3"):
        orchestration._poll_managed_components(failures, threshold=3)


def test_remote_service_status_preserves_systemd_diagnostics() -> None:
    class StatusRunner:
        def run(self, args, **kwargs):
            return RemoteResult(
                0,
                "LoadState=loaded\nActiveState=active\nSubState=running\nResult=success\n"
                "ExecMainCode=0\nExecMainStatus=0\nNRestarts=2\n",
            )

    recipe = parse_recipe(raw_recipe())
    manager = RemoteServiceManager(StatusRunner(), recipe.hosts[recipe.model.host], recipe.deployment_id)
    status = manager.status("model")
    assert status == {
        "probeOk": True,
        "returnCode": 0,
        "LoadState": "loaded",
        "ActiveState": "active",
        "SubState": "running",
        "Result": "success",
        "ExecMainCode": "0",
        "ExecMainStatus": "0",
        "NRestarts": "2",
    }


def test_python_robot_adapter_rejects_wrong_action_telemetry_width() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["telemetry"]["action"]["names"] = ["too_short"]
    with pytest.raises(ValueError, match="telemetry.action.names"):
        parse_recipe(raw)


def test_python_robot_adapter_rejects_invalid_numerical_tolerance() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["action"]["numerical_tolerance"] = -1
    with pytest.raises(ValueError, match="numerical_tolerance"):
        parse_recipe(raw)

    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["action"]["numerical_tolerance"] = [1e-6]
    with pytest.raises(ValueError, match="numerical_tolerance"):
        parse_recipe(raw)


def test_python_robot_adapter_accepts_configured_prompt_list() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["task_prompts"] = ["pick the ball", "put it back"]
    recipe = parse_recipe(raw)
    assert recipe.robot.client.config["task_prompts"] == ["pick the ball", "put it back"]

    raw["robot"]["client"]["config"]["task_prompts"] = [""]
    with pytest.raises(ValueError, match="task_prompts"):
        parse_recipe(raw)


def test_python_robot_adapter_accepts_large_formal_task_registry() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["task_prompts"] = [
        f"formal task prompt {index}" for index in range(110)
    ]
    recipe = parse_recipe(raw)
    assert len(recipe.robot.client.config["task_prompts"]) == 110

    raw["robot"]["client"]["config"]["task_prompts"] = [
        f"task {index}" for index in range(1001)
    ]
    with pytest.raises(ValueError, match="1 到 1000"):
        parse_recipe(raw)

def test_python_robot_adapter_accepts_configurable_async_scheduler() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["control"].update(
        {
            "inference_mode": "asynchronous",
            "action_steps": 2,
            "asynchronous": {"request_after_steps": 1},
        }
    )
    recipe = parse_recipe(raw)
    control = recipe.robot.client.config["control"]
    assert control["inference_mode"] == "asynchronous"
    assert control["action_steps"] == 2
    assert control["asynchronous"]["request_after_steps"] == 1


def test_python_robot_adapter_rejects_async_request_after_chunk_end() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["control"].update(
        {
            "inference_mode": "asynchronous",
            "action_steps": 2,
            "asynchronous": {"request_after_steps": 2},
        }
    )
    with pytest.raises(ValueError, match="action_steps-1"):
        parse_recipe(raw)


def test_python_robot_adapter_accepts_adapter_backed_dry_run_without_synthetic_inputs() -> None:
    raw = python_adapter_recipe()
    config = raw["robot"]["client"]["config"]
    config["dry_run_observation_source"] = "adapter"
    config.pop("dry_run_observations")
    config["control"]["dry_run_rate_hz"] = 5
    recipe = parse_recipe(raw)
    assert recipe.robot.client.config["dry_run_observation_source"] == "adapter"


def test_python_robot_adapter_accepts_absolute_vendor_module_search_paths() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["adapter"]["module_search_paths"] = ["/opt/vendor/sdk"]
    recipe = parse_recipe(raw)
    assert recipe.robot.client.config["adapter"]["module_search_paths"] == ["/opt/vendor/sdk"]


def test_python_robot_adapter_rejects_relative_vendor_module_search_path() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["adapter"]["module_search_paths"] = ["relative/sdk"]
    with pytest.raises(ValueError, match="module_search_paths"):
        parse_recipe(raw)


def test_python_robot_adapter_rejects_unknown_dry_run_observation_source() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["config"]["dry_run_observation_source"] = "camera_magic"
    with pytest.raises(ValueError, match="dry_run_observation_source"):
        parse_recipe(raw)


def test_python_robot_adapter_examples_are_valid() -> None:
    raw = raw_recipe()
    raw["robot"]["client"] = {
        "host": "robot",
        **json.loads(PYTHON_ADAPTER_CLIENT_PATH.read_text(encoding="utf-8")),
    }
    recipe = parse_recipe(raw)
    assert recipe.robot.client.builtin == "python_adapter"
    compile(PYTHON_ADAPTER_SOURCE_PATH.read_text(encoding="utf-8"), str(PYTHON_ADAPTER_SOURCE_PATH), "exec")


def test_builtin_robot_client_rejects_ambiguous_command() -> None:
    raw = python_adapter_recipe()
    raw["robot"]["client"]["command"] = ["python3", "legacy_client.py"]
    with pytest.raises(ValueError, match="自动生成"):
        parse_recipe(raw)


def test_recipe_store_uses_private_permissions(tmp_path: Path) -> None:
    store = RecipeStore(tmp_path / "recipes")
    description = store.save(raw_recipe())
    path = tmp_path / "recipes" / "robot-a-vla.json"
    assert description["version"] == 2
    assert store.get("robot-a-vla")["hosts"]["model"]["auth"]["password"]
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert os.stat(path.parent).st_mode & 0o777 == 0o700


def test_recipe_store_describes_local_model_without_auth(tmp_path: Path) -> None:
    robot = parse_robot_config(json.loads(ROBOT_CONFIG_PATH.read_text(encoding="utf-8")))
    model = parse_model_config(json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8")))
    recipe = compose_recipe(robot, model)
    description = RecipeStore(tmp_path / "recipes").save(recipe.model_dump(mode="json"))
    assert description["auth"]["model"] == {
        "connection": "local",
        "type": None,
        "configured": False,
    }


def test_component_config_stores_are_separate_and_private(tmp_path: Path) -> None:
    robot, model = split_recipe(raw_recipe(), robot_config_id="robot-a", model_config_id="vla-a")
    robot_store = DeploymentConfigStore(tmp_path / "configs", "robot")
    model_store = DeploymentConfigStore(tmp_path / "configs", "model")
    robot_store.save(robot.model_dump(mode="json"))
    model_store.save(model.model_dump(mode="json"))

    assert robot_store.get("robot-a")["kind"] == "robot"
    assert model_store.get("vla-a")["kind"] == "model"
    assert os.stat(tmp_path / "configs" / "robot" / "robot-a.json").st_mode & 0o777 == 0o600
    assert os.stat(tmp_path / "configs" / "model" / "vla-a.json").st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="仅接受 robot"):
        robot_store.save(model.model_dump(mode="json"))


def test_component_config_store_discovers_project_configs_and_saved_override(tmp_path: Path) -> None:
    robot, _model = split_recipe(raw_recipe(), robot_config_id="robot-a", model_config_id="vla-a")
    project = tmp_path / "project-configs"
    project.mkdir()
    (project / "custom-name.json").write_text(
        json.dumps(robot.model_dump(mode="json")), encoding="utf-8"
    )
    store = DeploymentConfigStore(
        tmp_path / "cache", "robot", discovery_roots=[project]
    )
    assert store.get("robot-a")["config_id"] == "robot-a"
    assert store.list()[0]["source"] == "project"

    saved = robot.model_copy(update={"name": "Saved robot"})
    store.save(saved.model_dump(mode="json"))
    listed = store.list()
    assert len([item for item in listed if item["configId"] == "robot-a"]) == 1
    assert listed[0]["source"] == "saved"
    assert store.get("robot-a")["name"] == "Saved robot"


def test_password_ssh_transport_keeps_password_out_of_argv(tmp_path: Path) -> None:
    host = parse_recipe(raw_recipe()).hosts["model"]
    runner = RecipeSshRunner(host, tmp_path / "known_hosts", tmp_path / "askpass")
    command, environment = runner.base_command()
    assert "CHANGE_ME_MODEL_PASSWORD" not in " ".join(command)
    assert environment["EMBODIT_RECIPE_SSH_PASSWORD"] == "CHANGE_ME_MODEL_PASSWORD"
    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert any("ControlMaster=auto" in item for item in command)


def test_password_ssh_transport_can_execute_with_resolved_auth(tmp_path: Path, monkeypatch) -> None:
    host = parse_recipe(raw_recipe()).hosts["model"]
    runner = RecipeSshRunner(host, tmp_path / "known_hosts", tmp_path / "askpass")
    captured = {}

    class Completed:
        returncode = 0
        stdout = b"ok\n"
        stderr = b""

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return Completed()

    monkeypatch.setattr("deploy.transport.subprocess.run", fake_run)
    result = runner.run(["python3", "-c", "print('ok')"])
    assert result.stdout == "ok\n"
    assert captured["kwargs"]["start_new_session"] is True
    assert "CHANGE_ME_MODEL_PASSWORD" not in " ".join(captured["command"])


def test_local_runner_executes_model_commands_without_ssh() -> None:
    runner = LocalCommandRunner()
    result = runner.run(
        ["python3", "-c", "import sys; print(sys.stdin.buffer.read().decode().upper())"],
        input_data=b"embodit",
    )
    assert result.returncode == 0
    assert result.stdout.strip() == "EMBODIT"


def test_orchestration_uses_local_runner_only_for_local_model(tmp_path: Path) -> None:
    robot = parse_robot_config(json.loads(ROBOT_CONFIG_PATH.read_text(encoding="utf-8")))
    model = parse_model_config(json.loads(MODEL_CONFIG_PATH.read_text(encoding="utf-8")))
    orchestration = DeploymentOrchestration(compose_recipe(robot, model), tmp_path)
    assert isinstance(orchestration.model_runner, LocalCommandRunner)
    assert isinstance(orchestration.robot_runner, RecipeSshRunner)


def test_remote_wrapper_enables_nounset_after_sourcing_setup() -> None:
    script = RemoteServiceManager._wrapper(
        CommandSpec(command=["/usr/bin/true"], setup=["/opt/ros/noetic/setup.bash"]),
        {},
    )
    lines = script.splitlines()
    assert lines[:4] == [
        "#!/usr/bin/env bash",
        "set -eo pipefail",
        "source /opt/ros/noetic/setup.bash",
        "set -u",
    ]


def test_ros_command_enables_nounset_after_sourcing_setup(tmp_path: Path) -> None:
    captured = {}

    class CaptureRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return RemoteResult(0, "")

    orchestration = DeploymentOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: CaptureRunner(),
    )
    orchestration._run_ros(["ros2", "node", "list"])
    bootstrap = captured["args"][4]
    assert bootstrap.startswith('set -eo pipefail; count="$1"')
    assert 'source "$1" >&2; shift; done; set -u; env_count=' in bootstrap


def test_ros1_topic_rate_uses_remote_timeout_and_unbuffered_output(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.recipe.robot.ros.version = 1
    topic = orchestration.recipe.robot.readiness.topics[0]
    topic.sample_seconds = 1.5
    captured = {}

    def fake_run_ros(command, timeout=15):
        captured["command"] = command
        captured["timeout"] = timeout
        return RemoteResult(124, "average rate: 250.0\n")

    orchestration._run_ros = fake_run_ros
    orchestration._check_topic_rates()
    assert captured["command"] == [
        "env", "PYTHONUNBUFFERED=1", "timeout", "--signal=INT", "1.5s",
        "rostopic", "hz", "-w", "5", topic.name,
    ]
    assert captured["timeout"] == 4.5


def test_tunnel_credential_python_snippets_are_valid(tmp_path: Path) -> None:
    class CredentialRunner:
        def __init__(self, host_name: str):
            self.host_name = host_name
            self.calls = []

        def run(self, args, **kwargs):
            self.calls.append((args, kwargs))
            if args[:2] == ["python3", "-c"]:
                compile(args[2], f"<{self.host_name}-credential-script>", "exec")
                if "ssh-keygen" in args[2]:
                    return RemoteResult(0, "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIFakeKey test\n")
            return RemoteResult(0, "")

    runners = {}

    def runner_factory(name, _host):
        runners[name] = CredentialRunner(name)
        return runners[name]

    orchestration = DeploymentOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=runner_factory,
    )
    orchestration._robot_home = "/home/robot"
    orchestration._model_home = "/home/model"
    orchestration._ensure_tunnel_credentials()

    model_installer = runners[orchestration.model_host_name].calls[0]
    assert model_installer[1]["input_data"].startswith(b'restrict,port-forwarding,permitopen="')
    robot_scripts = [call[0][2] for call in runners[orchestration.robot_host_name].calls]
    assert any("ssh-keygen" in script for script in robot_scripts)
    assert any("ssh-keyscan" in script for script in robot_scripts)


def test_orchestration_runs_fixed_order_and_finishes_in_dry_run(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.start()
    orchestration._thread.join(timeout=2)
    assert orchestration.state == OrchestrationState.DRY_RUN
    assert orchestration.calls == [
        "precheck", "tunnel_credentials", "model", "model_health", "tunnel", "tunnel_health",
        "ros", "ros_readiness", "power_on", "initial_pose", "client", "client_health", "monitor",
    ]
    assert all(step["status"] == "passed" for step in orchestration.steps)


def test_orchestration_can_prepare_model_then_start_prompted_dry_run(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.prepare_model()
    orchestration._thread.join(timeout=2)
    assert orchestration.state == OrchestrationState.MODEL_READY
    assert orchestration.calls == ["precheck", "model", "model_health"]

    # The harness records calls but does not materialize service state like the real implementation.
    orchestration.components["model"]["active"] = True
    orchestration.start(task_prompt="pick up the red block")
    orchestration._thread.join(timeout=2)
    assert orchestration.state == OrchestrationState.DRY_RUN
    assert orchestration.recipe.robot.client.config["task_prompt"] == "pick up the red block"
    assert orchestration.calls == [
        "precheck", "model", "model_health", "tunnel_credentials", "tunnel", "tunnel_health",
        "ros", "ros_readiness", "power_on", "initial_pose", "client", "client_health", "monitor",
    ]


def test_model_ready_can_start_live_evaluation_without_dry_run_challenge(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.state = OrchestrationState.MODEL_READY
    orchestration.components["model"]["active"] = True

    snapshot = orchestration.start_evaluation(task_prompt="pick the blue block")
    assert snapshot["state"] == "starting"
    orchestration._thread.join(timeout=2)

    assert orchestration.state == OrchestrationState.RUNNING
    assert orchestration.mode == "live"
    assert orchestration.recipe.robot.client.config["task_prompt"] == "pick the blue block"
    assert "model" not in orchestration.calls
    assert orchestration.calls[-1] == "monitor"


def test_prompt_can_change_while_model_stays_ready(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.state = OrchestrationState.MODEL_READY
    orchestration.components["model"]["active"] = True
    snapshot = orchestration.update_task_prompt("put the block in the tray")
    assert snapshot["state"] == "model_ready"
    assert snapshot["components"]["model"]["active"] is True
    assert orchestration.recipe.robot.client.config["task_prompt"] == "put the block in the tray"
    assert orchestration.events[-1]["event"] == "prompt_updated"
    assert orchestration.events[-1]["clientRestarted"] is False


def test_pose_record_captures_only_model_state_vector(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.model_io = {
        "input": {
            "state": {
                "values": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
                "names": [f"joint_{index}" for index in range(6)],
                "units": ["rad"] * 6,
            },
            "cameras": [{"key": "camera", "dataUrl": "ignored"}],
        },
        "output": {"action": {"chunk": [[0.0] * 6]}},
    }
    snapshot = orchestration.record_pose("实验起点")
    assert snapshot["recordedPoses"] == [
        {
            "poseId": snapshot["recordedPoses"][0]["poseId"],
            "name": "实验起点",
            "values": [0.1, 0.2, 0.3, 0.4, 0.5, 0.6],
            "names": [f"joint_{index}" for index in range(6)],
            "units": ["rad"] * 6,
            "createdNs": snapshot["recordedPoses"][0]["createdNs"],
        }
    ]
    assert "cameras" not in snapshot["recordedPoses"][0]

    pose_id = snapshot["recordedPoses"][0]["poseId"]
    deleted = orchestration.delete_pose(pose_id)
    assert deleted["recordedPoses"] == []


def test_failed_dry_run_returns_to_model_ready_without_stopping_model(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
        fail_at="client_health",
    )

    class RecordingManager:
        def __init__(self): self.stopped = []
        def stop(self, component): self.stopped.append(component)

    robot_manager = RecordingManager()
    model_manager = RecordingManager()
    orchestration._managers[orchestration.robot_host_name] = robot_manager
    orchestration._managers[orchestration.model_host_name] = model_manager
    orchestration.state = OrchestrationState.MODEL_READY
    for component in ("model", "tunnel", "ros", "client"):
        orchestration.components[component]["active"] = True

    orchestration.start()
    orchestration._thread.join(timeout=2)

    assert orchestration.state == OrchestrationState.MODEL_READY
    assert "failed at client_health" in orchestration.last_error
    assert robot_manager.stopped == ["client", "ros", "tunnel"]
    assert model_manager.stopped == []
    assert orchestration.components["model"]["active"] is True


def test_stop_evaluation_returns_to_dry_run_without_stopping_model_stack(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )

    class RecordingManager:
        def __init__(self): self.stopped = []
        def stop(self, component): self.stopped.append(component)

    manager = RecordingManager()
    orchestration._managers[orchestration.robot_host_name] = manager
    orchestration.state = OrchestrationState.RUNNING
    orchestration.mode = "live"
    for component in ("model", "tunnel", "ros", "client"):
        orchestration.components[component]["active"] = True

    def start_client():
        orchestration._called("client")
        orchestration.components["client"]["active"] = True

    orchestration._start_client = start_client
    snapshot = orchestration.stop_evaluation()

    assert snapshot["state"] == "dry_run"
    assert orchestration.mode == "dry_run"
    assert manager.stopped == ["client"]
    assert orchestration.calls == ["结束评测 hold", "client", "client_health"]
    assert all(orchestration.components[name]["active"] for name in ("model", "tunnel", "ros", "client"))


def test_orchestration_failure_is_reported_as_fault(tmp_path: Path) -> None:
    orchestration = HarnessOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
        fail_at="tunnel_health",
    )
    orchestration.start()
    orchestration._thread.join(timeout=2)
    assert orchestration.state == OrchestrationState.FAULT
    assert "failed at tunnel_health" in orchestration.last_error
    assert orchestration.steps[-1]["status"] == "failed"


def test_builtin_ros2_client_is_materialized_on_robot(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    manager = CapturingManager()
    orchestration._managers["robot"] = manager
    orchestration._start_client()
    assert any(path.endswith("ros2_robot_client.py") for path in manager.files)
    assert any(path.endswith("ros2_robot_client.json") and mode == 0o600 for path, (_, mode) in manager.files.items())
    component, spec, environment = manager.started
    assert component == "client"
    assert spec.command[0] == "python3"
    assert environment["EMBODIT_DEPLOYMENT_MODE"] == "dry_run"


def test_python_adapter_runtime_is_materialized_on_robot(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(python_adapter_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    orchestration.recipe.robot.client.config["task_prompt"] = "move the object"
    source_file = tmp_path / "vendor_adapter.py"
    source_file.write_text("class RobotAdapter: pass\n", encoding="utf-8")
    orchestration.recipe.robot.client.config["adapter"]["source_file"] = str(source_file)
    orchestration.recipe.robot.client.config["adapter"].pop("source_path")
    manager = CapturingManager()
    orchestration._managers["robot"] = manager
    orchestration._start_client()

    assert any(path.endswith("python_robot_client.py") for path in manager.files)
    assert any(path.endswith("python_adapter/vendor_adapter.py") for path in manager.files)
    status_payload, status_mode = next(
        value for path, value in manager.files.items() if path.endswith("python_robot_client.status.json")
    )
    config_payload, config_mode = next(
        value for path, value in manager.files.items() if path.endswith("python_robot_client.json")
    )
    generated = json.loads(config_payload)
    assert status_payload == b'{"status":"pending"}\n'
    assert status_mode == 0o600
    assert config_mode == 0o600
    assert generated["task_prompt"] == "move the object"
    assert generated["model"]["endpoint"] == "http://127.0.0.1:8000"
    assert generated["adapter"]["source_path"].endswith("/python_adapter")
    assert "source_file" not in generated["adapter"]
    component, spec, environment = manager.started
    assert component == "client"
    assert spec.command[0] == "/usr/bin/python3"
    assert environment["EMBODIT_DEPLOYMENT_MODE"] == "dry_run"


def test_python_model_runner_is_materialized_on_model_host(tmp_path: Path) -> None:
    orchestration = DeploymentOrchestration(
        parse_recipe(raw_recipe()),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    manager = CapturingManager()
    orchestration._managers["model"] = manager
    orchestration._start_model()

    assert any(path.endswith("model_runner.py") for path in manager.files)
    config_payload, config_mode = next(
        value for path, value in manager.files.items() if path.endswith("model_runner.json")
    )
    generated = json.loads(config_payload)
    assert config_mode == 0o600
    assert generated["entrypoint"] == "my_vla:MyVLA"
    assert generated["checkpoint"] == "/root/checkpoints/my-vla"
    component, spec, _environment = manager.started
    assert component == "model"
    assert spec.command[0] == "/root/miniconda3/envs/vla/bin/python"
    assert spec.command[-2:] == ["--port", "8000"]


@pytest.mark.parametrize(
    ("provider", "entrypoint", "source_suffix"),
    [
        ("openpi", "model_adapters:OpenPIAdapter", "third_party/models/openpi/src"),
        ("lerobot", "model_adapters:LeRobotAdapter", "third_party/models/lerobot/src"),
        ("starvla", "model_adapters:StarVLAAdapter", "third_party/models/starvla"),
    ],
)
def test_checkpoint_provider_is_materialized_with_pinned_adapter(
    tmp_path: Path,
    provider: str,
    entrypoint: str,
    source_suffix: str,
) -> None:
    raw = raw_recipe()
    raw["model"].update({
        "provider": provider,
        "checkpoint": f"/root/checkpoints/{provider}",
        "workdir": "/root/Embodit",
        "python_executable": f"/root/miniconda3/envs/{provider}/bin/python",
    })
    raw["model"].pop("entrypoint", None)
    orchestration = DeploymentOrchestration(
        parse_recipe(raw),
        tmp_path,
        runner_factory=lambda _name, _host: FakeRunner(),
    )
    manager = CapturingManager()
    orchestration._managers["model"] = manager
    orchestration._start_model()

    assert any(path.endswith("model_adapters.py") for path in manager.files)
    config_payload = next(
        payload for path, (payload, _mode) in manager.files.items() if path.endswith("model_runner.json")
    )
    generated = json.loads(config_payload)
    assert generated["entrypoint"] == entrypoint
    assert generated["checkpoint"] == f"/root/checkpoints/{provider}"
    assert generated["load_kwargs"]["source_path"] == f"/root/Embodit/{source_suffix}"
    assert manager.started[1].command[0].endswith(f"/{provider}/bin/python")


def test_api_validates_saves_and_serves_recipe_example(tmp_path: Path, monkeypatch) -> None:
    import settings

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    app = build_app("token", tmp_path, tmp_path)
    validate = _endpoint(app, "/api/deploy/recipes/validate")
    save = _endpoint(app, "/api/deploy/recipes")
    list_recipes = _endpoint(app, "/api/deploy/recipes", method="GET")
    example = _endpoint(app, "/api/deploy/examples/{name}", method="GET")

    checked = validate(DeploymentRecipeRequest(recipe=raw_recipe()))
    assert checked["valid"] is True
    assert checked["version"] == 2
    assert checked["recipe"]["hosts"]["model"]["auth"]["password"] == "********"
    assert save(DeploymentRecipeRequest(recipe=raw_recipe()))["recipe"]["version"] == 2
    assert list_recipes()["recipes"][0]["version"] == 2
    assert example("recipe")["recipe"]["version"] == 2
    assert example("robot-config")["config"]["kind"] == "robot"
    assert example("model-config")["config"]["kind"] == "model"


def test_api_saves_components_and_composes_a_recipe(tmp_path: Path, monkeypatch) -> None:
    import settings

    monkeypatch.setattr(settings, "CACHE_DIR", tmp_path / "cache")
    app = build_app("token", tmp_path, tmp_path)
    robot, model = split_recipe(raw_recipe(), robot_config_id="robot-a", model_config_id="vla-a")
    save_config = _endpoint(app, "/api/deploy/configs/{kind}")
    list_configs = _endpoint(app, "/api/deploy/configs/{kind}", method="GET")
    compose = _endpoint(app, "/api/deploy/compose")

    save_config("robot", DeploymentConfigRequest(config=robot.model_dump(mode="json")))
    save_config("model", DeploymentConfigRequest(config=model.model_dump(mode="json")))
    assert list_configs("robot")["configs"][0]["configId"] == "robot-a"
    assert list_configs("model")["configs"][0]["configId"] == "vla-a"

    result = compose(DeploymentComposeRequest(
        robot=robot.model_dump(mode="json"),
        model=model.model_dump(mode="json"),
        deployment_id="robot-a--vla-a",
        name="Robot A + VLA A",
    ))
    assert result["valid"] is True
    assert result["recipe"]["deployment_id"] == "robot-a--vla-a"
    assert result["recipe"]["hosts"]["robot"]["auth"]["password"] == "CHANGE_ME_ROBOT_PASSWORD"


def test_api_exposes_orchestration_control_routes(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/deploy/orchestrations" in paths
    assert "/api/deploy/robot-connection" in paths
    assert "/api/deploy/orchestrations/prepare-model" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/start-dry-run" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/start-evaluation" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/prompt" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/poses" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/poses/{pose_id}/move" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/poses/{pose_id}" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/start-live" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/stop-evaluation" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/emergency-stop" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/logs" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/components/{component}/restart" in paths
    assert "/api/deploy/recipes" in paths
    assert "/api/deploy/model-catalog" in paths
    assert not any(path.startswith("/api/deploy/sessions") for path in paths)
    assert not any(path.startswith("/api/deploy/profiles") for path in paths)

    capabilities = _endpoint(app, "/api/deploy/capabilities", method="GET")()
    catalog = _endpoint(app, "/api/deploy/model-catalog", method="GET")()
    assert capabilities["features"]["localModelHost"] is True
    assert capabilities["checkpointModelProviders"] == ["openpi", "lerobot", "starvla"]
    assert capabilities["robotClients"] == ["ros2_standard", "python_adapter", "custom"]
    assert {item["id"] for item in catalog["models"]} == {"openpi", "lerobot", "starvla"}
    assert all(item["checkpointOnly"] and len(item["revision"]) == 40 for item in catalog["models"])


def test_web_workspace_keeps_deployment_config_read_only() -> None:
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="deploymentComponents"' not in html
    assert 'id="deploymentModelIo"' in html
    assert 'id="deploymentCameraGrid"' in html
    assert 'id="deploymentStateGrid"' in html
    assert 'id="deploymentActionGrid"' in html
    assert 'id="deployMetricSteps"' not in html
    assert 'id="splitDeploymentSidebar"' in html
    assert 'id="deploymentRobotConfig" readonly' in html
    assert 'id="deploymentModelConfig" readonly' in html
    assert 'id="deploymentId" value="robot-a--my-vla" readonly' in html
    assert 'id="deploymentName" value="Robot A + My VLA" readonly' in html
    assert 'id="deploymentRobotSelect"' in html
    assert 'id="deploymentRobotStatus"' in html
    assert 'id="checkDeploymentRobot"' in html
    assert 'id="deploymentModelSelect"' in html
    assert 'id="deploymentModelStatus"' in html
    assert 'id="prepareDeploymentModel"' in html
    assert 'id="deploymentTaskPrompt"' in html
    assert 'id="quickstartDeployment"' not in html
    assert 'id="startLiveDeployment"' in html
    assert 'id="stopLiveDeployment"' in html
    assert 'id="applyDeploymentPrompt"' in html
    assert 'id="deploymentPoseSelect"' in html
    assert 'id="deploymentPoseName"' in html
    assert 'id="recordDeploymentPose"' in html
    assert 'id="moveDeploymentPose"' in html
    assert 'id="deploymentExecutionLog"' in html
    assert 'id="deploymentActionTrajectory"' in html
    assert 'id="loadDeploymentExample"' not in html
    assert 'id="deploymentSavedRobot"' not in html
    assert 'id="deploymentSavedModel"' not in html
    assert 'id="deploymentFile"' not in html
    assert 'id="exportDeployment"' not in html
    assert 'id="deploymentModelFamily"' not in html
    assert 'id="deploymentCheckpoint"' not in html
    assert "saveDeploymentConfig" not in javascript
    assert "deleteSavedDeploymentConfig" not in javascript
    assert "importDeploymentFile" not in javascript
    assert "/api/deploy/orchestrations/prepare-model" in javascript
    assert "/start-dry-run" in javascript
    assert 'id="disconnectDeploymentRobot"' in html
    assert 'id="closeDeploymentModel"' in html
    assert 'id="splitDeploymentLog"' in html
    assert 'id="emergencyStopDeployment"' not in html
    assert "/start-evaluation" in javascript
    assert "/prompt" in javascript
    assert "/poses" in javascript
    assert "arm-challenge" not in javascript
    assert "给当前模型关节位姿命名" not in javascript
    assert "/stop-evaluation" in javascript
    assert "taskPrompt" in javascript
    assert "/api/deploy/compose" in javascript
    assert "/api/deploy/configs/robot" in javascript
    assert "/api/deploy/configs/model" in javascript
    assert "/api/deploy/orchestrations" in javascript
    assert "renderDeploymentModelIo(snapshot.modelIo, snapshot.dryRunSafety, snapshot.trajectoryHistory, snapshot.runtimeTiming)" in javascript
    assert 'id="deploymentInferenceMode"' in html
    assert 'id="disconnectDeploymentRobot"' in html
    assert 'id="closeDeploymentModel"' in html
    assert 'id="deploymentLogFilter"' in html
    assert "snapshot?.state === 'dry_run'" in javascript
    assert "/api/deploy/sessions" not in javascript
