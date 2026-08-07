"""Recipe deployment orchestration.

Long-running model, tunnel, ROS and robot-client processes are supervised by
systemd on their target hosts. The model target may be the Embodit machine
itself; the robot client still owns the real-time observation/action loop and
safety checks.
"""

from __future__ import annotations

import json
import math
import os
import pwd
import re
import secrets
import shlex
import threading
import time
import uuid
from enum import Enum
from pathlib import Path
from typing import Any, Callable

from .recipe import (
    CommandSpec,
    DeploymentRecipe,
    HealthCheck,
    InitialPose,
    RecipeHost,
    RobotOperation,
    RosRuntime,
    parse_recipe,
    redact_recipe,
)
from .transport import CommandRunner, LocalCommandRunner, RecipeSshRunner, RemoteResult, require_remote_ok


class OrchestrationState(str, Enum):
    PENDING = "pending"
    STARTING = "starting"
    MODEL_READY = "model_ready"
    DRY_RUN = "dry_run"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAULT = "fault"


class StopRequested(RuntimeError):
    pass


class RemoteServiceManager:
    def __init__(self, runner: CommandRunner, host: RecipeHost, deployment_id: str):
        self.runner = runner
        self.host = host
        self.deployment_id = deployment_id
        self._home: str | None = None

    @property
    def systemctl(self) -> list[str]:
        return ["systemctl"] if self.host.service_manager == "system" else ["systemctl", "--user"]

    @property
    def systemd_run(self) -> list[str]:
        return ["systemd-run"] if self.host.service_manager == "system" else ["systemd-run", "--user"]

    def home(self) -> str:
        if self._home is None:
            result = require_remote_ok(
                self.runner.run(["python3", "-c", "from pathlib import Path; print(Path.home())"]),
                "读取目标主机 HOME",
            )
            self._home = result.stdout.strip()
            if not self._home.startswith("/"):
                raise RuntimeError("目标主机 HOME 不是绝对路径")
        return self._home

    @property
    def deployment_dir(self) -> str:
        return f"{self.home()}/.embodit/deployments/{self.deployment_id}"

    def write_file(self, path: str, payload: bytes, mode: int = 0o700) -> None:
        writer = (
            "import os,sys; p=sys.argv[1]; data=sys.stdin.buffer.read(); "
            "os.makedirs(os.path.dirname(p), exist_ok=True); open(p,'wb').write(data); "
            "os.chmod(p,int(sys.argv[2],8))"
        )
        require_remote_ok(
            self.runner.run(["python3", "-c", writer, path, f"{mode:o}"], input_data=payload),
            f"写入远端文件 {path}",
        )

    def unit_name(self, component: str) -> str:
        safe = re.sub(r"[^A-Za-z0-9_.@-]", "-", self.deployment_id)
        return f"embodit-{component}-{safe}.service"

    def start(self, component: str, spec: CommandSpec, *, environment: dict[str, str] | None = None) -> str:
        unit = self.unit_name(component)
        wrapper = f"{self.deployment_dir}/{component}.sh"
        script = self._wrapper(spec, environment or {})
        self.write_file(wrapper, script.encode("utf-8"), 0o700)
        self.stop(component, ignore_errors=True)
        restart = {"no": "no", "on-failure": "on-failure", "always": "always"}[spec.restart]
        command = [
            *self.systemd_run,
            f"--unit={unit}",
            "--collect",
            "--no-block",
            f"--property=Restart={restart}",
            "--property=RestartSec=2s",
            wrapper,
        ]
        require_remote_ok(self.runner.run(command), f"启动 {component}")
        return unit

    def start_argv(
        self,
        component: str,
        command: list[str],
        *,
        restart: str = "always",
    ) -> str:
        spec = CommandSpec(command=command, restart=restart)
        return self.start(component, spec)

    def stop(self, component: str, *, ignore_errors: bool = False) -> None:
        unit = self.unit_name(component)
        result = self.runner.run([*self.systemctl, "stop", unit], timeout=20)
        if result.returncode != 0 and not ignore_errors and "not loaded" not in result.stderr.lower():
            require_remote_ok(result, f"停止 {component}")
        self.runner.run([*self.systemctl, "reset-failed", unit], timeout=10)
        for _ in range(30):
            loaded = self.runner.run([*self.systemctl, "show", "-p", "LoadState", "--value", unit], timeout=5)
            if loaded.returncode != 0 or loaded.stdout.strip() in {"", "not-found"}:
                break
            time.sleep(0.1)

    def active(self, component: str) -> bool:
        result = self.runner.run([*self.systemctl, "is-active", self.unit_name(component)], timeout=10)
        return result.returncode == 0 and result.stdout.strip() == "active"

    def status(self, component: str) -> dict[str, Any]:
        """Return one diagnostic systemd snapshot without conflating probe errors with exits."""
        properties = (
            "LoadState,ActiveState,SubState,Result,ExecMainCode,ExecMainStatus,NRestarts"
        )
        result = self.runner.run(
            [*self.systemctl, "show", self.unit_name(component), f"--property={properties}"],
            timeout=10,
        )
        values: dict[str, Any] = {
            "probeOk": result.returncode == 0,
            "returnCode": result.returncode,
        }
        for line in result.stdout.splitlines():
            key, separator, value = line.partition("=")
            if separator:
                values[key] = value
        if result.returncode != 0:
            values["probeError"] = (result.stderr or result.stdout).strip()[-500:]
        return values

    def logs(self, component: str, lines: int = 100) -> dict[str, Any]:
        lines = max(1, min(int(lines), 1000))
        journal = ["journalctl"] if self.host.service_manager == "system" else ["journalctl", "--user"]
        result = self.runner.run(
            [*journal, "--no-pager", "-u", self.unit_name(component), "-n", str(lines), "-o", "short-iso"],
            timeout=15,
        )
        return {
            "component": component,
            "unit": self.unit_name(component),
            "lines": (result.stdout or result.stderr).splitlines(),
            "returnCode": result.returncode,
        }

    @staticmethod
    def _wrapper(spec: CommandSpec, extra_environment: dict[str, str]) -> str:
        lines = ["#!/usr/bin/env bash", "set -eo pipefail"]
        lines.extend(f"source {shlex.quote(path)}" for path in spec.setup)
        lines.append("set -u")
        environment = {**spec.environment, **extra_environment}
        lines.extend(f"export {key}={shlex.quote(value)}" for key, value in environment.items())
        if spec.workdir:
            lines.append(f"cd {shlex.quote(spec.workdir)}")
        lines.append("exec " + shlex.join(spec.command))
        return "\n".join(lines) + "\n"


class DeploymentOrchestration:
    COMPONENTS = ("model", "tunnel", "ros", "client")

    def __init__(
        self,
        recipe: DeploymentRecipe,
        root: Path,
        *,
        runner_factory: Callable[[str, RecipeHost], CommandRunner] | None = None,
    ):
        self.id = uuid.uuid4().hex
        self.recipe = recipe
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.state = OrchestrationState.PENDING
        self.mode = recipe.runtime.default_mode
        self.current_step: str | None = None
        self.last_error: str | None = None
        self.created_ns = time.time_ns()
        self.updated_ns = self.created_ns
        self.events: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.components = {name: {"active": False, "unit": None, "host": None} for name in self.COMPONENTS}
        self.model_io: dict[str, Any] | None = None
        self.trajectory_history: dict[str, Any] | None = None
        self.runtime_timing: dict[str, Any] | None = None
        self.client_runtime: dict[str, Any] | None = None
        self.scheduler_status: dict[str, Any] | None = None
        self.dry_run_safety: dict[str, Any] | None = None
        self.recorded_poses: list[dict[str, Any]] = []
        self._lock = threading.RLock()
        self._stop_requested = threading.Event()
        self._monitor_stop = threading.Event()
        self._maintenance = threading.Event()
        self._thread: threading.Thread | None = None
        self._arm_token: str | None = None
        self._arm_expires_ns = 0
        self._runners: dict[str, CommandRunner] = {}
        self._managers: dict[str, RemoteServiceManager] = {}
        self._robot_home: str | None = None
        self._model_home: str | None = None
        self._tunnel_key: str | None = None
        self._tunnel_known_hosts: str | None = None
        records = self.root / "runs"
        records.mkdir(parents=True, exist_ok=True)
        records.chmod(0o700)
        self._record_path = records / f"{self.id}.jsonl"
        self._append_record(
            {
                "kind": "manifest",
                "orchestrationId": self.id,
                "recipe": redact_recipe(recipe.model_dump(mode="json")),
            }
        )
        factory = runner_factory or self._default_runner
        for name, host in recipe.hosts.items():
            runner = factory(name, host)
            self._runners[name] = runner
            self._managers[name] = RemoteServiceManager(runner, host, recipe.deployment_id)
        self._record("created", mode=self.mode)

    def _default_runner(self, name: str, host: RecipeHost) -> CommandRunner:
        if host.connection == "local":
            return LocalCommandRunner()
        return RecipeSshRunner(
            host,
            self.root / "known_hosts" / name,
            self.root / "askpass",
        )

    @property
    def model_host_name(self) -> str:
        assert self.recipe.model.host is not None
        return self.recipe.model.host

    @property
    def robot_host_name(self) -> str:
        return self.recipe.robot.host

    @property
    def model_runner(self) -> CommandRunner:
        return self._runners[self.model_host_name]

    @property
    def robot_runner(self) -> CommandRunner:
        return self._runners[self.robot_host_name]

    @property
    def model_manager(self) -> RemoteServiceManager:
        return self._managers[self.model_host_name]

    @property
    def robot_manager(self) -> RemoteServiceManager:
        return self._managers[self.robot_host_name]

    def start(self, *, task_prompt: str | None = None) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            if self.state not in {
                OrchestrationState.PENDING,
                OrchestrationState.MODEL_READY,
                OrchestrationState.STOPPED,
                OrchestrationState.FAULT,
            }:
                raise ValueError(f"当前状态不能启动：{self.state.value}")
            resume_after_model = self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]
            if task_prompt is not None:
                self._set_task_prompt(task_prompt)
            self._stop_requested.clear()
            self._monitor_stop.clear()
            self.last_error = None
            self.state = OrchestrationState.STARTING
            target = self._run_after_model if resume_after_model else self._run
            self._thread = threading.Thread(target=target, daemon=True, name=f"recipe-{self.id[:8]}")
            self._thread.start()
            self._record("dry_run_requested", modelPrepared=resume_after_model)
            return self.snapshot()

    def _set_task_prompt(self, task_prompt: str) -> str:
        normalized_prompt = task_prompt.strip()
        if not normalized_prompt:
            raise ValueError("任务 Prompt 不能为空")
        if len(normalized_prompt) > 2000:
            raise ValueError("任务 Prompt 不能超过 2000 个字符")
        client_config = dict(self.recipe.robot.client.config or {})
        client_config["task_prompt"] = normalized_prompt
        self.recipe.robot.client.config = client_config
        return normalized_prompt

    def start_evaluation(self, *, task_prompt: str) -> dict[str, Any]:
        """Start or resume real evaluation while keeping the resident model untouched."""
        with self._lock:
            prompt = self._set_task_prompt(task_prompt)
            if self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]:
                self._stop_requested.clear()
                self._monitor_stop.clear()
                self.last_error = None
                self.mode = "live"
                self.state = OrchestrationState.STARTING
                self._thread = threading.Thread(
                    target=self._run_after_model,
                    daemon=True,
                    name=f"evaluation-{self.id[:8]}",
                )
                self._thread.start()
                self._record("evaluation_requested", prompt=prompt, modelPrepared=True)
                return self.snapshot()
            if self.state != OrchestrationState.DRY_RUN:
                raise ValueError("只有模型就绪或已暂停的部署可以开始真机评测")
            self._maintenance.set()
            self._record("evaluation_resume_requested", prompt=prompt)
        try:
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = "live"
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.mode = "dry_run"
            self.last_error = f"恢复真机评测失败：{error}"
            if not self.components["client"]["active"]:
                try:
                    self._start_client()
                    self._wait_client_health()
                except Exception as recovery_error:  # noqa: BLE001
                    self._record("evaluation_resume_recovery_failed", reason=str(recovery_error))
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = OrchestrationState.RUNNING
            self._record("evaluation_resumed", prompt=prompt)
            return self.snapshot()

    def update_task_prompt(self, task_prompt: str) -> dict[str, Any]:
        """Switch prompts by restarting only the lightweight robot client."""
        with self._lock:
            current_config = self.recipe.robot.client.config or {}
            previous_prompt = str(current_config.get("task_prompt") or current_config.get("default_prompt") or "")
            prompt = self._set_task_prompt(task_prompt)
            if self.state == OrchestrationState.MODEL_READY:
                self._record("prompt_updated", prompt=prompt, clientRestarted=False)
                return self.snapshot()
            if self.state not in {OrchestrationState.DRY_RUN, OrchestrationState.RUNNING}:
                raise ValueError("当前部署状态不能切换 Prompt")
            was_running = self.state == OrchestrationState.RUNNING
            self._maintenance.set()
            self._record("prompt_switch_requested", prompt=prompt, running=was_running)
        try:
            if was_running:
                self._run_operation(self.recipe.robot.hold, "切换 Prompt 前 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self._set_task_prompt(previous_prompt or prompt)
            if not self.components["client"]["active"]:
                try:
                    self._start_client()
                    self._wait_client_health()
                except Exception as recovery_error:  # noqa: BLE001
                    self._record("prompt_switch_recovery_failed", reason=str(recovery_error))
            self.last_error = f"切换 Prompt 失败：{error}"
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self._record("prompt_updated", prompt=prompt, clientRestarted=True)
            return self.snapshot()

    def prepare_model(self) -> dict[str, Any]:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            if self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]:
                return self.snapshot()
            if self.state not in {OrchestrationState.PENDING, OrchestrationState.STOPPED, OrchestrationState.FAULT}:
                raise ValueError(f"当前状态不能单独启动模型：{self.state.value}")
            self._stop_requested.clear()
            self.last_error = None
            self.state = OrchestrationState.STARTING
            self._thread = threading.Thread(target=self._run_model_only, daemon=True, name=f"model-{self.id[:8]}")
            self._thread.start()
            self._record("model_prepare_requested")
            return self.snapshot()

    def _run_model_only(self) -> None:
        try:
            self._step("precheck", self._precheck)
            self._step("model", self._start_model)
            self._step("model_health", self._wait_model_health)
            with self._lock:
                self.state = OrchestrationState.MODEL_READY
                self.current_step = None
                self._record("model_ready")
        except StopRequested:
            self._record("model_prepare_cancelled")
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self.last_error = str(error)
                self.state = OrchestrationState.FAULT
                self._record("fault", reason=str(error))
            if self.recipe.runtime.auto_rollback:
                self._rollback(emergency=True)

    def _run(self) -> None:
        model_prepared = False
        try:
            self._step("precheck", self._precheck)
            self._step("tunnel_credentials", self._ensure_tunnel_credentials)
            self._step("model", self._start_model)
            self._step("model_health", self._wait_model_health)
            model_prepared = True
            self._run_after_model_steps(include_tunnel_credentials=False)
        except StopRequested:
            self._record("start_cancelled")
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self.last_error = str(error)
                self.state = OrchestrationState.FAULT
                self._record("fault", reason=str(error))
            if self.recipe.runtime.auto_rollback:
                self._rollback(emergency=True, preserve_model=model_prepared)
        finally:
            if self._stop_requested.is_set() and self.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}:
                self._rollback(emergency=False)

    def _run_after_model(self) -> None:
        try:
            self._run_after_model_steps()
        except StopRequested:
            self._record("start_cancelled")
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self.last_error = str(error)
                self.state = OrchestrationState.FAULT
                self._record("fault", reason=str(error))
            if self.recipe.runtime.auto_rollback:
                self._rollback(emergency=True, preserve_model=True)
        finally:
            if self._stop_requested.is_set() and self.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}:
                self._rollback(emergency=False)

    def _run_after_model_steps(self, *, include_tunnel_credentials: bool = True) -> None:
        if include_tunnel_credentials:
            self._step("tunnel_credentials", self._ensure_tunnel_credentials)
        self._step("tunnel", self._start_tunnel)
        self._step("tunnel_health", self._wait_tunnel_health)
        self._step("ros", self._start_ros)
        self._step("ros_readiness", self._wait_ros_readiness)
        self._step("power_on", lambda: self._run_operation(self.recipe.robot.power_on, "上电"))
        self.components["power"] = {"active": self.recipe.robot.power_on.type != "none", "unit": None, "host": self.robot_host_name}
        self._step("initial_pose", self._move_initial_pose)
        self._step("client", self._start_client)
        self._step("client_health", self._wait_client_health)
        with self._lock:
            self.state = OrchestrationState.DRY_RUN if self.mode == "dry_run" else OrchestrationState.RUNNING
            self.current_step = None
            self._record("deployment_ready", mode=self.mode)
        self._monitor()

    def _step(self, name: str, action: Callable[[], None]) -> None:
        if self._stop_requested.is_set():
            raise StopRequested()
        started = time.time_ns()
        with self._lock:
            self.current_step = name
            item = {"name": name, "status": "running", "startedNs": started}
            self.steps.append(item)
            self._record("step_started", step=name)
        try:
            action()
        except Exception as error:
            with self._lock:
                item.update({"status": "failed", "finishedNs": time.time_ns(), "error": str(error)})
                self._record("step_failed", step=name, reason=str(error))
            raise
        with self._lock:
            item.update({"status": "passed", "finishedNs": time.time_ns()})
            self._record("step_passed", step=name)

    def _precheck(self) -> None:
        probe = (
            "import json,platform,shutil; "
            "print(json.dumps({'hostname':platform.node(),'python':platform.python_version(),"
            "'systemctl':shutil.which('systemctl'),'systemd_run':shutil.which('systemd-run'),"
            "'ssh':shutil.which('ssh'),'ssh_keygen':shutil.which('ssh-keygen'),"
            "'ssh_keyscan':shutil.which('ssh-keyscan')}))"
        )
        for name, runner in self._runners.items():
            result = require_remote_ok(runner.run(["python3", "-c", probe]), f"探测主机 {name}")
            info = json.loads(result.stdout)
            if not info.get("systemctl") or not info.get("systemd_run"):
                raise RuntimeError(f"主机 {name} 缺少 systemctl/systemd-run")
            if name == self.robot_host_name and not all(
                info.get(key) for key in ("ssh", "ssh_keygen", "ssh_keyscan")
            ):
                raise RuntimeError("本体主机缺少 ssh/ssh-keygen/ssh-keyscan")
            host = self.recipe.hosts[name]
            if host.connection == "local":
                actual_user = pwd.getpwuid(os.geteuid()).pw_name
                if host.user != actual_user:
                    raise RuntimeError(
                        f"本地模型主机 user={host.user} 与 Embodit 运行用户 {actual_user} 不一致"
                    )
        self._model_home = self.model_manager.home()
        self._robot_home = self.robot_manager.home()

    def read_only_preflight(self) -> dict[str, Any]:
        """Probe deployment prerequisites without starting or changing services.

        The runtime startup path remains authoritative and repeats all safety-
        critical checks.  This method is intentionally limited to SSH/local
        command execution, filesystem inspection, systemd queries, and ROS
        graph reads when the graph is already available.
        """
        checks: list[dict[str, Any]] = []

        def add(code: str, status: str, message: str, **details: Any) -> None:
            checks.append(
                {
                    "code": code,
                    "status": status,
                    "message": message,
                    "details": details,
                }
            )

        add("recipe.schema", "pass", "Recipe schema 有效", version=self.recipe.version)

        probe = (
            "import json,os,platform,pwd,shutil; "
            "print(json.dumps({'hostname':platform.node(),'python':platform.python_version(),"
            "'user':pwd.getpwuid(os.geteuid()).pw_name,'home':os.path.expanduser('~'),"
            "'systemctl':shutil.which('systemctl'),'systemd_run':shutil.which('systemd-run'),"
            "'ssh':shutil.which('ssh'),'ssh_keygen':shutil.which('ssh-keygen'),"
            "'ssh_keyscan':shutil.which('ssh-keyscan')}))"
        )
        reachable: set[str] = set()
        for name, runner in self._runners.items():
            host = self.recipe.hosts[name]
            try:
                result = require_remote_ok(
                    runner.run(["python3", "-c", probe], timeout=host.connect_timeout_s + 5),
                    f"探测主机 {name}",
                )
                info = json.loads(result.stdout)
                missing = [key for key in ("systemctl", "systemd_run") if not info.get(key)]
                if name == self.robot_host_name:
                    missing.extend(
                        key for key in ("ssh", "ssh_keygen", "ssh_keyscan") if not info.get(key)
                    )
                if host.connection == "local" and info.get("user") != host.user:
                    raise RuntimeError(
                        f"配置用户 {host.user} 与 Embodit 运行用户 {info.get('user')} 不一致"
                    )
                if missing:
                    raise RuntimeError("缺少命令：" + ", ".join(missing))
                reachable.add(name)
                add(
                    f"host.{name}.connectivity",
                    "pass",
                    f"主机 {name} 可达",
                    connection=host.connection,
                    hostname=info.get("hostname"),
                    python=info.get("python"),
                    user=info.get("user"),
                )
            except Exception as error:  # noqa: BLE001
                add(
                    f"host.{name}.connectivity",
                    "fail",
                    f"主机 {name} 连接或基础命令检查失败",
                    error=str(error),
                )

        for name in sorted(reachable):
            manager = self._managers[name]
            try:
                command = [
                    *manager.systemctl,
                    "list-units",
                    "--type=service",
                    "--state=running",
                    "--no-legend",
                    "--no-pager",
                ]
                require_remote_ok(manager.runner.run(command, timeout=10), f"检查主机 {name} systemd")
                add(
                    f"host.{name}.systemd",
                    "pass",
                    f"主机 {name} 可读取 {self.recipe.hosts[name].service_manager} systemd manager",
                )
            except Exception as error:  # noqa: BLE001
                add(
                    f"host.{name}.systemd",
                    "fail",
                    f"主机 {name} 无法访问配置的 systemd manager",
                    error=str(error),
                )

        if self.model_host_name in reachable:
            model = self.recipe.model
            paths = {
                "workdir": model.workdir,
                "checkpoint": model.checkpoint,
                "source_path": model.source_path,
            }
            path_probe = (
                "import json,os,shutil,sys; p=json.loads(sys.argv[1]); exe=sys.argv[2]; "
                "r={k:{'path':v,'exists':(os.path.exists(os.path.expanduser(v)) if v else None)} "
                "for k,v in p.items()}; "
                "r['python_executable']={'path':exe,'exists':bool((os.path.isfile(os.path.expanduser(exe)) "
                "and os.access(os.path.expanduser(exe),os.X_OK)) if '/' in exe else shutil.which(exe))}; "
                "print(json.dumps(r))"
            )
            try:
                result = require_remote_ok(
                    self.model_runner.run(
                        [
                            "python3",
                            "-c",
                            path_probe,
                            json.dumps(paths),
                            model.python_executable,
                        ],
                        timeout=15,
                    ),
                    "检查模型环境路径",
                )
                details = json.loads(result.stdout)
                missing: list[str] = []
                for key in ("workdir", "checkpoint", "source_path", "python_executable"):
                    item = details.get(key) or {}
                    value = item.get("path")
                    if value and not item.get("exists"):
                        # Hub-style checkpoint identifiers are not local paths.
                        if key == "checkpoint" and not str(value).startswith(("/", "~", ".")):
                            continue
                        missing.append(f"{key}={value}")
                if missing:
                    raise RuntimeError("路径不存在或不可执行：" + ", ".join(missing))
                add(
                    "model.environment",
                    "pass",
                    "模型工作目录、Checkpoint/来源与 Python 环境可用",
                    paths=details,
                    provider=model.provider,
                )
            except Exception as error:  # noqa: BLE001
                add(
                    "model.environment",
                    "fail",
                    "模型环境检查失败",
                    error=str(error),
                    provider=model.provider,
                )

        if self.robot_host_name in reachable:
            ros = self.recipe.robot.ros
            cli = "ros2" if ros.version == 2 else "rosnode"
            try:
                result = self._run_robot_environment(
                    ["bash", "--noprofile", "--norc", "-c", f"command -v {cli}"],
                    setup=ros.setup,
                    environment=self._ros_environment(ros),
                    timeout=15,
                )
                require_remote_ok(result, "检查 ROS 环境")
                add(
                    "robot.ros.environment",
                    "pass",
                    f"ROS {ros.version} setup 与命令可用",
                    command=result.stdout.strip(),
                    setup=ros.setup,
                )
                try:
                    self._check_ros_graph()
                    self._check_topic_freshness()
                    self._check_topic_rates()
                    add(
                        "robot.ros.runtime",
                        "pass",
                        "当前 ROS graph、类型、频率与新鲜度符合 Recipe",
                    )
                except Exception as error:  # noqa: BLE001
                    add(
                        "robot.ros.runtime",
                        "warning",
                        "ROS Bringup 未运行或当前 graph/readiness 未满足；启动编排时会强制复检",
                        error=str(error),
                    )
            except Exception as error:  # noqa: BLE001
                add(
                    "robot.ros.environment",
                    "fail",
                    "ROS setup 或命令检查失败",
                    error=str(error),
                    setup=ros.setup,
                )

        counts = {
            status: sum(1 for item in checks if item["status"] == status)
            for status in ("pass", "warning", "fail")
        }
        return {
            "ok": counts["fail"] == 0,
            "deploymentId": self.recipe.deployment_id,
            "recipeVersion": self.recipe.version,
            "readOnly": True,
            "summary": {
                "passed": counts["pass"],
                "warnings": counts["warning"],
                "failed": counts["fail"],
            },
            "checks": checks,
            "note": "预检不会启动模型、ROS、本体或发送动作；启动阶段会重复全部强制 readiness 检查。",
        }

    def _ensure_tunnel_credentials(self) -> None:
        assert self._robot_home and self._model_home
        base = f"{self._robot_home}/.embodit/deployments/{self.recipe.deployment_id}"
        key_path = f"{base}/keys/model_tunnel"
        known_hosts = f"{base}/known_hosts"
        generator = """
import os, subprocess, sys
path = sys.argv[1]
os.makedirs(os.path.dirname(path), exist_ok=True)
if not os.path.exists(path):
    subprocess.run(['ssh-keygen','-q','-t','ed25519','-N','','-C',sys.argv[2],'-f',path], check=True)
os.chmod(path, 0o600)
print(open(path + '.pub').read().strip())
""".strip()
        result = require_remote_ok(
            self.robot_runner.run(
                ["python3", "-c", generator, key_path, f"embodit:{self.recipe.deployment_id}"],
                timeout=20,
            ),
            "生成本体隧道密钥",
        )
        public_key = result.stdout.strip()
        parts = public_key.split()
        if len(parts) < 2 or not parts[0].startswith("ssh-"):
            raise RuntimeError("本体生成了无效 SSH 公钥")
        restricted = (
            f'restrict,port-forwarding,permitopen="{self.recipe.tunnel.remote_bind}:{self.recipe.tunnel.remote_port}" '
            f"{parts[0]} {parts[1]} embodit:{self.recipe.deployment_id}"
        )
        installer = r"""
import os, sys
home, deployment = sys.argv[1:]
line = sys.stdin.read().strip()
ssh_dir = os.path.join(home, '.ssh'); os.makedirs(ssh_dir, mode=0o700, exist_ok=True)
path = os.path.join(ssh_dir, 'authorized_keys')
existing = open(path).read().splitlines() if os.path.exists(path) else []
marker = 'embodit:' + deployment
existing = [item for item in existing if marker not in item]
existing.append(line)
with open(path, 'w') as handle: handle.write('\n'.join(existing) + '\n')
os.chmod(path, 0o600)
""".strip()
        require_remote_ok(
            self.model_runner.run(
                ["python3", "-c", installer, self._model_home, self.recipe.deployment_id],
                input_data=restricted.encode("utf-8"),
            ),
            "安装本体到模型服务器的隧道公钥",
        )
        destination = self.recipe.hosts[self.model_host_name]
        scanner = r"""
import os, subprocess, sys
host, port, path = sys.argv[1:]
result = subprocess.run(['ssh-keyscan','-T','5','-p',port,host], capture_output=True, text=True, check=False)
lines = [line for line in result.stdout.splitlines() if line and not line.startswith('#')]
if not lines: raise SystemExit(result.stderr or 'ssh-keyscan returned no keys')
os.makedirs(os.path.dirname(path), exist_ok=True)
open(path,'w').write('\n'.join(lines) + '\n'); os.chmod(path,0o600)
""".strip()
        require_remote_ok(
            self.robot_runner.run(
                ["python3", "-c", scanner, destination.address, str(destination.port), known_hosts],
                timeout=12,
            ),
            "固定模型服务器 host key",
        )
        self._tunnel_key = key_path
        self._tunnel_known_hosts = known_hosts

    def _start_model(self) -> None:
        configured = self.recipe.model
        service = configured
        managed_providers = {
            "python": None,
            "openpi": "model_adapters:OpenPIAdapter",
            "lerobot": "model_adapters:LeRobotAdapter",
            "starvla": "model_adapters:StarVLAAdapter",
        }
        if configured.provider in managed_providers:
            remote_script = f"{self.model_manager.deployment_dir}/model_runner.py"
            remote_config = f"{self.model_manager.deployment_dir}/model_runner.json"
            assets = Path(__file__).with_name("assets")
            self.model_manager.write_file(remote_script, (assets / "model_runner.py").read_bytes(), 0o700)
            entrypoint = configured.entrypoint
            load_kwargs = dict(configured.load_kwargs)
            if configured.action_horizon is not None:
                load_kwargs["action_horizon"] = configured.action_horizon
            if configured.provider != "python":
                remote_adapters = f"{self.model_manager.deployment_dir}/model_adapters.py"
                self.model_manager.write_file(
                    remote_adapters,
                    (assets / "model_adapters.py").read_bytes(),
                    0o600,
                )
                entrypoint = managed_providers[configured.provider]
                source_suffix = {
                    "openpi": "third_party/models/openpi/src",
                    "lerobot": "third_party/models/lerobot/src",
                    "starvla": "third_party/models/starvla",
                }[configured.provider]
                default_source_path = f"{configured.workdir.rstrip('/')}/{source_suffix}"
                if configured.source_path:
                    load_kwargs["source_path"] = configured.source_path
                else:
                    load_kwargs.setdefault("source_path", default_source_path)
            runner_config = {
                "entrypoint": entrypoint,
                "checkpoint": configured.checkpoint,
                "load_method": configured.load_method,
                "predict_method": configured.predict_method,
                "load_kwargs": load_kwargs,
                "predict_kwargs": configured.predict_kwargs,
                "maximum_request_bytes": configured.maximum_request_bytes,
                "module_search_path": configured.workdir,
            }
            self.model_manager.write_file(
                remote_config,
                (json.dumps(runner_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                0o600,
            )
            service = configured.model_copy(
                update={
                    "command": [
                        configured.python_executable,
                        remote_script,
                        "--config",
                        remote_config,
                        "--host",
                        self.recipe.tunnel.remote_bind,
                        "--port",
                        str(self.recipe.tunnel.remote_port),
                    ],
                }
            )
        unit = self.model_manager.start("model", service)
        self.components["model"] = {"active": True, "unit": unit, "host": self.model_host_name}

    def _effective_model_health(self) -> HealthCheck | None:
        if self.recipe.model.provider in {"python", "openpi", "lerobot", "starvla"}:
            return HealthCheck(
                type="http",
                url=(
                    f"http://{self.recipe.tunnel.remote_bind}:"
                    f"{self.recipe.tunnel.remote_port}{self.recipe.tunnel.health_path}"
                ),
                startup_timeout_s=self.recipe.model.startup_timeout_s,
                interval_s=1,
            )
        return self.recipe.model.health

    def _wait_model_health(self) -> None:
        health = self._effective_model_health()
        if health is None:
            timeout = self.recipe.model.startup_timeout_s
            self._wait_service_active(self.model_manager, "model", timeout)
            return
        self._wait_health(self.model_runner, health, ros=None)

    def _start_tunnel(self) -> None:
        if not self._tunnel_key or not self._tunnel_known_hosts:
            raise RuntimeError("隧道凭据尚未准备")
        destination = self.recipe.hosts[self.model_host_name]
        tunnel = self.recipe.tunnel
        command = [
            "ssh",
            "-N",
            "-L",
            f"{tunnel.local_bind}:{tunnel.local_port}:{tunnel.remote_bind}:{tunnel.remote_port}",
            "-p",
            str(destination.port),
            "-i",
            self._tunnel_key,
            "-o",
            "BatchMode=yes",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "StrictHostKeyChecking=yes",
            "-o",
            f"UserKnownHostsFile={self._tunnel_known_hosts}",
            "-o",
            "GlobalKnownHostsFile=/dev/null",
            "-o",
            f"ServerAliveInterval={tunnel.server_alive_interval_s}",
            "-o",
            f"ServerAliveCountMax={tunnel.server_alive_count_max}",
            f"{destination.user}@{destination.address}",
        ]
        unit = self.robot_manager.start_argv("tunnel", command, restart=tunnel.restart)
        self.components["tunnel"] = {"active": True, "unit": unit, "host": self.robot_host_name}

    def _wait_tunnel_health(self) -> None:
        tunnel = self.recipe.tunnel
        health = HealthCheck(
            type="http",
            url=f"http://{tunnel.local_bind}:{tunnel.local_port}{tunnel.health_path}",
            startup_timeout_s=tunnel.startup_timeout_s,
            interval_s=0.5,
        )
        self._wait_health(self.robot_runner, health, ros=None)

    def _start_ros(self) -> None:
        bringup = self.recipe.robot.bringup.model_copy(
            update={
                "setup": [*self.recipe.robot.ros.setup, *self.recipe.robot.bringup.setup],
                "environment": {
                    **self._ros_environment(self.recipe.robot.ros),
                    **self.recipe.robot.bringup.environment,
                },
            }
        )
        unit = self.robot_manager.start("ros", bringup)
        self.components["ros"] = {"active": True, "unit": unit, "host": self.robot_host_name}

    def _wait_ros_readiness(self) -> None:
        readiness = self.recipe.robot.readiness
        deadline = time.monotonic() + readiness.timeout_s
        last_error = "ROS graph 尚未就绪"
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                self._check_ros_graph()
                self._check_topic_freshness()
                self._check_topic_rates()
                return
            except Exception as error:  # noqa: BLE001
                last_error = str(error)
                time.sleep(readiness.interval_s)
        raise TimeoutError(last_error)

    def _check_ros_graph(self) -> None:
        readiness = self.recipe.robot.readiness
        ros = self.recipe.robot.ros
        if ros.version == 2:
            nodes = require_remote_ok(self._run_ros(["ros2", "node", "list"]), "读取 ROS2 nodes").stdout.splitlines()
            missing_nodes = sorted(set(readiness.nodes) - {item.strip() for item in nodes})
            if missing_nodes:
                raise RuntimeError("缺少 ROS2 节点：" + ", ".join(missing_nodes))
            typed_commands = (
                (readiness.topics, ["ros2", "topic", "list", "-t"], "topic"),
                (readiness.services, ["ros2", "service", "list", "-t"], "service"),
                (readiness.actions, ["ros2", "action", "list", "-t"], "action"),
            )
            for requirements, command, label in typed_commands:
                if not requirements:
                    continue
                output = require_remote_ok(self._run_ros(command), f"读取 ROS2 {label}").stdout
                typed = _typed_names(output)
                for required in requirements:
                    actual = typed.get(required.name, set())
                    if required.type not in actual:
                        raise RuntimeError(f"ROS2 {label} {required.name} 缺失或类型不匹配")
        else:
            nodes = require_remote_ok(self._run_ros(["rosnode", "list"]), "读取 ROS1 nodes").stdout.splitlines()
            missing_nodes = sorted(set(readiness.nodes) - {item.strip() for item in nodes})
            if missing_nodes:
                raise RuntimeError("缺少 ROS1 节点：" + ", ".join(missing_nodes))
            for topic in readiness.topics:
                actual = require_remote_ok(
                    self._run_ros(["rostopic", "type", topic.name]),
                    f"读取 ROS1 topic {topic.name}",
                ).stdout.strip()
                if actual != topic.type:
                    raise RuntimeError(f"ROS1 topic {topic.name} 类型不匹配：{actual}")
            for service in readiness.services:
                actual = require_remote_ok(
                    self._run_ros(["rosservice", "type", service.name]),
                    f"读取 ROS1 service {service.name}",
                ).stdout.strip()
                if actual != service.type:
                    raise RuntimeError(f"ROS1 service {service.name} 类型不匹配：{actual}")
            if readiness.actions:
                raise ValueError("ROS1 readiness 当前不接受 action 声明，请检查对应 actionlib topics")

    def _check_topic_rates(self) -> None:
        ros = self.recipe.robot.ros
        for topic in self.recipe.robot.readiness.topics:
            if topic.minimum_rate_hz <= 0:
                continue
            if ros.version == 2:
                command = ["ros2", "topic", "hz", topic.name, "--window", "5"]
            else:
                command = ["rostopic", "hz", "-w", "5", topic.name]
            result = self._run_ros(
                [
                    "env", "PYTHONUNBUFFERED=1",
                    "timeout", "--signal=INT", f"{topic.sample_seconds:g}s",
                    *command,
                ],
                timeout=topic.sample_seconds + 3,
            )
            rate = _average_rate(result.stdout)
            if rate is None or rate < topic.minimum_rate_hz:
                raise RuntimeError(
                    f"{topic.name} 频率未达标：actual={rate}, minimum={topic.minimum_rate_hz}"
                )

    def _check_topic_freshness(self) -> None:
        ros = self.recipe.robot.ros
        for topic in self.recipe.robot.readiness.topics:
            if topic.maximum_age_ms is None:
                continue
            if ros.version == 2:
                script = """
import json, sys, time
import rclpy
from rclpy.qos import qos_profile_sensor_data
from rosidl_runtime_py.utilities import get_message
topic, type_name, maximum_age_ms = sys.argv[1], sys.argv[2], float(sys.argv[3])
rclpy.init(args=None); node = rclpy.create_node('embodit_topic_freshness'); received = {'value': None}
def callback(message): received['value'] = message
subscription = node.create_subscription(get_message(type_name), topic, callback, qos_profile_sensor_data)
deadline = time.monotonic() + max(1.0, maximum_age_ms / 1000.0 * 2)
while received['value'] is None and time.monotonic() < deadline: rclpy.spin_once(node, timeout_sec=0.1)
message = received['value']
if message is None: raise SystemExit('topic sample timeout')
header = getattr(message, 'header', None); stamp = getattr(header, 'stamp', None)
if stamp is None: raise SystemExit('configured maximum_age_ms but message has no header stamp')
stamp_ns = int(stamp.sec) * 1000000000 + int(stamp.nanosec)
age_ms = (node.get_clock().now().nanoseconds - stamp_ns) / 1000000
print(json.dumps({'topic': topic, 'ageMs': age_ms, 'maximumAgeMs': maximum_age_ms}))
node.destroy_node(); rclpy.shutdown()
if age_ms < -maximum_age_ms or age_ms > maximum_age_ms: raise SystemExit('topic sample is stale')
""".strip()
                result = self._run_ros(
                    ["python3", "-c", script, topic.name, topic.type, str(topic.maximum_age_ms)],
                    timeout=max(3, topic.maximum_age_ms / 1000 * 2 + 1),
                )
            else:
                result = self._run_ros(
                    ["rostopic", "echo", "-n", "1", topic.name],
                    timeout=max(3, topic.maximum_age_ms / 1000 * 2 + 1),
                )
            require_remote_ok(result, f"检查 {topic.name} 新鲜度")

    def _run_operation(self, operation: RobotOperation, label: str) -> None:
        if operation.type == "none":
            return
        if operation.type == "command":
            require_remote_ok(self._run_ros(operation.command, timeout=operation.timeout_s), label)
            return
        if operation.type == "ros2_service":
            command = [
                "ros2",
                "service",
                "call",
                operation.name or "",
                operation.service_type or "",
                json.dumps(operation.request, separators=(",", ":")),
            ]
        else:
            command = [
                "rosservice",
                "call",
                operation.name or "",
                json.dumps(operation.request, separators=(",", ":")),
            ]
        require_remote_ok(self._run_ros(command, timeout=operation.timeout_s), label)

    def _move_initial_pose(self) -> None:
        pose = self.recipe.robot.initial_pose
        if pose.type == "none":
            return
        if pose.type == "command":
            require_remote_ok(self._run_ros(pose.command, timeout=pose.timeout_s), "移动到初始位姿")
            return
        if self.recipe.robot.ros.version != 2:
            raise ValueError("follow_joint_trajectory 初始位姿当前仅支持 ROS2")
        total_ns = int(pose.duration_s * 1_000_000_000)
        goal = {
            "trajectory": {
                "joint_names": pose.joint_names,
                "points": [
                    {
                        "positions": pose.positions,
                        "time_from_start": {
                            "sec": total_ns // 1_000_000_000,
                            "nanosec": total_ns % 1_000_000_000,
                        },
                    }
                ],
            }
        }
        require_remote_ok(
            self._run_ros(
                [
                    "ros2",
                    "action",
                    "send_goal",
                    pose.action or "",
                    "control_msgs/action/FollowJointTrajectory",
                    json.dumps(goal, separators=(",", ":")),
                ],
                timeout=pose.timeout_s,
            ),
            "发送初始位姿",
        )
        self._verify_initial_pose(pose)

    def _verify_initial_pose(self, pose: InitialPose) -> None:
        script = """
import json, sys, time
import rclpy
from sensor_msgs.msg import JointState
topic, names, targets, tolerance, timeout = sys.argv[1], json.loads(sys.argv[2]), json.loads(sys.argv[3]), float(sys.argv[4]), float(sys.argv[5])
rclpy.init(args=None); node = rclpy.create_node('embodit_initial_pose_check'); result = {'message': None}
def receive(message): result['message'] = message
subscription = node.create_subscription(JointState, topic, receive, 10)
deadline = time.monotonic() + timeout
while result['message'] is None and time.monotonic() < deadline: rclpy.spin_once(node, timeout_sec=0.1)
message = result['message']
if message is None: raise SystemExit('joint state timeout')
values = dict(zip(message.name, message.position)); missing = [name for name in names if name not in values]
if missing: raise SystemExit('missing joints: ' + ','.join(missing))
errors = [abs(float(values[name]) - float(target)) for name, target in zip(names, targets)]
print(json.dumps({'ok': max(errors, default=0) <= tolerance, 'maxError': max(errors, default=0), 'errors': errors}))
node.destroy_node(); rclpy.shutdown()
if max(errors, default=0) > tolerance: raise SystemExit('initial pose tolerance exceeded')
""".strip()
        require_remote_ok(
            self._run_ros(
                [
                    "python3",
                    "-c",
                    script,
                    pose.joint_state_topic,
                    json.dumps(pose.joint_names),
                    json.dumps(pose.positions),
                    str(pose.tolerance),
                    str(min(pose.timeout_s, 10)),
                ],
                timeout=pose.timeout_s,
            ),
            "验证初始位姿",
        )

    def _start_client(self) -> None:
        configured = self.recipe.robot.client
        command = list(configured.command)
        if configured.builtin == "ros2_standard":
            remote_script = f"{self.robot_manager.deployment_dir}/ros2_robot_client.py"
            remote_config = f"{self.robot_manager.deployment_dir}/ros2_robot_client.json"
            source = Path(__file__).with_name("assets") / "ros2_robot_client.py"
            self.robot_manager.write_file(remote_script, source.read_bytes(), 0o700)
            client_config = json.loads(json.dumps(configured.config or {}))
            client_config["deployment_id"] = self.recipe.deployment_id
            client_config.setdefault("node_name", "vla_robot_client")
            client_config.setdefault(
                "model",
                {
                    "endpoint": f"http://{self.recipe.tunnel.local_bind}:{self.recipe.tunnel.local_port}",
                    "infer_path": "/infer",
                    "timeout_s": 10,
                },
            )
            self.robot_manager.write_file(
                remote_config,
                (json.dumps(client_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                0o600,
            )
            command = ["python3", remote_script, "--config", remote_config]
        elif configured.builtin == "python_adapter":
            remote_script = f"{self.robot_manager.deployment_dir}/python_robot_client.py"
            remote_config = f"{self.robot_manager.deployment_dir}/python_robot_client.json"
            status_path = f"{self.robot_manager.deployment_dir}/python_robot_client.status.json"
            source = Path(__file__).with_name("assets") / "python_robot_client.py"
            self.robot_manager.write_file(remote_script, source.read_bytes(), 0o700)
            client_config = json.loads(json.dumps(configured.config or {}))
            adapter_config = client_config.get("adapter", {})
            source_file = adapter_config.pop("source_file", None)
            if source_file:
                local_source = Path(source_file).expanduser().resolve()
                if not local_source.is_file():
                    raise ValueError(f"Python Robot Adapter source_file 不存在：{local_source}")
                remote_source_dir = f"{self.robot_manager.deployment_dir}/python_adapter"
                remote_source = f"{remote_source_dir}/{local_source.name}"
                self.robot_manager.write_file(remote_source, local_source.read_bytes(), 0o600)
                adapter_config["source_path"] = remote_source_dir
            client_config["deployment_id"] = self.recipe.deployment_id
            client_config["status_path"] = status_path
            client_config["model"] = {
                "endpoint": f"http://{self.recipe.tunnel.local_bind}:{self.recipe.tunnel.local_port}",
                "infer_path": "/infer",
                "timeout_s": min(600.0, float(configured.startup_timeout_s)),
                "maximum_response_bytes": 10_000_000,
            }
            self.robot_manager.write_file(status_path, b'{"status":"pending"}\n', 0o600)
            self.robot_manager.write_file(
                remote_config,
                (json.dumps(client_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                0o600,
            )
            python_executable = str(client_config.get("adapter", {}).get("python_executable", "python3"))
            command = [python_executable, remote_script, "--config", remote_config]
        client = configured.model_copy(
            update={
                "host": self.robot_host_name,
                "command": command,
                "restart": "no" if self.mode == "live" else self.recipe.robot.client.restart,
                "setup": [*self.recipe.robot.ros.setup, *self.recipe.robot.client.setup],
                "environment": {
                    **self._ros_environment(self.recipe.robot.ros),
                    **self.recipe.robot.client.environment,
                },
            }
        )
        unit = self.robot_manager.start(
            "client",
            client,
            environment={"EMBODIT_DEPLOYMENT_MODE": self.mode},
        )
        self.components["client"] = {"active": True, "unit": unit, "host": self.robot_host_name}

    def _wait_client_health(self) -> None:
        health = self.recipe.robot.client.health
        if health is None:
            self._wait_service_active(self.robot_manager, "client", self.recipe.robot.client.startup_timeout_s)
        else:
            self._wait_health(self.robot_runner, health, ros=self.recipe.robot.ros)
        if self.recipe.robot.client.builtin == "ros2_standard":
            self._wait_builtin_client_ready()
        elif self.recipe.robot.client.builtin == "python_adapter":
            self._wait_python_adapter_client_ready()

    def _wait_builtin_client_ready(self) -> None:
        config = self.recipe.robot.client.config or {}
        status_topic = str(config.get("status_topic", "/embodit/deployment_status"))
        timeout = float(self.recipe.robot.client.startup_timeout_s)
        script = """
import json, sys, time
import rclpy
from std_msgs.msg import String
topic, timeout = sys.argv[1], float(sys.argv[2]); rclpy.init(args=None)
node = rclpy.create_node('embodit_client_readiness'); state = {'value': None}
def receive(message):
    try: state['value'] = json.loads(message.data)
    except Exception: pass
subscription = node.create_subscription(String, topic, receive, 10)
deadline = time.monotonic() + timeout
while time.monotonic() < deadline:
    rclpy.spin_once(node, timeout_sec=0.1); value = state['value']
    if value and value.get('status') == 'ready': print(json.dumps(value)); break
    if value and value.get('status') == 'fault': raise SystemExit('robot client fault: ' + str(value.get('error')))
else: raise SystemExit('robot client did not report ready')
node.destroy_node(); rclpy.shutdown()
""".strip()
        require_remote_ok(
            self._run_ros(["python3", "-c", script, status_topic, str(timeout)], timeout=timeout + 3),
            "等待 Robot Client 首次完整推理",
        )

    def _wait_python_adapter_client_ready(self) -> None:
        timeout = float(self.recipe.robot.client.startup_timeout_s)
        status_path = f"{self.robot_manager.deployment_dir}/python_robot_client.status.json"
        script = r"""
import json, os, sys, time
path, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout; last = 'status 尚未生成'
while time.monotonic() < deadline:
    if os.path.exists(path):
        try:
            value = json.load(open(path)); last = json.dumps(value, ensure_ascii=False)
            if value.get('status') == 'ready': print(last); break
            if value.get('status') == 'fault':
                print(last); raise SystemExit('Python Robot Adapter fault: ' + str(value.get('error') or 'unknown error'))
            if value.get('status') == 'finished':
                print(last); raise SystemExit('Python Robot Adapter exited before readiness')
        except (OSError, ValueError) as error: last = str(error)
    time.sleep(0.2)
else: raise SystemExit('Python Robot Adapter readiness timeout: ' + last)
""".strip()
        result = self.robot_runner.run(
            ["python3", "-c", script, status_path, str(timeout)], timeout=timeout + 3
        )
        self._ingest_python_adapter_status(result.stdout)
        require_remote_ok(
            result,
            "等待 Python Robot Adapter 首次完整推理",
        )

    def _ingest_python_adapter_status(self, text: str) -> None:
        lines = [line for line in text.splitlines() if line.strip()]
        if not lines:
            return
        try:
            value = json.loads(lines[-1])
        except (TypeError, ValueError):
            return
        model_io = value.get("modelIo") if isinstance(value, dict) else None
        if isinstance(model_io, dict):
            with self._lock:
                self.model_io = model_io
        if isinstance(value, dict):
            with self._lock:
                if isinstance(value.get("trajectoryHistory"), dict):
                    self.trajectory_history = value["trajectoryHistory"]
                if isinstance(value.get("runtimeTiming"), dict):
                    self.runtime_timing = value["runtimeTiming"]
                if isinstance(value.get("scheduler"), dict):
                    self.scheduler_status = value["scheduler"]
                self.client_runtime = {
                    key: value.get(key)
                    for key in (
                        "status", "mode", "hardwareActive", "actionShape",
                        "inferenceLatencyMs", "safetyPassed", "safetyError",
                        "safetyRejections", "updatedMonotonicNs",
                    )
                    if key in value
                }
        if isinstance(value, dict) and value.get("mode") == "dry_run" and isinstance(
            value.get("safetyPassed"), bool
        ):
            with self._lock:
                self.dry_run_safety = {
                    "passed": value["safetyPassed"],
                    "error": value.get("safetyError"),
                    "rejections": int(value.get("safetyRejections") or 0),
                    "updatedMonotonicNs": value.get("updatedMonotonicNs"),
                }

    def _refresh_python_adapter_model_io(self) -> None:
        if self.recipe.robot.client.builtin != "python_adapter":
            return
        status_path = f"{self.robot_manager.deployment_dir}/python_robot_client.status.json"
        result = self.robot_runner.run(["cat", status_path], timeout=5)
        if result.returncode == 0:
            self._ingest_python_adapter_status(result.stdout)

    def _wait_service_active(self, manager: RemoteServiceManager, component: str, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            self._check_stop()
            if manager.active(component):
                return
            time.sleep(0.5)
        raise TimeoutError(f"{component} systemd 服务未就绪")

    def _wait_health(self, runner: CommandRunner, health: HealthCheck, ros: RosRuntime | None) -> None:
        deadline = time.monotonic() + health.startup_timeout_s
        last_error = "health 尚未就绪"
        while time.monotonic() < deadline:
            self._check_stop()
            try:
                if health.type == "http":
                    script = (
                        "import json,sys,urllib.request; "
                        "r=urllib.request.urlopen(sys.argv[1],timeout=3); "
                        "body=r.read(); text=body.decode(errors='replace'); print(text); "
                        "value=json.loads(text) if text.lstrip().startswith('{') else {}; "
                        "ready=value.get('ready',value.get('ok',True)); "
                        "raise SystemExit(0 if 200 <= r.status < 300 and ready is not False else 1)"
                    )
                    result = runner.run(["python3", "-c", script, health.url or ""], timeout=5)
                elif health.type == "tcp":
                    script = "import socket,sys; s=socket.create_connection((sys.argv[1],int(sys.argv[2])),3); s.close()"
                    result = runner.run(["python3", "-c", script, health.host, str(health.port)], timeout=5)
                elif health.type == "command":
                    result = runner.run(health.command, timeout=5)
                else:
                    if ros is None:
                        raise ValueError("ros_node health 缺少 ROS 环境")
                    result = self._run_ros(["ros2", "node", "list"] if ros.version == 2 else ["rosnode", "list"])
                    if result.returncode == 0 and health.name not in result.stdout.splitlines():
                        result = RemoteResult(1, result.stdout, f"missing node: {health.name}")
                if result.returncode == 0:
                    return
                last_error = (result.stderr or result.stdout).strip()[-1000:]
            except Exception as error:  # noqa: BLE001
                last_error = str(error)
            time.sleep(health.interval_s)
        raise TimeoutError(f"health 检查超时：{last_error}")

    def _run_ros(self, command: list[str], timeout: float = 15) -> RemoteResult:
        ros = self.recipe.robot.ros
        return self._run_robot_environment(
            command,
            setup=ros.setup,
            environment=self._ros_environment(ros),
            timeout=timeout,
        )

    def _run_robot_environment(
        self,
        command: list[str],
        *,
        setup: list[str],
        environment: dict[str, str],
        timeout: float,
    ) -> RemoteResult:
        bootstrap = (
            'set -eo pipefail; count="$1"; shift; '
            'for ((i=0; i<count; i++)); do source "$1" >&2; shift; done; set -u; '
            'env_count="$1"; shift; '
            'for ((i=0; i<env_count; i++)); do export "$1"; shift; done; exec "$@"'
        )
        exported = [f"{key}={value}" for key, value in environment.items()]
        args = [
            "bash",
            "--noprofile",
            "--norc",
            "-c",
            bootstrap,
            "embodit-ros",
            str(len(setup)),
            *setup,
            str(len(exported)),
            *exported,
            *command,
        ]
        return self.robot_runner.run(args, timeout=timeout)

    @staticmethod
    def _ros_environment(ros: RosRuntime) -> dict[str, str]:
        result: dict[str, str] = {}
        if ros.version == 2 and ros.domain_id is not None:
            result["ROS_DOMAIN_ID"] = str(ros.domain_id)
        if ros.version == 2 and ros.rmw_implementation:
            result["RMW_IMPLEMENTATION"] = ros.rmw_implementation
        if ros.version == 1 and ros.master_uri:
            result["ROS_MASTER_URI"] = ros.master_uri
        return result

    def _monitor(self) -> None:
        cycle = 0
        failures = {component: 0 for component in self.COMPONENTS}
        interval = float(self.recipe.runtime.monitor_interval_s)
        threshold = int(self.recipe.runtime.component_failure_threshold)
        while not self._stop_requested.is_set() and not self._monitor_stop.wait(interval):
            if self._maintenance.is_set():
                continue
            self._poll_managed_components(failures, threshold)
            self._refresh_python_adapter_model_io()
            cycle += 1
            if cycle % 5 == 0 and not self._maintenance.is_set():
                model_health = self._effective_model_health()
                if model_health is not None and self.components.get("model", {}).get("active"):
                    self._wait_health(
                        self.model_runner,
                        model_health.model_copy(update={"startup_timeout_s": 5.0, "interval_s": 0.5}),
                        ros=None,
                    )
                tunnel = self.recipe.tunnel
                if self.components.get("tunnel", {}).get("active"):
                    self._wait_health(
                        self.robot_runner,
                        HealthCheck(
                            type="http",
                            url=f"http://{tunnel.local_bind}:{tunnel.local_port}{tunnel.health_path}",
                            startup_timeout_s=5,
                            interval_s=0.5,
                        ),
                        ros=None,
                    )
                client_health = self.recipe.robot.client.health
                if client_health is not None and self.components.get("client", {}).get("active"):
                    self._wait_health(
                        self.robot_runner,
                        client_health.model_copy(update={"startup_timeout_s": 5.0, "interval_s": 0.5}),
                        ros=self.recipe.robot.ros,
                    )
            self.updated_ns = time.time_ns()

    def _poll_managed_components(self, failures: dict[str, int], threshold: int) -> None:
        for component in self.COMPONENTS:
            if self._maintenance.is_set() or self._stop_requested.is_set():
                break
            if (
                self.state == OrchestrationState.MODEL_READY
                and component != "model"
                and not self.components.get(component, {}).get("active")
            ):
                failures[component] = 0
                continue
            manager = self.model_manager if component == "model" else self.robot_manager
            observed = manager.status(component)
            with self._lock:
                self.components[component]["status"] = {
                    "probeOk": bool(observed.get("probeOk")),
                    "activeState": observed.get("ActiveState"),
                    "subState": observed.get("SubState"),
                    "result": observed.get("Result"),
                    "pid": observed.get("MainPID"),
                    "checkedNs": time.time_ns(),
                }
            healthy = observed.get("probeOk") and observed.get("ActiveState") == "active"
            if healthy:
                if failures[component]:
                    self._record(
                        "component_recovered",
                        component=component,
                        failedChecks=failures[component],
                    )
                failures[component] = 0
                continue

            failures[component] += 1
            if failures[component] == 1:
                self._record(
                    "component_unhealthy_observed",
                    component=component,
                    status=observed,
                    threshold=threshold,
                )
            if failures[component] < threshold:
                continue
            state = observed.get("ActiveState") or "unknown"
            substate = observed.get("SubState") or "unknown"
            result = observed.get("Result") or "unknown"
            if not observed.get("probeOk"):
                reason = observed.get("probeError") or f"probe rc={observed.get('returnCode')}"
                raise RuntimeError(
                    f"受管组件状态连续 {threshold} 次探测失败：{component}（{reason}）"
                )
            raise RuntimeError(
                f"受管组件持续异常：{component}（ActiveState={state}, "
                f"SubState={substate}, Result={result}, 连续 {threshold} 次）"
            )

    def arm_challenge(self) -> dict[str, Any]:
        with self._lock:
            if self.state != OrchestrationState.DRY_RUN or self.mode != "dry_run":
                raise ValueError("只有已就绪的 Dry Run 部署可以切换 Live")
            if self.dry_run_safety is not None and (
                not self.dry_run_safety.get("passed")
                or int(self.dry_run_safety.get("rejections") or 0) > 0
            ):
                raise ValueError(
                    "最近一次 Dry Run 动作校验未通过："
                    + str(self.dry_run_safety.get("error") or "未知安全错误")
                )
            self._arm_token = secrets.token_hex(3).upper()
            self._arm_expires_ns = time.monotonic_ns() + 60_000_000_000
            phrase = f"LIVE {self.recipe.deployment_id} {self._arm_token}"
            self._record("live_challenge_created")
            return {"phrase": phrase, "expiresInSeconds": 60}

    def promote_live(self, confirmation: str) -> dict[str, Any]:
        with self._lock:
            expected = f"LIVE {self.recipe.deployment_id} {self._arm_token or ''}"
            if time.monotonic_ns() > self._arm_expires_ns or not secrets.compare_digest(confirmation.strip(), expected):
                raise ValueError("Live 确认短语无效或已过期")
            if self.state != OrchestrationState.DRY_RUN:
                raise ValueError("部署当前不处于 Dry Run")
            if self.dry_run_safety is not None and (
                not self.dry_run_safety.get("passed")
                or int(self.dry_run_safety.get("rejections") or 0) > 0
            ):
                raise ValueError(
                    "最近一次 Dry Run 动作校验未通过："
                    + str(self.dry_run_safety.get("error") or "未知安全错误")
                )
            self._arm_token = None
            self._maintenance.set()
        try:
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = "live"
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.mode = "dry_run"
            self.last_error = f"切换 Live 失败：{error}"
            self.state = OrchestrationState.FAULT
            self._rollback(emergency=True, preserve_model=True)
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = OrchestrationState.RUNNING
            self._record("live_started")
            return self.snapshot()

    def stop_evaluation(self) -> dict[str, Any]:
        """Stop real actions while keeping the model stack and read-only inference alive."""
        with self._lock:
            if self.state != OrchestrationState.RUNNING or self.mode != "live":
                raise ValueError("只有正在运行的真机评测可以结束")
            self._maintenance.set()
            self._record("live_stop_requested")
        try:
            self._run_operation(self.recipe.robot.hold, "结束评测 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = "dry_run"
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.last_error = f"结束评测后恢复 Dry Run 失败：{error}"
            self.state = OrchestrationState.FAULT
            self._stop_requested.set()
            self._rollback(emergency=True, preserve_model=True)
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = OrchestrationState.DRY_RUN
            self._record("live_stopped", modelActive=self.components.get("model", {}).get("active", False))
            return self.snapshot()

    def record_pose(self, name: str | None = None) -> dict[str, Any]:
        """Capture exactly the state vector consumed by the model, and nothing else."""
        with self._lock:
            state = ((self.model_io or {}).get("input") or {}).get("state") or {}
            values = state.get("values")
            width = int((self.recipe.robot.client.config or {}).get("action", {}).get("width", 0))
            if not isinstance(values, list) or len(values) != width or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
                for value in values
            ):
                raise ValueError("尚未获得可记录的模型关节状态")
            normalized_name = (name or "").strip() or time.strftime("位姿 %H:%M:%S")
            if len(normalized_name) > 100:
                raise ValueError("位姿名称不能超过 100 个字符")
            pose = {
                "poseId": uuid.uuid4().hex[:12],
                "name": normalized_name,
                "values": [float(value) for value in values],
                "names": list(state.get("names") or []),
                "units": list(state.get("units") or []),
                "createdNs": time.time_ns(),
            }
            self.recorded_poses.append(pose)
            del self.recorded_poses[:-50]
            self._record("pose_recorded", poseId=pose["poseId"], name=pose["name"])
            return self.snapshot()

    def delete_pose(self, pose_id: str) -> dict[str, Any]:
        with self._lock:
            before = len(self.recorded_poses)
            self.recorded_poses = [pose for pose in self.recorded_poses if pose["poseId"] != pose_id]
            if len(self.recorded_poses) == before:
                raise ValueError("记录位姿不存在")
            self._record("pose_deleted", poseId=pose_id)
            return self.snapshot()

    def move_to_recorded_pose(self, pose_id: str, *, duration_s: float = 3.0) -> dict[str, Any]:
        """Pause evaluation, move through the generic adapter, then resume observation only."""
        if self.recipe.robot.client.builtin != "python_adapter":
            raise ValueError("一键回位当前需要使用通用 Python Adapter")
        with self._lock:
            if self.state not in {OrchestrationState.DRY_RUN, OrchestrationState.RUNNING}:
                raise ValueError("只有已连接本体并获得模型关节状态后才能一键回位")
            pose = next((item for item in self.recorded_poses if item["poseId"] == pose_id), None)
            if pose is None:
                raise ValueError("记录位姿不存在")
            was_running = self.state == OrchestrationState.RUNNING
            self._maintenance.set()
            self._record("pose_move_requested", poseId=pose_id, name=pose["name"], wasRunning=was_running)
        try:
            if was_running:
                self._run_operation(self.recipe.robot.hold, "回位前 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = "dry_run"

            deployment_dir = self.robot_manager.deployment_dir
            remote_script = f"{deployment_dir}/python_robot_client.py"
            remote_config = f"{deployment_dir}/python_robot_client.json"
            remote_pose = f"{deployment_dir}/recorded_pose_{pose_id}.json"
            self.robot_manager.write_file(
                remote_pose,
                (json.dumps({"values": pose["values"], "duration_s": duration_s}, ensure_ascii=False) + "\n").encode("utf-8"),
                0o600,
            )
            client_config = self.recipe.robot.client.config or {}
            python_executable = str(client_config.get("adapter", {}).get("python_executable", "python3"))
            result = self._run_robot_environment(
                [python_executable, remote_script, "--config", remote_config, "--move-pose", remote_pose],
                setup=[*self.recipe.robot.ros.setup, *self.recipe.robot.client.setup],
                environment={
                    **self._ros_environment(self.recipe.robot.ros),
                    **self.recipe.robot.client.environment,
                },
                timeout=max(30.0, float(duration_s) + 20.0),
            )
            require_remote_ok(result, f"移动到记录位姿 {pose['name']}")
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.last_error = f"一键回位失败：{error}"
            if not self.components["client"]["active"]:
                try:
                    self.mode = "dry_run"
                    self._start_client()
                    self._wait_client_health()
                except Exception:
                    pass
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = OrchestrationState.DRY_RUN
            self._record("pose_move_finished", poseId=pose_id, name=pose["name"])
            return self.snapshot()

    def _configured_scheduler_snapshot(self) -> dict[str, Any] | None:
        if self.recipe.robot.client.builtin != "python_adapter":
            return None
        config = self.recipe.robot.client.config or {}
        action = config.get("action") if isinstance(config.get("action"), dict) else {}
        control = config.get("control") if isinstance(config.get("control"), dict) else {}
        asynchronous = (
            control.get("asynchronous") if isinstance(control.get("asynchronous"), dict) else {}
        )
        horizon = int(action.get("horizon") or 1)
        action_steps = int(control.get("action_steps") or horizon)
        mode = str(control.get("inference_mode") or "synchronous")
        return {
            "mode": mode,
            "outputSteps": horizon,
            "actionSteps": action_steps,
            "requestAfterSteps": asynchronous.get("request_after_steps", "auto")
            if mode == "asynchronous"
            else None,
            "prefetchPolicy": (
                "auto" if asynchronous.get("request_after_steps", "auto") == "auto" else "fixed"
            )
            if mode == "asynchronous"
            else None,
            "latencyMarginMs": float(asynchronous.get("latency_margin_ms", 30))
            if mode == "asynchronous"
            else None,
        }

    def update_action_scheduler(
        self,
        *,
        mode: str,
        action_steps: int,
        request_after_steps: int | str = "auto",
        latency_margin_ms: float = 30,
    ) -> dict[str, Any]:
        """Apply a horizon-independent scheduler and restart only the thin client."""
        if self.recipe.robot.client.builtin != "python_adapter":
            raise ValueError("同步/异步调度当前仅适用于通用 Python Adapter Client")
        previous_config = json.loads(json.dumps(self.recipe.robot.client.config or {}))
        config = dict(previous_config)
        action = config.get("action") if isinstance(config.get("action"), dict) else {}
        horizon = int(action.get("horizon") or 1)
        if mode not in {"synchronous", "asynchronous"}:
            raise ValueError("推理模式必须是 synchronous 或 asynchronous")
        if isinstance(action_steps, bool) or not 1 <= int(action_steps) <= horizon:
            raise ValueError(f"执行步数必须在 1 到动作 horizon {horizon} 之间")
        if request_after_steps != "auto" and (
            isinstance(request_after_steps, bool)
            or not isinstance(request_after_steps, int)
            or not 1 <= request_after_steps < int(action_steps)
        ):
            raise ValueError("异步预取点必须是 auto 或位于 1 到 action_steps-1 的整数")
        if not math.isfinite(float(latency_margin_ms)) or float(latency_margin_ms) < 0:
            raise ValueError("异步延迟余量必须是非负有限数值")
        control = dict(config.get("control") or {})
        control.update(
            {
                "inference_mode": mode,
                "action_steps": int(action_steps),
                "asynchronous": {
                    **dict(control.get("asynchronous") or {}),
                    "request_after_steps": request_after_steps,
                    "latency_margin_ms": float(latency_margin_ms),
                },
            }
        )
        config["control"] = control
        with self._lock:
            if self.state not in {
                OrchestrationState.MODEL_READY,
                OrchestrationState.DRY_RUN,
                OrchestrationState.RUNNING,
            }:
                raise ValueError("当前部署状态不能切换推理调度")
            was_running = self.state == OrchestrationState.RUNNING
            self.recipe.robot.client.config = config
            self.scheduler_status = None
            self._record(
                "scheduler_update_requested",
                mode=mode,
                actionSteps=int(action_steps),
                requestAfterSteps=request_after_steps,
            )
            if self.state == OrchestrationState.MODEL_READY:
                self._record("scheduler_updated", clientRestarted=False)
                return self.snapshot()
            self._maintenance.set()
        try:
            if was_running:
                self._run_operation(self.recipe.robot.hold, "切换推理调度前 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.recipe.robot.client.config = previous_config
            self.scheduler_status = None
            if not self.components["client"]["active"]:
                try:
                    self._start_client()
                    self._wait_client_health()
                except Exception as recovery_error:  # noqa: BLE001
                    self._record("scheduler_update_recovery_failed", reason=str(recovery_error))
            self.last_error = f"切换推理调度失败：{error}"
            raise
        finally:
            self._maintenance.clear()
        self._record("scheduler_updated", clientRestarted=True)
        return self.snapshot()

    def disconnect_robot(self) -> dict[str, Any]:
        """Disconnect robot-side communication while keeping the model resident."""
        with self._lock:
            robot_linked = any(
                self.components.get(component, {}).get("active")
                for component in ("client", "ros", "tunnel")
            )
            if not robot_linked:
                raise ValueError("当前没有可断开的本体连接")
            was_running = self.state == OrchestrationState.RUNNING
            self._maintenance.set()
            self._monitor_stop.set()
            self._record("robot_disconnect_requested", running=was_running)
        errors: list[str] = []
        try:
            if was_running:
                try:
                    self._run_operation(self.recipe.robot.hold, "断开本体前 hold")
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
            for component in ("client", "ros", "tunnel"):
                if self.components.get(component, {}).get("active"):
                    try:
                        self.robot_manager.stop(component)
                    except Exception as error:  # noqa: BLE001
                        errors.append(f"{component}: {error}")
                    self.components[component]["active"] = False
        finally:
            self._maintenance.clear()
        with self._lock:
            self.mode = "dry_run"
            self.state = (
                OrchestrationState.MODEL_READY
                if self.components.get("model", {}).get("active")
                else OrchestrationState.STOPPED
            )
            self.last_error = "；".join(errors) if errors else None
            self._record("robot_disconnected", errors=errors, modelPreserved=True)
            return self.snapshot()

    def close_model(self) -> dict[str, Any]:
        """Stop robot communication and the resident model explicitly."""
        with self._lock:
            if not self.components.get("model", {}).get("active"):
                raise ValueError("模型当前未启动")
            self._maintenance.set()
            self._monitor_stop.set()
            self._stop_requested.set()
            was_running = self.state == OrchestrationState.RUNNING
            self._record("model_close_requested", running=was_running)
        errors: list[str] = []
        try:
            if was_running:
                try:
                    self._run_operation(self.recipe.robot.hold, "关闭模型前 hold")
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
            for component in ("client", "ros", "tunnel"):
                if self.components.get(component, {}).get("active"):
                    try:
                        self.robot_manager.stop(component)
                    except Exception as error:  # noqa: BLE001
                        errors.append(f"{component}: {error}")
                    self.components[component]["active"] = False
            try:
                self.model_manager.stop("model")
            except Exception as error:  # noqa: BLE001
                errors.append(f"model: {error}")
            self.components["model"]["active"] = False
        finally:
            self._maintenance.clear()
        with self._lock:
            self.mode = "dry_run"
            self.current_step = None
            self.state = OrchestrationState.FAULT if errors else OrchestrationState.STOPPED
            self.last_error = "；".join(errors) if errors else None
            self._record("model_closed", errors=errors)
            return self.snapshot()

    def restart_component(self, component: str) -> dict[str, Any]:
        if component not in self.COMPONENTS:
            raise ValueError(f"未知组件：{component}")
        with self._lock:
            if self.state not in {OrchestrationState.DRY_RUN, OrchestrationState.RUNNING}:
                raise ValueError("只有已运行的部署可以重启组件")
            self._maintenance.set()
            self._record("component_restart_requested", component=component)
        try:
            if component == "client":
                if self.mode == "live":
                    self._run_operation(self.recipe.robot.hold, "重启 Client 前 hold")
                self.robot_manager.stop("client")
                self.components["client"]["active"] = False
                self._start_client()
                self._wait_client_health()
            elif component == "tunnel":
                self.robot_manager.stop("tunnel")
                self.components["tunnel"]["active"] = False
                self._start_tunnel()
                self._wait_tunnel_health()
            elif component == "model":
                self.model_manager.stop("model")
                self.components["model"]["active"] = False
                self._start_model()
                self._wait_model_health()
                self._wait_tunnel_health()
            else:
                self._run_operation(self.recipe.robot.hold, "重启 ROS 前 hold")
                if self.components["client"]["active"]:
                    self.robot_manager.stop("client")
                    self.components["client"]["active"] = False
                self.robot_manager.stop("ros")
                self.components["ros"]["active"] = False
                self._start_ros()
                self._wait_ros_readiness()
                self._start_client()
                self._wait_client_health()
            self._record("component_restarted", component=component)
            return self.snapshot()
        except Exception as error:
            self.last_error = f"重启 {component} 失败：{error}"
            self.state = OrchestrationState.FAULT
            self._stop_requested.set()
            self._rollback(emergency=True, preserve_model=component != "model")
            raise
        finally:
            self._maintenance.clear()

    def stop(self, *, emergency: bool = False, wait_s: float = 30) -> dict[str, Any]:
        with self._lock:
            if self.state == OrchestrationState.STOPPED:
                return self.snapshot()
            self.state = OrchestrationState.STOPPING
            self._stop_requested.set()
            self._monitor_stop.set()
            self._record("emergency_stop_requested" if emergency else "stop_requested")
        if emergency:
            try:
                operation = self.recipe.robot.stop if self.recipe.robot.stop.type != "none" else self.recipe.robot.hold
                self._run_operation(operation, "本体急停")
            except Exception as error:  # noqa: BLE001
                self._record("emergency_operation_failed", reason=str(error))
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait_s)
        if self.state == OrchestrationState.STOPPING:
            self._rollback(emergency=emergency)
        return self.snapshot()

    def _rollback(self, *, emergency: bool, preserve_model: bool = False) -> None:
        with self._lock:
            self.state = OrchestrationState.STOPPING
            self.current_step = "rollback"
        errors: list[str] = []
        if self.components.get("client", {}).get("active"):
            if not emergency:
                try:
                    self._run_operation(self.recipe.robot.hold, "本体 hold")
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
            try:
                self.robot_manager.stop("client")
            except Exception as error:  # noqa: BLE001
                errors.append(str(error))
            self.components["client"]["active"] = False
        if self.recipe.runtime.power_off_on_exit and self.components.get("power", {}).get("active"):
            try:
                self._run_operation(self.recipe.robot.power_off, "本体下电")
            except Exception as error:  # noqa: BLE001
                errors.append(str(error))
            self.components["power"]["active"] = False
        for component in ("ros", "tunnel"):
            if self.components.get(component, {}).get("active"):
                try:
                    self.robot_manager.stop(component)
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
                self.components[component]["active"] = False
        if (
            not preserve_model
            and self.recipe.runtime.stop_model_on_exit
            and self.components.get("model", {}).get("active")
        ):
            try:
                self.model_manager.stop("model")
            except Exception as error:  # noqa: BLE001
                errors.append(str(error))
            self.components["model"]["active"] = False
        with self._lock:
            self.current_step = None
            if errors:
                self.last_error = (self.last_error + "；" if self.last_error else "") + "；".join(errors)
                self.state = OrchestrationState.FAULT
            elif preserve_model and self.components.get("model", {}).get("active"):
                self.state = OrchestrationState.MODEL_READY
            elif self.last_error and emergency:
                self.state = OrchestrationState.FAULT
            else:
                self.state = OrchestrationState.STOPPED
            self._record(
                "rollback_finished",
                errors=errors,
                emergency=emergency,
                modelPreserved=preserve_model and self.components.get("model", {}).get("active"),
            )

    def component_logs(self, component: str, lines: int = 100) -> dict[str, Any]:
        if component not in self.COMPONENTS:
            raise ValueError(f"未知组件：{component}")
        manager = self.model_manager if component == "model" else self.robot_manager
        return manager.logs(component, lines)

    def _check_stop(self) -> None:
        if self._stop_requested.is_set():
            raise StopRequested()

    def _record(self, event: str, **details: Any) -> None:
        with self._lock:
            item = {"timeNs": time.time_ns(), "event": event, "state": self.state.value, **details}
            self.events.append(item)
            del self.events[:-500]
            self.updated_ns = item["timeNs"]
            self._append_record({"kind": "event", **item})

    def _append_record(self, payload: dict[str, Any]) -> None:
        with self._record_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._record_path.chmod(0o600)

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "orchestrationId": self.id,
                "deploymentId": self.recipe.deployment_id,
                "name": self.recipe.name,
                "recipeVersion": 2,
                "state": self.state.value,
                "mode": self.mode,
                "currentStep": self.current_step,
                "lastError": self.last_error,
                "components": json.loads(json.dumps(self.components)),
                "modelIo": json.loads(json.dumps(self.model_io)) if self.model_io is not None else None,
                "trajectoryHistory": (
                    json.loads(json.dumps(self.trajectory_history))
                    if self.trajectory_history is not None
                    else None
                ),
                "runtimeTiming": (
                    json.loads(json.dumps(self.runtime_timing))
                    if self.runtime_timing is not None
                    else None
                ),
                "clientRuntime": (
                    json.loads(json.dumps(self.client_runtime))
                    if self.client_runtime is not None
                    else None
                ),
                "scheduler": (
                    json.loads(json.dumps(self.scheduler_status))
                    if self.scheduler_status is not None
                    else self._configured_scheduler_snapshot()
                ),
                "dryRunSafety": (
                    json.loads(json.dumps(self.dry_run_safety))
                    if self.dry_run_safety is not None
                    else None
                ),
                "recordedPoses": json.loads(json.dumps(self.recorded_poses)),
                "steps": list(self.steps[-100:]),
                "events": list(self.events[-100:]),
                "createdNs": self.created_ns,
                "updatedNs": self.updated_ns,
                "recordPath": str(self._record_path),
            }

    def manifest(self) -> dict[str, Any]:
        return {"orchestration": self.snapshot(), "recipe": redact_recipe(self.recipe.model_dump(mode="json"))}


class OrchestrationRegistry:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self._items: dict[str, DeploymentOrchestration] = {}
        self._lock = threading.RLock()

    def create(self, raw: dict[str, Any], *, mode: str | None = None) -> DeploymentOrchestration:
        recipe = parse_recipe(raw)
        if mode is not None:
            if mode not in {"dry_run", "live"}:
                raise ValueError("mode 必须是 dry_run 或 live")
            recipe.runtime.default_mode = mode
        item = DeploymentOrchestration(recipe, self.root / recipe.deployment_id)
        with self._lock:
            active = [
                existing
                for existing in self._items.values()
                if existing.recipe.deployment_id == recipe.deployment_id
                and existing.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}
            ]
            if active:
                raise ValueError(f"Deployment 已有活动编排：{active[0].id}")
            self._items[item.id] = item
        return item

    def get(self, orchestration_id: str) -> DeploymentOrchestration:
        with self._lock:
            try:
                return self._items[orchestration_id]
            except KeyError as error:
                raise KeyError(f"部署编排不存在：{orchestration_id}") from error

    def list(self) -> list[dict[str, Any]]:
        with self._lock:
            return [item.snapshot() for item in self._items.values()]

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._items.values())
        for item in items:
            if item.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}:
                try:
                    item.stop(emergency=True, wait_s=10)
                except Exception:  # noqa: BLE001
                    pass


def _typed_names(output: str) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {}
    for line in output.splitlines():
        match = re.match(r"^\s*(/\S+)\s+\[([^]]+)]\s*$", line)
        if match:
            result[match.group(1)] = {value.strip() for value in match.group(2).split(",")}
    return result


def _average_rate(output: str) -> float | None:
    matches = re.findall(r"average rate:\s*([0-9]+(?:\.[0-9]+)?)", output, flags=re.IGNORECASE)
    return float(matches[-1]) if matches else None
