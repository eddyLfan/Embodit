"""Composable deployment configs and runtime recipe for real robots.

Robot/model Config documents are independently reusable. They compose into a
Recipe, which deliberately keeps the Embodit Hub out of the control data
path and describes the complete model, tunnel, ROS, and robot-client runtime.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, PositiveFloat, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)


class SshAuth(StrictModel):
    type: Literal["key", "password", "password_env"]
    identity_file: str | None = None
    password: str | None = None
    environment_variable: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_]*$",
    )

    @model_validator(mode="after")
    def validate_auth(self) -> "SshAuth":
        if self.type == "key":
            if not self.identity_file:
                raise ValueError("key 认证必须配置 identity_file")
        elif self.type == "password":
            if not self.password:
                raise ValueError("password 认证必须配置 password")
        elif not self.environment_variable:
            raise ValueError("password_env 认证必须配置 environment_variable")
        return self

    def resolved_password(self) -> str | None:
        if self.type == "password":
            return self.password
        if self.type == "password_env":
            value = os.environ.get(self.environment_variable or "", "")
            if not value:
                raise ValueError(f"SSH 密码环境变量未设置：{self.environment_variable}")
            return value
        return None


class RecipeHost(StrictModel):
    connection: Literal["ssh", "local"] = "ssh"
    address: str = Field(min_length=1)
    port: int = Field(default=22, ge=1, le=65535)
    user: str = Field(min_length=1)
    auth: SshAuth | None = None
    connect_timeout_s: int = Field(default=8, ge=1, le=60)
    host_key_policy: Literal["strict", "accept-new"] = "accept-new"
    service_manager: Literal["system", "user"] = "system"

    @model_validator(mode="after")
    def validate_target(self) -> "RecipeHost":
        if self.connection == "ssh" and self.auth is None:
            raise ValueError("SSH 主机必须配置 auth")
        if self.connection == "local" and self.auth is not None:
            raise ValueError("本地主机由 Embodit 直接执行，不能配置 SSH auth")
        for label, value in (("address", self.address), ("user", self.user)):
            if value.startswith("-") or any(char.isspace() for char in value):
                raise ValueError(f"主机 {label} 非法")
        if "@" in self.user:
            raise ValueError("主机 user 不能包含 @")
        return self


class CommandSpec(StrictModel):
    command: list[str] = Field(default_factory=list)
    workdir: str | None = None
    setup: list[str] = Field(default_factory=list)
    environment: dict[str, str] = Field(default_factory=dict)
    startup_timeout_s: PositiveFloat = 60
    restart: Literal["no", "on-failure", "always"] = "on-failure"

    @model_validator(mode="after")
    def validate_command(self) -> "CommandSpec":
        generated_model_command = getattr(self, "provider", "external") != "external"
        if not self.command and getattr(self, "builtin", None) is None and not generated_model_command:
            raise ValueError("command 至少需要一个参数")
        if any(not item or "\x00" in item for item in self.command):
            raise ValueError("command 不允许空参数或 NUL")
        if self.workdir is not None and not Path(self.workdir).is_absolute():
            raise ValueError("workdir 必须使用目标主机绝对路径")
        if any(not Path(path).is_absolute() for path in self.setup):
            raise ValueError("setup 必须使用目标主机绝对路径")
        invalid_environment = [
            key for key in self.environment if not re_fullmatch_environment_name(key)
        ]
        if invalid_environment:
            raise ValueError("environment 包含非法变量名：" + ", ".join(invalid_environment))
        return self


class HealthCheck(StrictModel):
    type: Literal["http", "tcp", "command", "ros_node"]
    url: str | None = None
    host: str = "127.0.0.1"
    port: int | None = Field(default=None, ge=1, le=65535)
    command: list[str] = Field(default_factory=list)
    name: str | None = None
    startup_timeout_s: PositiveFloat = 60
    interval_s: PositiveFloat = 1

    @model_validator(mode="after")
    def validate_health(self) -> "HealthCheck":
        if self.type == "http" and not self.url:
            raise ValueError("HTTP health 必须配置 url")
        if self.type == "tcp" and self.port is None:
            raise ValueError("TCP health 必须配置 port")
        if self.type == "command" and not self.command:
            raise ValueError("command health 必须配置 command")
        if self.type == "ros_node" and not self.name:
            raise ValueError("ros_node health 必须配置 name")
        return self


class ManagedService(CommandSpec):
    host: str | None = None
    health: HealthCheck | None = None


class ModelService(ManagedService):
    """Model process description.

    Built-in OpenPI/LeRobot/StarVLA providers take a checkpoint and use a
    pinned adapter. ``python`` accepts a user entrypoint implementing ``load``
    and ``predict``. ``external`` preserves the command/HTTP contract for an
    already deployed inference service.
    """

    provider: Literal["external", "python", "openpi", "lerobot", "starvla"] = "external"
    entrypoint: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z_][A-Za-z0-9_.]*:[A-Za-z_][A-Za-z0-9_.]*$",
    )
    checkpoint: str | None = None
    python_executable: str = "python3"
    load_method: str = Field(default="load", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    predict_method: str = Field(default="predict", pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    load_kwargs: dict[str, Any] = Field(default_factory=dict)
    predict_kwargs: dict[str, Any] = Field(default_factory=dict)
    maximum_request_bytes: int = Field(default=50_000_000, ge=1024, le=1_000_000_000)
    source_path: str | None = None

    @model_validator(mode="after")
    def validate_provider(self) -> "ModelService":
        if self.provider == "python":
            if not self.entrypoint:
                raise ValueError("python 模型必须配置 entrypoint")
            if not self.checkpoint:
                raise ValueError("python 模型必须配置 checkpoint")
            if self.command:
                raise ValueError("python 模型由 Embodit 自动启动，不能同时配置 command")
            if self.health is not None:
                raise ValueError("python 模型健康检查由 Embodit 自动生成，不能配置 health")
        elif self.provider in {"openpi", "lerobot", "starvla"}:
            if not self.checkpoint:
                raise ValueError(f"{self.provider} 模型必须配置 checkpoint")
            if self.entrypoint:
                raise ValueError(f"{self.provider} 是内置 Provider，不能配置 entrypoint")
            if self.command:
                raise ValueError(f"{self.provider} 模型由 Embodit 自动启动，不能同时配置 command")
            if self.health is not None:
                raise ValueError(f"{self.provider} 模型健康检查由 Embodit 自动生成，不能配置 health")
            if not self.workdir:
                raise ValueError(f"{self.provider} 模型必须配置包含 third_party/models 的目标主机 workdir")
        if self.provider != "external" and (not self.python_executable or "\x00" in self.python_executable):
            raise ValueError("python_executable 非法")
        if self.source_path is not None and not Path(self.source_path).is_absolute():
            raise ValueError("source_path 必须使用目标主机绝对路径")
        return self


class RobotClientService(ManagedService):
    builtin: Literal["ros2_standard"] | None = None
    config: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_builtin(self) -> "RobotClientService":
        if self.builtin == "ros2_standard":
            if not isinstance(self.config, dict):
                raise ValueError("ros2_standard Client 必须配置 config")
            required = {"observations", "controller", "action"}
            missing = sorted(required - set(self.config))
            if missing:
                raise ValueError("ros2_standard Client config 缺少：" + ", ".join(missing))
        return self


class TunnelSpec(StrictModel):
    source_host: str
    destination_host: str
    local_bind: str = "127.0.0.1"
    local_port: int = Field(ge=1, le=65535)
    remote_bind: str = "127.0.0.1"
    remote_port: int = Field(ge=1, le=65535)
    server_alive_interval_s: int = Field(default=10, ge=1, le=300)
    server_alive_count_max: int = Field(default=3, ge=1, le=20)
    restart: Literal["on-failure", "always"] = "always"
    health_path: str = "/health"
    startup_timeout_s: PositiveFloat = 30


class RosRuntime(StrictModel):
    version: Literal[1, 2]
    distro: str
    setup: list[str]
    domain_id: int | None = Field(default=None, ge=0, le=232)
    master_uri: str | None = None
    rmw_implementation: str | None = None

    @model_validator(mode="after")
    def validate_ros(self) -> "RosRuntime":
        if any(not Path(path).is_absolute() for path in self.setup):
            raise ValueError("ROS setup 必须使用本体主机绝对路径")
        if self.version == 1 and self.domain_id is not None:
            raise ValueError("ROS1 不支持 domain_id")
        if self.version == 2 and self.master_uri is not None:
            raise ValueError("ROS2 不支持 master_uri")
        return self


class TopicRequirement(StrictModel):
    name: str = Field(pattern=r"^/")
    type: str
    minimum_rate_hz: float = Field(default=0, ge=0)
    sample_seconds: float = Field(default=2, gt=0, le=15)
    maximum_age_ms: float | None = Field(default=None, gt=0)


class TypedRequirement(StrictModel):
    name: str = Field(pattern=r"^/")
    type: str


class RosReadiness(StrictModel):
    timeout_s: PositiveFloat = 60
    interval_s: PositiveFloat = 1
    nodes: list[str] = Field(default_factory=list)
    topics: list[TopicRequirement] = Field(default_factory=list)
    services: list[TypedRequirement] = Field(default_factory=list)
    actions: list[TypedRequirement] = Field(default_factory=list)


class RobotOperation(StrictModel):
    type: Literal["none", "command", "ros2_service", "ros1_service"] = "none"
    command: list[str] = Field(default_factory=list)
    name: str | None = None
    service_type: str | None = None
    request: dict[str, Any] = Field(default_factory=dict)
    timeout_s: PositiveFloat = 15

    @model_validator(mode="after")
    def validate_operation(self) -> "RobotOperation":
        if self.type == "command" and not self.command:
            raise ValueError("command operation 必须配置 command")
        if self.type in {"ros1_service", "ros2_service"} and (not self.name or not self.service_type):
            raise ValueError("ROS service operation 必须配置 name 和 service_type")
        return self


class InitialPose(StrictModel):
    type: Literal["none", "follow_joint_trajectory", "command"] = "none"
    action: str | None = None
    command: list[str] = Field(default_factory=list)
    joint_state_topic: str = "/joint_states"
    joint_names: list[str] = Field(default_factory=list)
    positions: list[float] = Field(default_factory=list)
    duration_s: PositiveFloat = 5
    tolerance: PositiveFloat = 0.03
    timeout_s: PositiveFloat = 20

    @model_validator(mode="after")
    def validate_pose(self) -> "InitialPose":
        if self.type == "command" and not self.command:
            raise ValueError("command initial_pose 必须配置 command")
        if self.type == "follow_joint_trajectory":
            if not self.action or not self.joint_names or not self.positions:
                raise ValueError("trajectory initial_pose 缺少 action/joint_names/positions")
            if len(self.joint_names) != len(self.positions):
                raise ValueError("initial_pose joint_names 与 positions 维度不一致")
        return self


class RobotDeployment(StrictModel):
    host: str
    ros: RosRuntime
    bringup: CommandSpec
    readiness: RosReadiness = Field(default_factory=RosReadiness)
    power_on: RobotOperation = Field(default_factory=RobotOperation)
    power_off: RobotOperation = Field(default_factory=RobotOperation)
    hold: RobotOperation = Field(default_factory=RobotOperation)
    stop: RobotOperation = Field(default_factory=RobotOperation)
    initial_pose: InitialPose = Field(default_factory=InitialPose)
    client: RobotClientService


class RuntimePolicy(StrictModel):
    default_mode: Literal["dry_run", "live"] = "dry_run"
    auto_rollback: bool = True
    stop_model_on_exit: bool = True
    power_off_on_exit: bool = False


class RobotBody(StrictModel):
    """Reusable robot-only portion of a deployment.

    Host references deliberately live outside this model so one robot config
    can be paired with any model config without editing internal aliases.
    """

    ros: RosRuntime
    bringup: CommandSpec
    readiness: RosReadiness = Field(default_factory=RosReadiness)
    power_on: RobotOperation = Field(default_factory=RobotOperation)
    power_off: RobotOperation = Field(default_factory=RobotOperation)
    hold: RobotOperation = Field(default_factory=RobotOperation)
    stop: RobotOperation = Field(default_factory=RobotOperation)
    initial_pose: InitialPose = Field(default_factory=InitialPose)
    client: RobotClientService

    @model_validator(mode="after")
    def validate_client_runtime(self) -> "RobotBody":
        if self.client.builtin == "ros2_standard" and self.ros.version != 2:
            raise ValueError("内置 ros2_standard Client 只能用于 ROS2")
        return self


class RobotTunnelConfig(StrictModel):
    """Robot-side half of the managed model tunnel."""

    local_bind: str = "127.0.0.1"
    local_port: int = Field(ge=1, le=65535)
    server_alive_interval_s: int = Field(default=10, ge=1, le=300)
    server_alive_count_max: int = Field(default=3, ge=1, le=20)
    restart: Literal["on-failure", "always"] = "always"
    health_path: str = "/health"
    startup_timeout_s: PositiveFloat = 30


class ModelEndpoint(StrictModel):
    """Model-side endpoint consumed by a robot tunnel."""

    bind: str = "127.0.0.1"
    port: int = Field(ge=1, le=65535)


class RobotConfig(StrictModel):
    version: Literal[1] = 1
    kind: Literal["robot"] = "robot"
    config_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1)
    host: RecipeHost
    robot: RobotBody
    tunnel: RobotTunnelConfig
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @model_validator(mode="after")
    def validate_remote_robot(self) -> "RobotConfig":
        if self.host.connection != "ssh":
            raise ValueError("本体主机当前必须使用 SSH 连接")
        return self


class ModelConfig(StrictModel):
    version: Literal[1] = 1
    kind: Literal["model"] = "model"
    config_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1)
    host: RecipeHost
    model: ModelService
    endpoint: ModelEndpoint

    @model_validator(mode="after")
    def validate_service_host(self) -> "ModelConfig":
        # Component configs have no recipe host namespace. Accept an omitted
        # host or the canonical alias only, then normalize during composition.
        if self.model.host not in {None, "model"}:
            raise ValueError("模型配置中的 model.host 必须为空或为 model")
        return self


class DeploymentRecipe(StrictModel):
    version: Literal[2]
    deployment_id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    name: str = Field(min_length=1)
    hosts: dict[str, RecipeHost]
    model: ModelService
    tunnel: TunnelSpec
    robot: RobotDeployment
    runtime: RuntimePolicy = Field(default_factory=RuntimePolicy)

    @model_validator(mode="after")
    def validate_references(self) -> "DeploymentRecipe":
        model_host = self.model.host
        if not model_host:
            raise ValueError("model.host 必须配置")
        references = {
            "model.host": model_host,
            "tunnel.source_host": self.tunnel.source_host,
            "tunnel.destination_host": self.tunnel.destination_host,
            "robot.host": self.robot.host,
        }
        missing = [f"{field}={host}" for field, host in references.items() if host not in self.hosts]
        if missing:
            raise ValueError("引用了未定义主机：" + ", ".join(missing))
        if self.tunnel.source_host != self.robot.host:
            raise ValueError("tunnel.source_host 必须与 robot.host 相同")
        if self.tunnel.destination_host != model_host:
            raise ValueError("tunnel.destination_host 必须与 model.host 相同")
        if self.robot.client.host not in {None, self.robot.host}:
            raise ValueError("robot.client.host 必须为空或与 robot.host 相同")
        if self.hosts[self.robot.host].connection != "ssh":
            raise ValueError("本体主机当前必须使用 SSH 连接")
        if self.robot.client.builtin == "ros2_standard" and self.robot.ros.version != 2:
            raise ValueError("内置 ros2_standard Client 只能用于 ROS2")
        return self


def parse_robot_config(raw: dict[str, Any]) -> RobotConfig:
    return RobotConfig.model_validate(raw)


def parse_model_config(raw: dict[str, Any]) -> ModelConfig:
    return ModelConfig.model_validate(raw)


def parse_deployment_config(raw: dict[str, Any]) -> RobotConfig | ModelConfig:
    kind = raw.get("kind") if isinstance(raw, dict) else None
    if kind == "robot":
        return parse_robot_config(raw)
    if kind == "model":
        return parse_model_config(raw)
    raise ValueError("部署组件配置 kind 必须是 robot 或 model")


def compose_recipe(
    robot_config: RobotConfig | dict[str, Any],
    model_config: ModelConfig | dict[str, Any],
    *,
    deployment_id: str | None = None,
    name: str | None = None,
    runtime: RuntimePolicy | dict[str, Any] | None = None,
) -> DeploymentRecipe:
    """Compose independently managed robot/model configs into a Recipe."""

    robot = robot_config if isinstance(robot_config, RobotConfig) else parse_robot_config(robot_config)
    model = model_config if isinstance(model_config, ModelConfig) else parse_model_config(model_config)
    resolved_id = deployment_id or _bounded_config_id(f"{robot.config_id}--{model.config_id}")
    resolved_runtime = robot.runtime if runtime is None else RuntimePolicy.model_validate(runtime)

    model_service = model.model.model_copy(update={"host": "model"})
    robot_client = robot.robot.client.model_copy(update={"host": "robot"})
    robot_deployment = RobotDeployment(
        host="robot",
        **robot.robot.model_dump(mode="python", exclude={"client"}),
        client=robot_client,
    )
    tunnel = TunnelSpec(
        source_host="robot",
        destination_host="model",
        remote_bind=model.endpoint.bind,
        remote_port=model.endpoint.port,
        **robot.tunnel.model_dump(mode="python"),
    )
    return DeploymentRecipe(
        version=2,
        deployment_id=resolved_id,
        name=name or f"{robot.name} + {model.name}",
        hosts={"robot": robot.host, "model": model.host},
        model=model_service,
        tunnel=tunnel,
        robot=robot_deployment,
        runtime=resolved_runtime,
    )


def split_recipe(
    recipe: DeploymentRecipe | dict[str, Any],
    *,
    robot_config_id: str | None = None,
    model_config_id: str | None = None,
) -> tuple[RobotConfig, ModelConfig]:
    """Convert a full Recipe into reusable component configs."""

    value = recipe if isinstance(recipe, DeploymentRecipe) else parse_recipe(recipe)
    robot_body = RobotBody.model_validate(value.robot.model_dump(mode="python", exclude={"host"}))
    robot = RobotConfig(
        config_id=robot_config_id or _bounded_config_id(f"{value.deployment_id}-robot"),
        name=f"{value.name} · Robot",
        host=value.hosts[value.robot.host],
        robot=robot_body,
        tunnel=RobotTunnelConfig.model_validate(
            value.tunnel.model_dump(
                mode="python",
                exclude={"source_host", "destination_host", "remote_bind", "remote_port"},
            )
        ),
        runtime=value.runtime,
    )
    model = ModelConfig(
        config_id=model_config_id or _bounded_config_id(f"{value.deployment_id}-model"),
        name=f"{value.name} · Model",
        host=value.hosts[value.model.host or ""],
        model=value.model.model_copy(update={"host": None}),
        endpoint=ModelEndpoint(bind=value.tunnel.remote_bind, port=value.tunnel.remote_port),
    )
    return robot, model


def _bounded_config_id(value: str) -> str:
    if len(value) <= 64:
        return value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:10]
    return f"{value[:53]}-{digest}"


def parse_recipe(raw: dict[str, Any]) -> DeploymentRecipe:
    return DeploymentRecipe.model_validate(raw)


def load_recipe(path: str | Path) -> DeploymentRecipe:
    import json

    recipe_path = Path(path).expanduser().resolve()
    if recipe_path.suffix.lower() != ".json":
        raise ValueError("当前版本的部署 Recipe 仅支持 .json")
    raw = json.loads(recipe_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("部署 Recipe 根节点必须是对象")
    return parse_recipe(raw)


def load_deployment_config(path: str | Path) -> RobotConfig | ModelConfig:
    import json

    config_path = Path(path).expanduser().resolve()
    if config_path.suffix.lower() != ".json":
        raise ValueError("当前版本的部署组件配置仅支持 .json")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("部署组件配置根节点必须是对象")
    return parse_deployment_config(raw)


def redact_recipe(raw: dict[str, Any]) -> dict[str, Any]:
    """Return a JSON-safe copy suitable for logs and session records."""
    import copy

    value = copy.deepcopy(raw)
    for host in (value.get("hosts") or {}).values():
        auth = host.get("auth") if isinstance(host, dict) else None
        if isinstance(auth, dict) and auth.get("password"):
            auth["password"] = "********"
    standalone_auth = value.get("host", {}).get("auth") if isinstance(value.get("host"), dict) else None
    if isinstance(standalone_auth, dict) and standalone_auth.get("password"):
        standalone_auth["password"] = "********"
    _redact_environment_values(value)
    return value


def re_fullmatch_environment_name(value: str) -> bool:
    import re

    return re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", value) is not None


def _redact_environment_values(value: Any) -> None:
    if isinstance(value, dict):
        for container_name in ("environment", "load_kwargs", "predict_kwargs"):
            container = value.get(container_name)
            if not isinstance(container, dict):
                continue
            for key in list(container):
                upper = key.upper()
                if any(marker in upper for marker in ("PASSWORD", "TOKEN", "SECRET", "API_KEY", "PRIVATE_KEY")):
                    container[key] = "********"
        for item in value.values():
            _redact_environment_values(item)
    elif isinstance(value, list):
        for item in value:
            _redact_environment_values(item)
