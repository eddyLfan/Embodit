import json
import os
import time
from pathlib import Path

import pytest

from app import DeploymentComposeRequest, DeploymentConfigRequest, DeploymentRecipeRequest, build_app
from deploy.orchestrator import DeploymentOrchestration, OrchestrationState
from deploy.recipe import compose_recipe, parse_model_config, parse_recipe, parse_robot_config, redact_recipe, split_recipe
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


def raw_recipe() -> dict:
    return json.loads(RECIPE_PATH.read_text(encoding="utf-8"))


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
    assert "/api/deploy/orchestrations/{orchestration_id}/start-live" in paths
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
    assert {item["id"] for item in catalog["models"]} == {"openpi", "lerobot", "starvla"}
    assert all(item["checkpointOnly"] and len(item["revision"]) == 40 for item in catalog["models"])


def test_web_workspace_keeps_deployment_config_read_only() -> None:
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="deploymentComponents"' in html
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
    assert 'id="quickstartDeployment"' in html
    assert 'id="startLiveDeployment"' in html
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
    assert "taskPrompt" in javascript
    assert "/api/deploy/compose" in javascript
    assert "/api/deploy/configs/robot" in javascript
    assert "/api/deploy/configs/model" in javascript
    assert "/api/deploy/orchestrations" in javascript
    assert "/api/deploy/sessions" not in javascript
