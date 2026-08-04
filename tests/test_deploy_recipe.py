import json
import os
import time
from pathlib import Path

import pytest

from app import DeploymentRecipeRequest, build_app
from deploy.orchestrator import DeploymentOrchestration, OrchestrationState
from deploy.recipe import parse_recipe, redact_recipe
from deploy.store import RecipeStore
from deploy.transport import RecipeSshRunner, RemoteResult


PROJECT_ROOT = Path(__file__).resolve().parents[1]
RECIPE_PATH = PROJECT_ROOT / "config" / "deployment.recipe-v2.example.json"


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


def test_recipe_v2_example_is_valid_and_references_two_hosts() -> None:
    recipe = parse_recipe(raw_recipe())
    assert recipe.version == 2
    assert recipe.model.provider == "python"
    assert recipe.model.entrypoint == "my_vla:MyVLA"
    assert recipe.model.command == []
    assert recipe.model.host == "model"
    assert recipe.robot.host == "robot"
    assert recipe.tunnel.source_host == "robot"
    assert recipe.tunnel.destination_host == "model"


def test_recipe_rejects_tunnel_that_does_not_start_on_robot() -> None:
    raw = raw_recipe()
    raw["tunnel"]["source_host"] = "model"
    with pytest.raises(ValueError, match="source_host"):
        parse_recipe(raw)


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


def test_password_ssh_transport_keeps_password_out_of_argv(tmp_path: Path) -> None:
    host = parse_recipe(raw_recipe()).hosts["model"]
    runner = RecipeSshRunner(host, tmp_path / "known_hosts", tmp_path / "askpass")
    command, environment = runner.base_command()
    assert "CHANGE_ME_MODEL_PASSWORD" not in " ".join(command)
    assert environment["EMBODIT_RECIPE_SSH_PASSWORD"] == "CHANGE_ME_MODEL_PASSWORD"
    assert environment["SSH_ASKPASS_REQUIRE"] == "force"
    assert any("ControlMaster=auto" in item for item in command)


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


def test_api_validates_saves_and_serves_recipe_v2_example(tmp_path: Path, monkeypatch) -> None:
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
    assert example("recipe-v2")["recipe"]["version"] == 2


def test_api_exposes_orchestration_control_routes(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    paths = {getattr(route, "path", "") for route in app.routes}
    assert "/api/deploy/orchestrations" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/start-live" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/emergency-stop" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/logs" in paths
    assert "/api/deploy/orchestrations/{orchestration_id}/components/{component}/restart" in paths
    assert "/api/deploy/recipes" in paths
    assert not any(path.startswith("/api/deploy/sessions") for path in paths)
    assert not any(path.startswith("/api/deploy/profiles") for path in paths)


def test_web_workspace_defaults_to_recipe_v2_controls() -> None:
    html = (PROJECT_ROOT / "web" / "index.html").read_text(encoding="utf-8")
    javascript = (PROJECT_ROOT / "web" / "app.js").read_text(encoding="utf-8")
    assert 'id="loadDeploymentExample"' in html
    assert 'id="deploymentComponents"' in html
    assert "await loadDeploymentExample('recipe-v2')" in javascript
    assert "/api/deploy/orchestrations" in javascript
    assert "/api/deploy/sessions" not in javascript
