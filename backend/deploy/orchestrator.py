"""Recipe deployment orchestration.

Long-running model, tunnel, ROS and robot-client processes are supervised by
systemd on their target hosts. Either target may be the Embodit machine itself;
the robot client still owns the real-time observation/action loop and safety
checks.
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
from concurrent.futures import ThreadPoolExecutor
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
from .recording import DeploymentVideoRecorder
from .transport import CommandRunner, LocalCommandRunner, RecipeSshRunner, RemoteResult, require_remote_ok


OFFLINE_INFERENCE_TIMEOUT_S = 30.0


class OrchestrationState(str, Enum):
    PENDING = "pending"
    STARTING = "starting"
    MODEL_READY = "model_ready"
    ROBOT_READY = "robot_ready"
    DRY_RUN = "dry_run"
    REPLAYING = "replaying"
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
            f"写入目标文件 {path}",
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

    def request_stop(self, component: str) -> None:
        """Queue a systemd stop without waiting for the unit to unload."""
        unit = self.unit_name(component)
        result = self.runner.run([*self.systemctl, "--no-block", "stop", unit], timeout=10)
        if result.returncode != 0 and "not loaded" not in (result.stderr or "").lower():
            require_remote_ok(result, f"请求停止 {component}")

    def stop_many(self, components: list[str], *, ignore_errors: bool = False) -> None:
        """Stop several managed units in one systemd/SSH round trip.

        ``systemctl stop`` returns only after all requested units are inactive.
        The per-unit unload wait in :meth:`stop` is useful before an immediate
        restart, but it only adds latency when explicitly disconnecting a stack.
        """
        normalized = list(dict.fromkeys(component for component in components if component))
        if not normalized:
            return
        units = [self.unit_name(component) for component in normalized]
        result = self.runner.run([*self.systemctl, "stop", *units], timeout=30)
        if result.returncode != 0 and not ignore_errors:
            output = (result.stderr or result.stdout).lower()
            if "not loaded" not in output and "not found" not in output:
                require_remote_ok(result, f"停止组件 {', '.join(normalized)}")

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
        recording_root: Path | None = None,
        pose_path: Path | None = None,
    ):
        self.id = uuid.uuid4().hex
        self.recipe = recipe
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.state = OrchestrationState.PENDING
        # Live is never a startup mode.  A Recipe may keep ``default_mode=live``
        # as a request for an interactive caller (for example the CLI), but the
        # orchestration itself always enters Dry Run first.  The only transition
        # to Live is ``promote_live`` after a short-lived arm challenge.
        self.mode = "dry_run"
        self.current_step: str | None = None
        self.last_error: str | None = None
        self.created_ns = time.time_ns()
        self.updated_ns = self.created_ns
        self.events: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []
        self.components = {name: {"active": False, "unit": None, "host": None} for name in self.COMPONENTS}
        self.model_io: dict[str, Any] | None = None
        self.live_preview: dict[str, Any] | None = None
        self.trajectory_history: dict[str, Any] | None = None
        self.runtime_timing: dict[str, Any] | None = None
        self.client_runtime: dict[str, Any] | None = None
        self.scheduler_status: dict[str, Any] | None = None
        self.dry_run_safety: dict[str, Any] | None = None
        self.recorded_poses: list[dict[str, Any]] = []
        self.pose_path = (pose_path or (self.root / "poses.json")).resolve()
        self._pose_store_warning: str | None = None
        self.recording_root = (recording_root or (self.root / "recordings")).resolve()
        self._recording: DeploymentVideoRecorder | None = None
        self._recording_segment = 0
        self._recording_status: dict[str, Any] = {
            "enabled": False,
            "status": "idle",
            "directory": str(self.recording_root),
            "path": None,
            "startedNs": None,
            "finishedNs": None,
            "frameRate": None,
            "cameraKey": None,
            "cameraLabel": None,
            "frames": 0,
            "droppedFrames": 0,
            "error": None,
        }
        self.hardware_replay: dict[str, Any] = {
            "status": "idle",
            "dataset": None,
            "episodeIndex": None,
            "startFrame": None,
            "endFrame": None,
            "fps": None,
            "totalFrames": 0,
            "framesApplied": 0,
            "error": None,
        }
        self._lock = threading.RLock()
        self._load_recorded_poses()
        self._stop_requested = threading.Event()
        self._monitor_stop = threading.Event()
        self._maintenance = threading.Event()
        self._hardware_replay_stop = threading.Event()
        self._offline_inference_active = False
        self._thread: threading.Thread | None = None
        self._operation_thread: threading.Thread | None = None
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
        if self._pose_store_warning:
            self._record("pose_store_load_failed", reason=self._pose_store_warning)

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
        monitor_thread: threading.Thread | None = None
        with self._lock:
            if self._offline_inference_active:
                raise ValueError("离线评测正在进行，不能启动本体链路")
            if self.state == OrchestrationState.FAULT and self._has_active_components_unlocked():
                raise ValueError("故障部署仍有活动组件，请先停止并清理后再启动")
            from_robot_observation = (
                self.state == OrchestrationState.ROBOT_READY
                and bool(self.components.get("client", {}).get("active"))
            )
            if from_robot_observation and not self.components.get("model", {}).get("active"):
                raise ValueError("请先启动模型，再开始 Dry Run")
            if self._thread is not None and self._thread.is_alive():
                if not from_robot_observation:
                    return self.snapshot()
                self._monitor_stop.set()
                monitor_thread = self._thread
            if self.state not in {
                OrchestrationState.PENDING,
                OrchestrationState.MODEL_READY,
                OrchestrationState.ROBOT_READY,
                OrchestrationState.STOPPED,
                OrchestrationState.FAULT,
            }:
                raise ValueError(f"当前状态不能启动：{self.state.value}")
            resume_after_model = self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]
            if task_prompt is not None:
                self._set_task_prompt(task_prompt)
        if monitor_thread is not None and monitor_thread is not threading.current_thread():
            monitor_thread.join(timeout=5)
            if monitor_thread.is_alive():
                raise RuntimeError("切换 Dry Run 前停止本体观测监控超时")
        with self._lock:
            self.mode = "dry_run"
            self._invalidate_arm_unlocked()
            self._stop_requested.clear()
            self._monitor_stop.clear()
            self.last_error = None
            self.state = OrchestrationState.STARTING
            target = (
                self._run_from_robot_observation
                if from_robot_observation
                else (self._run_after_model if resume_after_model else self._run)
            )
            self._thread = threading.Thread(target=target, daemon=True, name=f"recipe-{self.id[:8]}")
            self._thread.start()
            self._record(
                "dry_run_requested",
                modelPrepared=resume_after_model or from_robot_observation,
                robotObserved=from_robot_observation,
            )
            return self.snapshot()

    def connect_robot(self) -> dict[str, Any]:
        """Start a read-only robot observation link without the model or actions."""
        with self._lock:
            if self._offline_inference_active:
                raise ValueError("离线评测正在进行，不能连接本体")
            if self.state == OrchestrationState.FAULT and self._has_active_components_unlocked():
                raise ValueError("故障部署仍有活动组件，请先停止并清理后再连接本体")
            if (
                self.state == OrchestrationState.ROBOT_READY
                and self.components.get("client", {}).get("active")
            ):
                return self.snapshot()
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            if self.state not in {
                OrchestrationState.PENDING,
                OrchestrationState.MODEL_READY,
                OrchestrationState.STOPPED,
                OrchestrationState.FAULT,
            }:
                raise ValueError(f"当前状态不能连接本体：{self.state.value}")
            model_prepared = bool(self.components.get("model", {}).get("active"))
            self.mode = "observe"
            self._invalidate_arm_unlocked()
            self._stop_requested.clear()
            self._monitor_stop.clear()
            self.last_error = None
            self.state = OrchestrationState.STARTING
            self._thread = threading.Thread(
                target=self._run_robot_observation_only,
                args=(model_prepared,),
                daemon=True,
                name=f"robot-observe-{self.id[:8]}",
            )
            self._thread.start()
            self._record("robot_connect_requested", observationOnly=True)
            return self.snapshot()

    def _run_robot_observation_only(self, model_prepared: bool) -> None:
        try:
            self._step("robot_precheck", self._precheck_robot_only)
            if not self.components.get("ros", {}).get("active"):
                self._step("ros", self._start_ros)
                self._step("ros_readiness", self._wait_ros_readiness)
            self._step("client", self._start_client)
            self._step("client_health", self._wait_client_health)
            with self._lock:
                self.state = OrchestrationState.ROBOT_READY
                self.current_step = None
                self._record(
                    "robot_observation_ready",
                    modelActive=self.components.get("model", {}).get("active", False),
                )
            self._monitor()
        except StopRequested:
            self._record("robot_connect_cancelled")
        except Exception as error:  # noqa: BLE001
            with self._lock:
                self.last_error = str(error)
                self._record("robot_connect_failed", reason=str(error))
            if self.recipe.runtime.auto_rollback:
                self._rollback(emergency=True, preserve_model=model_prepared)

    def _stop_observation_client(self) -> None:
        if self.components.get("client", {}).get("active"):
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False

    def _run_from_robot_observation(self) -> None:
        """Upgrade an observation-only link to Dry Run while preserving ROS state."""
        try:
            self._step("observation_client_stop", self._stop_observation_client)
            self._step("tunnel_credentials", self._ensure_tunnel_credentials)
            self._step("tunnel", self._start_tunnel)
            self._step("tunnel_health", self._wait_tunnel_health)
            self._step("client", self._start_client)
            self._step("client_health", self._wait_client_health)
            with self._lock:
                self.state = OrchestrationState.DRY_RUN
                self.current_step = None
                self._record("deployment_ready", mode=self.mode, reusedRobotObservation=True)
            self._monitor()
        except StopRequested:
            self._record("start_cancelled")
        except Exception as error:  # noqa: BLE001
            failure = str(error)
            try:
                self._stop_observation_client()
                if self.components.get("tunnel", {}).get("active"):
                    self.robot_manager.stop("tunnel")
                    self.components["tunnel"]["active"] = False
                self.mode = "observe"
                self._start_client()
                self._wait_client_health()
                with self._lock:
                    self.last_error = f"启动 Dry Run 失败，已恢复本体只读观测：{failure}"
                    self.state = OrchestrationState.ROBOT_READY
                    self.current_step = None
                    self._record("dry_run_failed_observation_restored", reason=failure)
                self._monitor()
            except Exception as recovery_error:  # noqa: BLE001
                with self._lock:
                    self.last_error = f"启动 Dry Run 失败：{failure}；恢复本体观测失败：{recovery_error}"
                    self.state = OrchestrationState.FAULT
                    self._record("fault", reason=self.last_error)
                if self.recipe.runtime.auto_rollback:
                    self._rollback(emergency=True, preserve_model=True)

    def _set_task_prompt(self, task_prompt: str) -> str:
        normalized_prompt = task_prompt.strip()
        if not normalized_prompt:
            raise ValueError("任务 Prompt 不能为空")
        if len(normalized_prompt) > 2000:
            raise ValueError("任务 Prompt 不能超过 2000 个字符")
        client_config = dict(self.recipe.robot.client.config or {})
        client_config["task_prompt"] = normalized_prompt
        self.recipe.robot.client.config = client_config
        self._invalidate_arm_unlocked()
        return normalized_prompt

    def _invalidate_arm_unlocked(self) -> None:
        """Invalidate any confirmation issued for an earlier Dry Run state."""
        self._arm_token = None
        self._arm_expires_ns = 0

    def _write_python_adapter_runtime_control(self) -> None:
        if self.recipe.robot.client.builtin != "python_adapter":
            raise ValueError("运行时控制文件仅适用于通用 Python Adapter")
        config = self.recipe.robot.client.config or {}
        path = f"{self.robot_manager.deployment_dir}/python_robot_client.control.json"
        payload = {
            "task_prompt": str(config.get("task_prompt") or config.get("default_prompt") or ""),
            "updated_ns": time.time_ns(),
        }
        self.robot_manager.write_file(
            path,
            (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8"),
            0o600,
        )

    def _recover_client_switch(
        self,
        *,
        previous_state: OrchestrationState,
        previous_mode: str,
        previous_config: dict[str, Any],
        failure_label: str,
        failure: Exception,
    ) -> None:
        """Stop a failed replacement Client and restore its last known-good configuration."""
        def restore_previous_state() -> None:
            with self._lock:
                self.state = previous_state
                self.mode = previous_mode
                self.recipe.robot.client.config = json.loads(json.dumps(previous_config))

        stop_errors: list[str] = []
        try:
            self.robot_manager.stop("client")
        except Exception as stop_error:  # noqa: BLE001
            stop_errors.append(f"停止新 Client 失败：{stop_error}")
        self.components["client"]["active"] = False
        restore_previous_state()
        try:
            self._start_client()
            self._wait_client_health()
        except Exception as recovery_error:  # noqa: BLE001
            try:
                self.robot_manager.stop("client")
            except Exception as stop_error:  # noqa: BLE001
                stop_errors.append(f"停止恢复 Client 失败：{stop_error}")
            self.components["client"]["active"] = False
            details = "；".join(
                [
                    f"{failure_label}：{failure}",
                    f"恢复原 Client 失败：{recovery_error}",
                    *stop_errors,
                ]
            )
            self.last_error = details
            self._monitor_stop.set()
            monitor = self._thread
            if monitor is not None and monitor is not threading.current_thread():
                monitor.join(timeout=5)
            self._rollback(emergency=True, preserve_model=True)
            with self._lock:
                self.state = OrchestrationState.FAULT
                self.last_error = details
                self._record("client_switch_recovery_failed", reason=details)
            raise RuntimeError(details) from failure
        restore_previous_state()
        if stop_errors:
            self._record("client_switch_stop_warning", errors=stop_errors)
        self._record("client_switch_recovered", failure=failure_label)

    def start_evaluation(self, *, task_prompt: str) -> dict[str, Any]:
        """Backward-compatible evaluation entry point that can only reach Dry Run.

        Live activation intentionally remains a separate ``arm_challenge`` /
        ``promote_live`` operation.  Keeping this endpoint as a safe alias avoids
        turning older clients into an arming bypass.
        """
        with self._lock:
            if self._offline_inference_active:
                raise ValueError("离线评测正在进行，不能开始真机评测")
            if self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]:
                self._record("legacy_evaluation_downgraded", targetMode="dry_run")
                return self.start(task_prompt=task_prompt)
            if self.state == OrchestrationState.ROBOT_READY and self.components["model"]["active"]:
                self._record("legacy_evaluation_downgraded", targetMode="dry_run")
                return self.start(task_prompt=task_prompt)
            if self.state != OrchestrationState.DRY_RUN or self.mode != "dry_run":
                raise ValueError("只有模型就绪或 Dry Run 部署可以准备评测")
            self._record("legacy_evaluation_downgraded", targetMode="dry_run")
        return self.update_task_prompt(task_prompt)

    def update_task_prompt(self, task_prompt: str) -> dict[str, Any]:
        """Switch prompts through the adapter's runtime control channel."""
        with self._lock:
            previous_state = self.state
            previous_mode = self.mode
            previous_config = json.loads(json.dumps(self.recipe.robot.client.config or {}))
            prompt = self._set_task_prompt(task_prompt)
            if self.state == OrchestrationState.MODEL_READY:
                self._record("prompt_updated", prompt=prompt, clientRestarted=False)
                return self.snapshot()
            if self.state not in {
                OrchestrationState.ROBOT_READY,
                OrchestrationState.DRY_RUN,
                OrchestrationState.RUNNING,
            }:
                raise ValueError("当前部署状态不能切换 Prompt")
            was_running = self.state == OrchestrationState.RUNNING
            hot_reload = self.recipe.robot.client.builtin == "python_adapter"
            if hot_reload:
                self._record("prompt_switch_requested", prompt=prompt, running=was_running)
            else:
                self._maintenance.set()
                self._record("prompt_switch_requested", prompt=prompt, running=was_running)
        if hot_reload:
            try:
                self._write_python_adapter_runtime_control()
            except Exception as error:
                with self._lock:
                    self.recipe.robot.client.config = previous_config
                    self.last_error = f"切换 Prompt 失败：{error}"
                raise
            with self._lock:
                self._record("prompt_updated", prompt=prompt, clientRestarted=False, hotReloaded=True)
                return self.snapshot()
        with self._lock:
            self._maintenance.set()
        try:
            if was_running:
                self._run_operation(self.recipe.robot.hold, "切换 Prompt 前 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.last_error = f"切换 Prompt 失败：{error}"
            self._recover_client_switch(
                previous_state=previous_state,
                previous_mode=previous_mode,
                previous_config=previous_config,
                failure_label="切换 Prompt 失败",
                failure=error,
            )
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self._record("prompt_updated", prompt=prompt, clientRestarted=True)
            return self.snapshot()

    def prepare_model(self) -> dict[str, Any]:
        with self._lock:
            if self.state == OrchestrationState.FAULT and self._has_active_components_unlocked():
                raise ValueError("故障部署仍有活动组件，请先停止并清理后再准备模型")
            if self.state == OrchestrationState.ROBOT_READY:
                if self.components.get("model", {}).get("active"):
                    return self.snapshot()
                if self._operation_thread is not None and self._operation_thread.is_alive():
                    return self.snapshot()
                self._maintenance.set()
                self.last_error = None
                self.state = OrchestrationState.STARTING
                thread = threading.Thread(
                    target=self._run_model_alongside_robot,
                    daemon=True,
                    name=f"model-with-robot-{self.id[:8]}",
                )
                self._operation_thread = thread
                thread.start()
                self._record("model_prepare_requested", robotObserved=True)
                return self.snapshot()
            if self._thread is not None and self._thread.is_alive():
                return self.snapshot()
            if self.state == OrchestrationState.MODEL_READY and self.components["model"]["active"]:
                return self.snapshot()
            if self.state not in {OrchestrationState.PENDING, OrchestrationState.STOPPED, OrchestrationState.FAULT}:
                raise ValueError(f"当前状态不能单独启动模型：{self.state.value}")
            self.mode = "dry_run"
            self._invalidate_arm_unlocked()
            self._stop_requested.clear()
            self.last_error = None
            self.state = OrchestrationState.STARTING
            self._thread = threading.Thread(target=self._run_model_only, daemon=True, name=f"model-{self.id[:8]}")
            self._thread.start()
            self._record("model_prepare_requested")
            return self.snapshot()

    def _run_model_only(self) -> None:
        try:
            self._step("precheck", self._precheck_model_only)
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

    def _run_model_alongside_robot(self) -> None:
        """Prepare model service while the robot observation Client stays online."""
        try:
            self._step("precheck", self._precheck_model_only)
            self._step("model", self._start_model)
            self._step("model_health", self._wait_model_health)
            with self._lock:
                self.state = OrchestrationState.ROBOT_READY
                self.current_step = None
                self._record("model_ready", robotObserved=True)
        except StopRequested:
            with self._lock:
                self.state = OrchestrationState.ROBOT_READY
                self.current_step = None
                self._record("model_prepare_cancelled", robotObserved=True)
        except Exception as error:  # noqa: BLE001
            if self.components.get("model", {}).get("active"):
                try:
                    self.model_manager.stop("model")
                except Exception:  # noqa: BLE001
                    pass
                self.components["model"]["active"] = False
            with self._lock:
                self.last_error = str(error)
                self.state = OrchestrationState.ROBOT_READY
                self.current_step = None
                self._record("model_prepare_failed", reason=str(error), robotPreserved=True)
        finally:
            self._maintenance.clear()
            with self._lock:
                self._operation_thread = None

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
            if self._robot_home is None:
                self._step("robot_precheck", self._precheck_robot_only)
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
        if self.mode != "dry_run":
            raise RuntimeError("部署启动链路只能进入 Dry Run；Live 必须通过确认门控")
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

    def _precheck_hosts(self, names: tuple[str, ...]) -> None:
        probe = (
            "import json,platform,shutil; "
            "print(json.dumps({'hostname':platform.node(),'python':platform.python_version(),"
            "'systemctl':shutil.which('systemctl'),'systemd_run':shutil.which('systemd-run'),"
            "'ssh':shutil.which('ssh'),'ssh_keygen':shutil.which('ssh-keygen'),"
            "'ssh_keyscan':shutil.which('ssh-keyscan')}))"
        )
        for name in names:
            runner = self._runners[name]
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
                        f"本地主机 {name} user={host.user} 与 Embodit 运行用户 {actual_user} 不一致"
                    )
            if name == self.model_host_name:
                self._model_home = self.model_manager.home()
            if name == self.robot_host_name:
                self._robot_home = self.robot_manager.home()

    def _precheck(self) -> None:
        self._precheck_hosts(tuple(self._runners))

    def _precheck_model_only(self) -> None:
        self._precheck_hosts((self.model_host_name,))

    def _precheck_robot_only(self) -> None:
        self._precheck_hosts((self.robot_host_name,))

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
                self._check_topic_observability()
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

    def _run_topic_checks(self, check: Callable[[Any], None], topics: list[Any]) -> None:
        if len(topics) <= 1:
            for topic in topics:
                check(topic)
            return
        with ThreadPoolExecutor(
            max_workers=min(8, len(topics)),
            thread_name_prefix="embodit-readiness",
        ) as executor:
            futures = [executor.submit(check, topic) for topic in topics]
            for future in futures:
                future.result()

    def _check_topic_observability(self) -> None:
        topics = list(self.recipe.robot.readiness.topics)

        def check(topic: Any) -> None:
            self._check_single_topic_freshness(topic)
            self._check_single_topic_rate(topic)

        self._run_topic_checks(check, topics)

    def _check_topic_rates(self) -> None:
        self._run_topic_checks(self._check_single_topic_rate, list(self.recipe.robot.readiness.topics))

    def _check_single_topic_rate(self, topic: Any) -> None:
        ros = self.recipe.robot.ros
        if topic.minimum_rate_hz <= 0:
            return
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
        self._run_topic_checks(self._check_single_topic_freshness, list(self.recipe.robot.readiness.topics))

    def _check_single_topic_freshness(self, topic: Any) -> None:
        ros = self.recipe.robot.ros
        if topic.maximum_age_ms is None:
            return
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

    def _start_client(self, *, replay_payload: dict[str, Any] | None = None) -> None:
        configured = self.recipe.robot.client
        if replay_payload is not None and configured.builtin != "python_adapter":
            raise ValueError("真机 Replay 需要使用通用 Python Adapter")
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
            preview_status_path = f"{self.robot_manager.deployment_dir}/python_robot_client.preview.json"
            runtime_control_path = f"{self.robot_manager.deployment_dir}/python_robot_client.control.json"
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
            client_config["preview_status_path"] = preview_status_path
            client_config["runtime_control_path"] = runtime_control_path
            client_config["model"] = {
                "endpoint": f"http://{self.recipe.tunnel.local_bind}:{self.recipe.tunnel.local_port}",
                "infer_path": "/infer",
                "timeout_s": min(600.0, float(configured.startup_timeout_s)),
                "maximum_response_bytes": 10_000_000,
            }
            self.robot_manager.write_file(status_path, b'{"status":"pending"}\n', 0o600)
            self.robot_manager.write_file(preview_status_path, b'{"status":"pending"}\n', 0o600)
            self.robot_manager.write_file(
                runtime_control_path,
                (
                    json.dumps(
                        {"task_prompt": str(client_config.get("task_prompt") or "")},
                        ensure_ascii=False,
                    )
                    + "\n"
                ).encode("utf-8"),
                0o600,
            )
            with self._lock:
                self.live_preview = None
            self.robot_manager.write_file(
                remote_config,
                (json.dumps(client_config, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
                0o600,
            )
            python_executable = str(client_config.get("adapter", {}).get("python_executable", "python3"))
            command = [python_executable, remote_script, "--config", remote_config]
            if replay_payload is not None:
                remote_replay = f"{self.robot_manager.deployment_dir}/hardware_replay.json"
                self.robot_manager.write_file(
                    remote_replay,
                    (json.dumps(replay_payload, ensure_ascii=False) + "\n").encode("utf-8"),
                    0o600,
                )
                command.extend(["--replay-actions", remote_replay])
        client = configured.model_copy(
            update={
                "host": self.robot_host_name,
                "command": command,
                "restart": "no" if self.mode in {"live", "replay"} else self.recipe.robot.client.restart,
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
replay = sys.argv[3] == 'replay'
deadline = time.monotonic() + timeout; last = 'status 尚未生成'
while time.monotonic() < deadline:
    if os.path.exists(path):
        try:
            value = json.load(open(path)); last = json.dumps(value, ensure_ascii=False)
            status = value.get('status')
            if status == 'ready' or (replay and status in {'moving_to_start', 'replaying'}): print(last); break
            if status == 'fault':
                print(last); raise SystemExit('Python Robot Adapter fault: ' + str(value.get('error') or 'unknown error'))
            if status in {'finished', 'stopped'}:
                print(last)
                if replay: break
                raise SystemExit('Python Robot Adapter exited before readiness')
        except (OSError, ValueError) as error: last = str(error)
    time.sleep(0.2)
else: raise SystemExit('Python Robot Adapter readiness timeout: ' + last)
""".strip()
        result = self.robot_runner.run(
            [
                "python3", "-c", script, status_path, str(timeout),
                "replay" if self.mode == "replay" else "normal",
            ],
            timeout=timeout + 3,
        )
        self._ingest_python_adapter_status(result.stdout)
        require_remote_ok(
            result,
            "等待 Python Robot Adapter 观测/推理运行时就绪",
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
                        "status",
                        "mode",
                        "hardwareActive",
                        "actionShape",
                        "observationOnly",
                        "observationLatencyMs",
                        "inferenceLatencyMs",
                        "safetyPassed",
                        "safetyError",
                        "safetyRejections",
                        "updatedMonotonicNs",
                        "totalFrames",
                        "framesApplied",
                        "commandsSent",
                        "framesSkipped",
                        "fps",
                        "startFrame",
                        "timingDegraded",
                        "effectiveCommandHz",
                        "replayDurationS",
                    )
                    if key in value
                }
                if value.get("mode") == "replay":
                    replay_fields = (
                        "status", "totalFrames", "framesApplied", "commandsSent",
                        "framesSkipped", "timingDegraded", "effectiveCommandHz",
                        "replayDurationS", "fps", "startFrame", "error",
                    )
                    self.hardware_replay.update(
                        {
                            key: value.get(key)
                            for key in replay_fields
                            if key in value
                        }
                    )
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

    def _refresh_python_adapter_live_preview(self) -> None:
        if self.recipe.robot.client.builtin != "python_adapter":
            return
        preview_path = f"{self.robot_manager.deployment_dir}/python_robot_client.preview.json"
        result = self.robot_runner.run(["cat", preview_path], timeout=5)
        if result.returncode != 0:
            return
        try:
            value = json.loads(result.stdout)
        except json.JSONDecodeError:
            return
        if not isinstance(value, dict) or not isinstance(value.get("capturedMonotonicNs"), int):
            return
        recorder = None
        with self._lock:
            self.live_preview = value
            if self.state == OrchestrationState.RUNNING and self.mode == "live":
                recorder = self._recording
        if recorder is not None:
            recorder.submit(value)

    def _start_video_recording(self) -> None:
        client_config = self.recipe.robot.client.config or {}
        telemetry = client_config.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        frame_rate = min(15.0, max(0.5, float(telemetry.get("preview_rate_hz", 8))))
        camera_key, camera_label = self._recording_camera_spec(telemetry)
        with self._lock:
            self._recording_segment += 1
            recorder = DeploymentVideoRecorder(
                self.recording_root,
                deployment_id=self.recipe.deployment_id,
                orchestration_id=self.id,
                segment=self._recording_segment,
                frame_rate=frame_rate,
                camera_key=camera_key,
                camera_label=camera_label,
            )
            self._recording = recorder
        try:
            recorder.start()
            status = recorder.snapshot()
            self._record(
                "video_recording_started",
                directory=status["directory"],
                segment=self._recording_segment,
                cameraKey=camera_key,
                cameraLabel=camera_label,
            )
        except Exception as error:  # noqa: BLE001
            status = {
                "enabled": True,
                "status": "error",
                "directory": str(self.recording_root),
                "path": None,
                "startedNs": None,
                "finishedNs": time.time_ns(),
                "frameRate": frame_rate,
                "cameraKey": camera_key,
                "cameraLabel": camera_label,
                "frames": 0,
                "droppedFrames": 0,
                "error": str(error),
            }
            with self._lock:
                self._recording = None
            self._record("video_recording_failed", reason=str(error))
        with self._lock:
            self._recording_status = status

    def _stop_video_recording(self) -> dict[str, Any]:
        with self._lock:
            recorder = self._recording
        if recorder is None:
            with self._lock:
                return json.loads(json.dumps(self._recording_status))
        status = recorder.stop()
        with self._lock:
            self._recording_status = status
            self._recording = None
        if status.get("status") == "saved":
            self._record(
                "video_recording_saved",
                path=status.get("path"),
                cameraKey=status.get("cameraKey"),
                frames=status.get("frames"),
                droppedFrames=status.get("droppedFrames"),
            )
        elif status.get("status") == "error":
            self._record("video_recording_failed", reason=status.get("error"))
        return status

    @staticmethod
    def _recording_camera_spec(telemetry: dict[str, Any]) -> tuple[str | None, str | None]:
        cameras = [camera for camera in (telemetry.get("cameras") or []) if isinstance(camera, dict)]
        if not cameras:
            return None, None
        selected = next(
            (
                camera
                for camera in cameras
                if camera.get("role") == "primary" or camera.get("record") is True
            ),
            cameras[0],
        )
        key = selected.get("key")
        normalized_key = key.strip() if isinstance(key, str) and key.strip() else None
        label = selected.get("label")
        normalized_label = label.strip() if isinstance(label, str) and label.strip() else normalized_key
        return normalized_key, normalized_label

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
        client_config = self.recipe.robot.client.config or {}
        telemetry = client_config.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        preview_rate_hz = min(15.0, max(0.5, float(telemetry.get("preview_rate_hz", 8))))
        preview_enabled = self.recipe.robot.client.builtin == "python_adapter"
        preview_interval = 1.0 / preview_rate_hz if preview_enabled else interval
        monitor_tick = min(0.05, preview_interval) if preview_enabled else interval
        next_preview_check = time.monotonic()
        next_component_check = time.monotonic()
        while not self._stop_requested.is_set() and not self._monitor_stop.wait(monitor_tick):
            now = time.monotonic()
            if self.components.get("client", {}).get("active") and now >= next_preview_check:
                self._refresh_python_adapter_live_preview()
                next_preview_check += preview_interval
                if next_preview_check <= now:
                    next_preview_check = time.monotonic() + preview_interval
            if self._maintenance.is_set():
                continue
            now = time.monotonic()
            if now < next_component_check:
                continue
            next_component_check = now + interval
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
            if not self.components.get(component, {}).get("active"):
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

    def promote_live(self, confirmation: str, *, record_video: bool = False) -> dict[str, Any]:
        with self._lock:
            expected = f"LIVE {self.recipe.deployment_id} {self._arm_token or ''}"
            if time.monotonic_ns() > self._arm_expires_ns or not secrets.compare_digest(confirmation.strip(), expected):
                raise ValueError("Live 确认短语无效或已过期")
            if self.state != OrchestrationState.DRY_RUN or self.mode != "dry_run":
                raise ValueError("部署当前不处于 Dry Run")
            if self.dry_run_safety is not None and (
                not self.dry_run_safety.get("passed")
                or int(self.dry_run_safety.get("rejections") or 0) > 0
            ):
                raise ValueError(
                    "最近一次 Dry Run 动作校验未通过："
                    + str(self.dry_run_safety.get("error") or "未知安全错误")
                )
            previous_state = self.state
            previous_mode = self.mode
            previous_config = json.loads(json.dumps(self.recipe.robot.client.config or {}))
            self._invalidate_arm_unlocked()
            self._maintenance.set()
            self.state = OrchestrationState.STARTING
            self._record("live_start_requested")
        try:
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = "live"
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.last_error = f"切换 Live 失败：{error}"
            self._recover_client_switch(
                previous_state=previous_state,
                previous_mode=previous_mode,
                previous_config=previous_config,
                failure_label="切换 Live 失败",
                failure=error,
            )
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = OrchestrationState.RUNNING
            if not record_video:
                self._recording_status = {
                    **self._recording_status,
                    "enabled": False,
                    "status": "disabled",
                    "path": None,
                    "startedNs": None,
                    "finishedNs": None,
                    "frames": 0,
                    "droppedFrames": 0,
                    "error": None,
                }
        if record_video:
            self._start_video_recording()
        with self._lock:
            self._record("live_started", recordVideo=record_video)
            return self.snapshot()

    def request_stop_evaluation(self) -> dict[str, Any]:
        """Queue a fast Live pause and restore read-only inference in the background.

        The action client is stopped before video finalization and Dry Run recovery.
        Returning immediately keeps the control panel responsive while the snapshot
        exposes the exact recovery step.
        """
        with self._lock:
            if self._operation_thread is not None and self._operation_thread.is_alive():
                raise ValueError("已有部署控制操作正在进行")
            if self.state != OrchestrationState.RUNNING or self.mode != "live":
                raise ValueError("只有正在运行的真机评测可以结束")
            self._maintenance.set()
            self.state = OrchestrationState.STOPPING
            self.current_step = "evaluation_client_stop"
            self._record("live_stop_requested")
            thread = threading.Thread(
                target=self._stop_evaluation_worker,
                daemon=True,
                name=f"evaluation-stop-{self.id[:8]}",
            )
            self._operation_thread = thread
            thread.start()
            return self.snapshot()

    def stop_evaluation(self) -> dict[str, Any]:
        """Synchronous compatibility wrapper for CLI and direct callers."""
        self.request_stop_evaluation()
        with self._lock:
            thread = self._operation_thread
        if thread is not None and thread is not threading.current_thread():
            timeout = float(self.recipe.robot.client.startup_timeout_s) + 45.0
            thread.join(timeout=timeout)
        return self.snapshot()

    def _stop_evaluation_worker(self) -> None:
        failed = False
        try:
            self._step(
                "evaluation_hold",
                lambda: self._run_operation(self.recipe.robot.hold, "结束评测 hold"),
            )

            def stop_action_client() -> None:
                self.robot_manager.stop("client")
                self.components["client"]["active"] = False

            # Stop command output before doing slower MP4 finalization or waiting for
            # the replacement read-only client to complete its first inference.
            self._step("evaluation_client_stop", stop_action_client)
            self._step("evaluation_recording_finalize", self._stop_video_recording)
            with self._lock:
                self.mode = "dry_run"
            self._step("evaluation_dry_run_start", self._start_client)
            self._step("evaluation_dry_run_health", self._wait_client_health)
        except Exception as error:
            failed = True
            with self._lock:
                self.last_error = f"结束评测后恢复 Dry Run 失败：{error}"
                self.state = OrchestrationState.FAULT
                self._stop_requested.set()
            self._rollback(emergency=True, preserve_model=True)
        finally:
            self._maintenance.clear()
        with self._lock:
            if not failed:
                self.state = OrchestrationState.DRY_RUN
                self.current_step = None
                self._record(
                    "live_stopped",
                    modelActive=self.components.get("model", {}).get("active", False),
                )
            self._operation_thread = None

    def _recordable_robot_state_unlocked(self) -> tuple[dict[str, Any] | None, str | None]:
        """Return the controlled-joint observation without requiring model inference."""
        if not self.components.get("client", {}).get("active"):
            return None, None
        preview_state = (self.live_preview or {}).get("state")
        if isinstance(preview_state, dict):
            return preview_state, "livePreview"
        return None, None

    def _load_recorded_poses(self) -> None:
        """Load robot-scoped poses without allowing a broken file to block deployment."""
        if not self.pose_path.exists():
            return
        try:
            payload = json.loads(self.pose_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("version") != 1:
                raise ValueError("位姿文件版本无效")
            poses = payload.get("poses")
            if not isinstance(poses, list):
                raise ValueError("位姿文件 poses 必须是数组")
            width = int((self.recipe.robot.client.config or {}).get("action", {}).get("width", 0))
            loaded: list[dict[str, Any]] = []
            for raw in poses[-50:]:
                if not isinstance(raw, dict):
                    continue
                values = raw.get("values")
                names = raw.get("names", [])
                units = raw.get("units", [])
                if (
                    not isinstance(raw.get("poseId"), str)
                    or not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", raw["poseId"])
                    or not isinstance(raw.get("name"), str)
                    or not 1 <= len(raw["name"]) <= 100
                    or not isinstance(values, list)
                    or len(values) != width
                    or not all(
                        isinstance(value, (int, float))
                        and not isinstance(value, bool)
                        and math.isfinite(float(value))
                        for value in values
                    )
                    or not isinstance(names, list)
                    or len(names) not in {0, width}
                    or any(not isinstance(value, str) for value in names)
                    or not isinstance(units, list)
                    or len(units) not in {0, width}
                    or any(not isinstance(value, str) for value in units)
                ):
                    continue
                created_ns = raw.get("createdNs")
                loaded.append(
                    {
                        "poseId": raw["poseId"],
                        "name": raw["name"],
                        "values": [float(value) for value in values],
                        "names": list(names),
                        "units": list(units),
                        "createdNs": int(created_ns) if isinstance(created_ns, int) else 0,
                    }
                )
            self.recorded_poses = loaded
        except (OSError, TypeError, ValueError, json.JSONDecodeError) as error:
            self.recorded_poses = []
            self._pose_store_warning = str(error)

    def _persist_recorded_poses_unlocked(self) -> None:
        """Atomically persist the complete pose list while holding ``self._lock``."""
        self.pose_path.parent.mkdir(parents=True, exist_ok=True)
        self.pose_path.parent.chmod(0o700)
        temporary = self.pose_path.with_name(f".{self.pose_path.name}.{uuid.uuid4().hex}.tmp")
        payload = {
            "version": 1,
            "updatedNs": time.time_ns(),
            "poses": self.recorded_poses,
        }
        try:
            with temporary.open("x", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            temporary.chmod(0o600)
            os.replace(temporary, self.pose_path)
            self.pose_path.chmod(0o600)
        finally:
            if temporary.exists():
                try:
                    temporary.unlink()
                except OSError:
                    pass

    def record_pose(self, name: str | None = None) -> dict[str, Any]:
        """Capture controlled robot joints from the observation link, independent of the model."""
        with self._lock:
            client_active = bool(self.components.get("client", {}).get("active"))
        if not client_active:
            raise ValueError("本体观测链路尚未连接")
        if self.recipe.robot.client.builtin == "python_adapter":
            # Read the newest adapter observation immediately instead of
            # waiting for the monitor interval or an inference request.
            self._refresh_python_adapter_live_preview()
        with self._lock:
            state, source = self._recordable_robot_state_unlocked()
            state = state or {}
            values = state.get("values")
            width = int((self.recipe.robot.client.config or {}).get("action", {}).get("width", 0))
            if not isinstance(values, list) or len(values) != width or not all(
                isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))
                for value in values
            ):
                raise ValueError("尚未获得可记录的本体受控关节状态")
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
            previous = list(self.recorded_poses)
            self.recorded_poses.append(pose)
            del self.recorded_poses[:-50]
            try:
                self._persist_recorded_poses_unlocked()
            except OSError as error:
                self.recorded_poses = previous
                raise RuntimeError(f"保存位姿失败：{error}") from error
            self._record(
                "pose_recorded",
                poseId=pose["poseId"],
                name=pose["name"],
                stateSource=source,
            )
            return self.snapshot()

    def delete_pose(self, pose_id: str) -> dict[str, Any]:
        with self._lock:
            previous = list(self.recorded_poses)
            before = len(previous)
            self.recorded_poses = [pose for pose in self.recorded_poses if pose["poseId"] != pose_id]
            if len(self.recorded_poses) == before:
                raise ValueError("记录位姿不存在")
            try:
                self._persist_recorded_poses_unlocked()
            except OSError as error:
                self.recorded_poses = previous
                raise RuntimeError(f"删除位姿保存失败：{error}") from error
            self._record("pose_deleted", poseId=pose_id)
            return self.snapshot()

    def move_to_recorded_pose(self, pose_id: str, *, duration_s: float = 3.0) -> dict[str, Any]:
        """Pause evaluation, move through the generic adapter, then resume observation only."""
        if self.recipe.robot.client.builtin != "python_adapter":
            raise ValueError("一键回位当前需要使用通用 Python Adapter")
        with self._lock:
            if self._maintenance.is_set():
                raise ValueError("部署切换正在进行，请等待暂停或启动完成后再回位")
            if (
                self.state not in {
                    OrchestrationState.ROBOT_READY,
                    OrchestrationState.DRY_RUN,
                    OrchestrationState.RUNNING,
                }
                or not self.components.get("client", {}).get("active")
            ):
                raise ValueError("只有本体观测与执行链路可用时才能一键回位")
            pose = next((item for item in self.recorded_poses if item["poseId"] == pose_id), None)
            if pose is None:
                raise ValueError("记录位姿不存在")
            previous_state = self.state
            was_running = self.state == OrchestrationState.RUNNING
            resume_mode = "observe" if previous_state == OrchestrationState.ROBOT_READY else "dry_run"
            self._maintenance.set()
            self._record("pose_move_requested", poseId=pose_id, name=pose["name"], wasRunning=was_running)
        try:
            if was_running:
                self._run_operation(self.recipe.robot.hold, "回位前 hold")
            self.robot_manager.stop("client")
            self.components["client"]["active"] = False
            self.mode = resume_mode

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
            move_lines = [line for line in result.stdout.splitlines() if line.strip()]
            try:
                move_result = json.loads(move_lines[-1]) if move_lines else {}
            except json.JSONDecodeError:
                move_result = {}
            self._start_client()
            self._wait_client_health()
        except Exception as error:
            self.last_error = f"一键回位失败：{error}"
            self._record(
                "pose_move_failed",
                poseId=pose_id,
                name=pose["name"],
                reason=str(error),
            )
            if not self.components["client"]["active"]:
                try:
                    self.mode = resume_mode
                    self._start_client()
                    self._wait_client_health()
                except Exception:
                    pass
            raise
        finally:
            self._maintenance.clear()
        with self._lock:
            self.state = (
                OrchestrationState.ROBOT_READY
                if previous_state == OrchestrationState.ROBOT_READY
                else OrchestrationState.DRY_RUN
            )
            self._record(
                "pose_move_finished",
                poseId=pose_id,
                name=pose["name"],
                verified=move_result.get("verified"),
                maxError=move_result.get("maxError"),
                worstIndex=move_result.get("worstIndex"),
                moveDurationS=move_result.get("totalDurationS"),
            )
            return self.snapshot()

    def request_hardware_replay(
        self,
        replay: dict[str, Any],
        *,
        move_to_start_duration_s: float = 3.0,
    ) -> dict[str, Any]:
        """Replay a recorded dataset action segment through the configured robot adapter."""
        if self.recipe.robot.client.builtin != "python_adapter":
            raise ValueError("真机 Replay 需要使用通用 Python Adapter")
        action = replay.get("action") if isinstance(replay, dict) else None
        actions = action.get("values") if isinstance(action, dict) else None
        fps = replay.get("fps") if isinstance(replay, dict) else None
        client_config = self.recipe.robot.client.config or {}
        action_config = client_config.get("action", {})
        control_config = client_config.get("control", {})
        width = int(action_config.get("width", 0))
        control_rate_hz = float(control_config.get("rate_hz", 0))
        configured_names = (
            client_config.get("telemetry", {}).get("action", {}).get("names")
            if isinstance(client_config.get("telemetry"), dict)
            and isinstance(client_config["telemetry"].get("action"), dict)
            else None
        )
        recorded_names = action.get("names") if isinstance(action, dict) else None
        if (
            not isinstance(actions, list)
            or not actions
            or len(actions) > 100_000
            or any(
                not isinstance(row, list)
                or len(row) != width
                or not all(
                    isinstance(value, (int, float))
                    and not isinstance(value, bool)
                    and math.isfinite(float(value))
                    for value in row
                )
                for row in actions
            )
        ):
            raise ValueError(f"数据集动作必须是非空的 [时间, {width}] 有限数值数组")
        if (
            isinstance(configured_names, list)
            and len(configured_names) == width
            and isinstance(recorded_names, list)
            and len(recorded_names) == width
            and [str(item) for item in recorded_names]
            != [str(item) for item in configured_names]
        ):
            raise ValueError("数据集动作关节顺序与当前本体配置不一致")
        if (
            not isinstance(fps, (int, float))
            or isinstance(fps, bool)
            or not math.isfinite(float(fps))
            or float(fps) <= 0
            or float(fps) > control_rate_hz * 1.01
        ):
            raise ValueError(
                f"数据集 FPS 必须大于 0 且不高于本体控制频率 {control_rate_hz:g} Hz"
            )
        if not math.isfinite(move_to_start_duration_s) or not 0 < move_to_start_duration_s <= 60:
            raise ValueError("移动到 Replay 起始位姿的时长必须在 0 到 60 秒之间")
        with self._lock:
            if self._operation_thread is not None and self._operation_thread.is_alive():
                raise ValueError("已有部署控制操作正在进行")
            if (
                self.state not in {OrchestrationState.ROBOT_READY, OrchestrationState.DRY_RUN}
                or not self.components.get("client", {}).get("active")
            ):
                raise ValueError("本体连接并处于只读观测或 Dry Run 时才能执行真机 Replay")
            previous_state = self.state
            previous_mode = self.mode
            payload = {
                "actions": actions,
                "fps": float(fps),
                "start_frame": int(replay.get("startFrame", 0)),
                "move_to_start_duration_s": float(move_to_start_duration_s),
            }
            self._maintenance.set()
            self._hardware_replay_stop.clear()
            self.mode = "replay"
            self.state = OrchestrationState.STARTING
            self.current_step = "hardware_replay_prepare"
            self.hardware_replay = {
                "status": "preparing",
                "dataset": replay.get("dataset"),
                "episodeIndex": replay.get("episodeIndex"),
                "startFrame": replay.get("startFrame"),
                "endFrame": replay.get("endFrame"),
                "fps": float(fps),
                "totalFrames": len(actions),
                "framesApplied": 0,
                "error": None,
            }
            self._record(
                "hardware_replay_requested",
                dataset=replay.get("dataset"),
                episodeIndex=replay.get("episodeIndex"),
                startFrame=replay.get("startFrame"),
                totalFrames=len(actions),
                fps=float(fps),
            )
            thread = threading.Thread(
                target=self._hardware_replay_worker,
                args=(payload, previous_state, previous_mode),
                daemon=True,
                name=f"hardware-replay-{self.id[:8]}",
            )
            self._operation_thread = thread
            thread.start()
            return self.snapshot()

    def request_stop_hardware_replay(self) -> dict[str, Any]:
        with self._lock:
            if self.mode != "replay" or self.state not in {
                OrchestrationState.STARTING,
                OrchestrationState.REPLAYING,
                OrchestrationState.STOPPING,
            }:
                raise ValueError("当前没有正在运行的真机 Replay")
            self._hardware_replay_stop.set()
            self.state = OrchestrationState.STOPPING
            self.current_step = "hardware_replay_stop"
            self.hardware_replay["status"] = "stopping"
            self._record("hardware_replay_stop_requested")
        try:
            self.robot_manager.request_stop("client")
        except Exception as error:
            with self._lock:
                self._record("hardware_replay_stop_signal_failed", reason=str(error))
        return self.snapshot()

    def _hardware_replay_worker(
        self,
        payload: dict[str, Any],
        previous_state: OrchestrationState,
        previous_mode: str,
    ) -> None:
        replay_error: str | None = None
        try:
            self._step("hardware_replay_client_stop", self._stop_observation_client)
            if self._hardware_replay_stop.is_set() or self._stop_requested.is_set():
                raise StopRequested()
            with self._lock:
                self.mode = "replay"
            self._step(
                "hardware_replay_client_start",
                lambda: self._start_client(replay_payload=payload),
            )
            with self._lock:
                self.state = OrchestrationState.REPLAYING
                self.current_step = None
                self.hardware_replay["status"] = "starting"
                self._record("hardware_replay_started")
            deadline = (
                time.monotonic()
                + float(payload["move_to_start_duration_s"])
                + len(payload["actions"]) / float(payload["fps"])
                + 60.0
            )
            while (
                not self._hardware_replay_stop.is_set()
                and not self._stop_requested.is_set()
                and time.monotonic() < deadline
            ):
                self._refresh_python_adapter_model_io()
                with self._lock:
                    status = str(self.hardware_replay.get("status") or "")
                    replay_fault = self.hardware_replay.get("error")
                if status in {"finished", "stopped"}:
                    break
                if status == "fault":
                    raise RuntimeError(str(replay_fault or "真机 Replay Client 故障"))
                time.sleep(0.2)
            else:
                if (
                    not self._hardware_replay_stop.is_set()
                    and not self._stop_requested.is_set()
                ):
                    raise TimeoutError("真机 Replay 超过预计时长")
        except StopRequested:
            pass
        except Exception as error:
            replay_error = str(error)
            with self._lock:
                self.hardware_replay["status"] = "fault"
                self.hardware_replay["error"] = replay_error
                self.last_error = f"真机 Replay 失败：{error}"
                self._record("hardware_replay_failed", reason=replay_error)
        finally:
            if self.components.get("client", {}).get("active"):
                try:
                    self.robot_manager.stop("client", ignore_errors=True)
                except Exception as error:
                    replay_error = replay_error or str(error)
                self.components["client"]["active"] = False

            if not self._stop_requested.is_set():
                try:
                    with self._lock:
                        self.mode = previous_mode
                        self.current_step = "hardware_replay_restore"
                    self._start_client()
                    self._wait_client_health()
                except Exception as error:
                    replay_error = (
                        f"{replay_error}；恢复本体观测失败：{error}"
                        if replay_error
                        else f"恢复本体观测失败：{error}"
                    )
                    with self._lock:
                        self.last_error = replay_error
                        self.hardware_replay["status"] = "fault"
                        self.hardware_replay["error"] = replay_error
            self._maintenance.clear()

        with self._lock:
            if not self._stop_requested.is_set():
                restored = bool(self.components.get("client", {}).get("active"))
                if restored:
                    self.state = previous_state
                    self.current_step = None
                else:
                    self.state = OrchestrationState.FAULT
                if not replay_error:
                    stopped = self._hardware_replay_stop.is_set()
                    self.hardware_replay["status"] = "stopped" if stopped else "finished"
                    self._record(
                        "hardware_replay_stopped" if stopped else "hardware_replay_finished",
                        framesApplied=self.hardware_replay.get("framesApplied"),
                        totalFrames=self.hardware_replay.get("totalFrames"),
                    )
            self._operation_thread = None


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
                OrchestrationState.ROBOT_READY,
                OrchestrationState.DRY_RUN,
                OrchestrationState.RUNNING,
            }:
                raise ValueError("当前部署状态不能切换推理调度")
            previous_state = self.state
            was_running = self.state == OrchestrationState.RUNNING
            previous_mode = self.mode
            self.recipe.robot.client.config = config
            self.scheduler_status = None
            self._record(
                "scheduler_update_requested",
                mode=mode,
                actionSteps=int(action_steps),
                requestAfterSteps=request_after_steps,
            )
            if self.state in {
                OrchestrationState.MODEL_READY,
                OrchestrationState.ROBOT_READY,
                OrchestrationState.DRY_RUN,
            }:
                self._record(
                    "scheduler_updated",
                    clientRestarted=False,
                    effectiveOnNextLive=self.state == OrchestrationState.DRY_RUN,
                )
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
            self.scheduler_status = None
            self.last_error = f"切换推理调度失败：{error}"
            self._recover_client_switch(
                previous_state=previous_state,
                previous_mode=previous_mode,
                previous_config=previous_config,
                failure_label="切换推理调度失败",
                failure=error,
            )
            raise
        finally:
            self._maintenance.clear()
        self._record("scheduler_updated", clientRestarted=True)
        return self.snapshot()

    def request_disconnect_robot(self) -> dict[str, Any]:
        """Start a non-blocking robot disconnect and keep the model resident."""
        with self._lock:
            if self._operation_thread is not None and self._operation_thread.is_alive():
                raise ValueError("已有部署控制操作正在进行")
            robot_linked = any(
                self.components.get(component, {}).get("active")
                for component in ("client", "ros", "tunnel")
            )
            if not robot_linked:
                raise ValueError("当前没有可断开的本体连接")
            was_running = self.state == OrchestrationState.RUNNING
            self._maintenance.set()
            self._monitor_stop.set()
            self.state = OrchestrationState.STOPPING
            self.current_step = "robot_disconnect"
            step = {
                "name": "robot_disconnect",
                "status": "running",
                "startedNs": time.time_ns(),
            }
            self.steps.append(step)
            self._record("robot_disconnect_requested", running=was_running)
            thread = threading.Thread(
                target=self._disconnect_robot_worker,
                args=(was_running, step),
                daemon=True,
                name=f"robot-disconnect-{self.id[:8]}",
            )
            self._operation_thread = thread
            thread.start()
            return self.snapshot()

    def disconnect_robot(self) -> dict[str, Any]:
        """Synchronous compatibility wrapper for CLI and direct callers."""
        self.request_disconnect_robot()
        with self._lock:
            thread = self._operation_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=45)
        return self.snapshot()

    def _disconnect_robot_worker(self, was_running: bool, step: dict[str, Any]) -> None:
        errors: list[str] = []
        try:
            if was_running:
                try:
                    self._run_operation(self.recipe.robot.hold, "断开本体前 hold")
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
            self._stop_video_recording()
            active_components = [
                component
                for component in ("client", "ros", "tunnel")
                if self.components.get(component, {}).get("active")
            ]
            if active_components:
                stop_many = getattr(self.robot_manager, "stop_many", None)
                if callable(stop_many):
                    try:
                        stop_many(active_components)
                        for component in active_components:
                            self.components[component]["active"] = False
                    except Exception as error:  # noqa: BLE001
                        errors.append(f"robot stack: {error}")
                if not callable(stop_many) or any(
                    self.components.get(component, {}).get("active")
                    for component in active_components
                ):
                    for component in active_components:
                        if not self.components.get(component, {}).get("active"):
                            continue
                        try:
                            self.robot_manager.stop(component)
                        except Exception as fallback_error:  # noqa: BLE001
                            errors.append(f"{component}: {fallback_error}")
                        self.components[component]["active"] = False
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))
        finally:
            self._maintenance.clear()
        with self._lock:
            step.update(
                {
                    "status": "failed" if errors else "passed",
                    "finishedNs": time.time_ns(),
                    **({"error": "；".join(errors)} if errors else {}),
                }
            )
            self.mode = "dry_run"
            self.live_preview = None
            self.current_step = None
            self.state = (
                OrchestrationState.MODEL_READY
                if self.components.get("model", {}).get("active")
                else OrchestrationState.STOPPED
            )
            self.last_error = "；".join(errors) if errors else None
            self._record("robot_disconnected", errors=errors, modelPreserved=True)
            self._operation_thread = None

    def request_close_model(self) -> dict[str, Any]:
        """Stop model inference while preserving or restoring robot observation."""
        with self._lock:
            if self._operation_thread is not None and self._operation_thread.is_alive():
                raise ValueError("已有部署控制操作正在进行")
            if not self.components.get("model", {}).get("active"):
                raise ValueError("模型当前未启动")
            self._maintenance.set()
            was_running = self.state == OrchestrationState.RUNNING
            self.state = OrchestrationState.STOPPING
            self.current_step = "model_close"
            step = {
                "name": "model_close",
                "status": "running",
                "startedNs": time.time_ns(),
            }
            self.steps.append(step)
            self._record("model_close_requested", running=was_running)
            thread = threading.Thread(
                target=self._close_model_worker,
                args=(was_running, step),
                daemon=True,
                name=f"model-close-{self.id[:8]}",
            )
            self._operation_thread = thread
            thread.start()
            return self.snapshot()

    def close_model(self) -> dict[str, Any]:
        """Synchronous compatibility wrapper for CLI and direct callers."""
        self.request_close_model()
        with self._lock:
            thread = self._operation_thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=60)
        return self.snapshot()

    def _close_model_worker(self, was_running: bool, step: dict[str, Any]) -> None:
        errors: list[str] = []
        robot_observable = bool(self.components.get("ros", {}).get("active"))
        try:
            if was_running:
                try:
                    self._run_operation(self.recipe.robot.hold, "关闭模型前 hold")
                except Exception as error:  # noqa: BLE001
                    errors.append(str(error))
            self._stop_video_recording()
            client_is_observation_only = (
                self.mode == "observe"
                or (
                    isinstance(self.client_runtime, dict)
                    and self.client_runtime.get("mode") == "observe"
                )
            )
            if self.components.get("client", {}).get("active") and not client_is_observation_only:
                try:
                    self.robot_manager.stop("client")
                except Exception as error:  # noqa: BLE001
                    errors.append(f"client: {error}")
                self.components["client"]["active"] = False
            if self.components.get("tunnel", {}).get("active"):
                try:
                    self.robot_manager.stop("tunnel")
                except Exception as error:  # noqa: BLE001
                    errors.append(f"tunnel: {error}")
                self.components["tunnel"]["active"] = False
            stop_many = getattr(self.model_manager, "stop_many", None)
            if callable(stop_many):
                try:
                    stop_many(["model"])
                except Exception as error:  # noqa: BLE001
                    errors.append(f"model: {error}")
                    stop_many = None
            if not callable(stop_many):
                try:
                    self.model_manager.stop("model")
                except Exception as fallback_error:  # noqa: BLE001
                    errors.append(f"model fallback: {fallback_error}")
            self.components["model"]["active"] = False
            if robot_observable and not self.components.get("client", {}).get("active"):
                self.mode = "observe"
                self._start_client()
                self._wait_client_health()
        except Exception as error:  # noqa: BLE001
            errors.append(str(error))
        finally:
            self._maintenance.clear()
        with self._lock:
            step.update(
                {
                    "status": "failed" if errors else "passed",
                    "finishedNs": time.time_ns(),
                    **({"error": "；".join(errors)} if errors else {}),
                }
            )
            if not robot_observable:
                self.mode = "dry_run"
            self.current_step = None
            self.state = (
                OrchestrationState.FAULT
                if errors
                else (OrchestrationState.ROBOT_READY if robot_observable else OrchestrationState.STOPPED)
            )
            self.last_error = "；".join(errors) if errors else None
            self._record("model_closed", errors=errors, robotPreserved=robot_observable and not errors)
            self._operation_thread = None

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
        self._stop_video_recording()
        with self._lock:
            operation_thread = self._operation_thread
        if operation_thread is not None and operation_thread is not threading.current_thread():
            operation_thread.join(timeout=wait_s)
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=wait_s)
        if self.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}:
            self._rollback(emergency=emergency)
        return self.snapshot()

    def _rollback(self, *, emergency: bool, preserve_model: bool = False) -> None:
        with self._lock:
            self.state = OrchestrationState.STOPPING
            self.current_step = "rollback"
        self._stop_video_recording()
        errors: list[str] = []
        if self.components.get("client", {}).get("active"):
            if not emergency and self.mode != "observe":
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

    def _has_active_components_unlocked(self) -> bool:
        return any(bool(value.get("active")) for value in self.components.values())

    def has_active_components(self) -> bool:
        with self._lock:
            return self._has_active_components_unlocked()

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

    def snapshot(
        self,
        *,
        include_preview: bool = True,
        include_model_images: bool = True,
        trajectory_max_points: int | None = 360,
    ) -> dict[str, Any]:
        with self._lock:
            recording = self._recording.snapshot() if self._recording is not None else self._recording_status
            model_io = json.loads(json.dumps(self.model_io)) if self.model_io is not None else None
            if not include_model_images and isinstance(model_io, dict):
                model_input = model_io.get("input")
                if isinstance(model_input, dict) and isinstance(model_input.get("cameras"), list):
                    model_input["cameras"] = [
                        {key: value for key, value in camera.items() if key != "dataUrl"}
                        if isinstance(camera, dict)
                        else camera
                        for camera in model_input["cameras"]
                    ]
            trajectory_history = (
                json.loads(json.dumps(self.trajectory_history))
                if self.trajectory_history is not None
                else None
            )
            if trajectory_max_points is not None and isinstance(trajectory_history, dict):
                if trajectory_max_points < 2:
                    raise ValueError("trajectory_max_points must be at least 2 or None")
                for key in ("state", "planned", "executed"):
                    points = trajectory_history.get(key)
                    if not isinstance(points, list) or len(points) <= trajectory_max_points:
                        continue
                    last = len(points) - 1
                    count = trajectory_max_points - 1
                    trajectory_history[key] = [
                        points[round(index * last / count)]
                        for index in range(trajectory_max_points)
                    ]
            robot_state, _robot_state_source = self._recordable_robot_state_unlocked()
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
                "modelIo": model_io,
                "robotState": (
                    json.loads(json.dumps(robot_state))
                    if robot_state is not None
                    else None
                ),
                "livePreview": (
                    json.loads(json.dumps(self.live_preview))
                    if include_preview and self.live_preview is not None
                    else None
                ),
                "trajectoryHistory": trajectory_history,
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
                "poseStorage": {
                    "persistent": True,
                    "path": str(self.pose_path),
                    "count": len(self.recorded_poses),
                },
                "recording": json.loads(json.dumps(recording)),
                "hardwareReplay": json.loads(json.dumps(self.hardware_replay)),
                "steps": list(self.steps[-100:]),
                "events": list(self.events[-100:]),
                "createdNs": self.created_ns,
                "updatedNs": self.updated_ns,
                "recordPath": str(self._record_path),
            }

    def live_preview_snapshot(self) -> dict[str, Any]:
        with self._lock:
            return {
                "orchestrationId": self.id,
                "state": self.state.value,
                "livePreview": (
                    json.loads(json.dumps(self.live_preview))
                    if self.live_preview is not None
                    else None
                ),
            }

    def infer_observations(self, observations: dict[str, Any]) -> dict[str, Any]:
        """Run an out-of-band inference on the resident model service.

        This path is used by offline dataset evaluation and deliberately talks
        to the model on its own host, so it does not require or mutate the
        robot-side tunnel/client loop.
        """
        with self._lock:
            if not self.components.get("model", {}).get("active"):
                raise ValueError("模型服务尚未启动")
            if self.state not in {OrchestrationState.MODEL_READY, OrchestrationState.ROBOT_READY}:
                raise ValueError("离线评测要求模型已就绪，且真机动作执行未启动")
            if self._offline_inference_active:
                raise ValueError("已有离线评测正在进行")
            self._offline_inference_active = True
        try:
            return self._infer_observations(observations)
        finally:
            with self._lock:
                self._offline_inference_active = False

    def _infer_observations(self, observations: dict[str, Any]) -> dict[str, Any]:
        bind = self.recipe.tunnel.remote_bind
        if bind in {"0.0.0.0", "::", "[::]"}:
            bind = "127.0.0.1"
        endpoint = f"http://{bind}:{self.recipe.tunnel.remote_port}/infer"
        payload = json.dumps(
            {
                "protocolVersion": 2,
                "deploymentId": self.recipe.deployment_id,
                "mode": "offline_evaluation",
                "capturedMonotonicNs": time.monotonic_ns(),
                "observations": observations,
            },
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        script = r"""
import json, sys, urllib.error, urllib.request
url = sys.argv[1]
body = sys.stdin.buffer.read()
request = urllib.request.Request(url, data=body, headers={'Content-Type':'application/json','Accept':'application/json'})
try:
    with urllib.request.urlopen(request, timeout=float(sys.argv[2])) as response:
        payload = response.read(int(sys.argv[3]) + 1)
        if len(payload) > int(sys.argv[3]): raise SystemExit('模型响应过大')
        sys.stdout.buffer.write(payload)
except urllib.error.HTTPError as error:
    detail = error.read(4000).decode('utf-8', errors='replace')
    raise SystemExit(f'模型 HTTP {error.code}: {detail}')
""".strip()
        timeout = OFFLINE_INFERENCE_TIMEOUT_S
        result = require_remote_ok(
            self.model_runner.run(
                ["python3", "-c", script, endpoint, str(timeout), "10000000"],
                input_data=payload,
                timeout=timeout + 5,
            ),
            "离线模型推理",
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError as error:
            raise RuntimeError("模型返回了非法 JSON") from error
        if not isinstance(response, dict):
            raise RuntimeError("模型响应必须是对象")
        self._record("offline_inference", observationKeys=sorted(observations))
        return response

    def manifest(self) -> dict[str, Any]:
        return {"orchestration": self.snapshot(), "recipe": redact_recipe(self.recipe.model_dump(mode="json"))}


class OrchestrationRegistry:
    def __init__(
        self,
        root: Path,
        *,
        recording_root: Path | None = None,
        pose_root: Path | None = None,
    ):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.recording_root = (recording_root or (self.root / "recordings")).resolve()
        self.recording_root.mkdir(parents=True, exist_ok=True)
        self.recording_root.chmod(0o700)
        self.pose_root = (pose_root or (self.root / "poses")).resolve()
        self.pose_root.mkdir(parents=True, exist_ok=True)
        self.pose_root.chmod(0o700)
        self._items: dict[str, DeploymentOrchestration] = {}
        self._lock = threading.RLock()

    def create(
        self,
        raw: dict[str, Any],
        *,
        mode: str | None = None,
        robot_config_id: str | None = None,
    ) -> DeploymentOrchestration:
        recipe = parse_recipe(raw)
        if mode is not None:
            if mode not in {"dry_run", "live"}:
                raise ValueError("mode 必须是 dry_run 或 live")
            recipe.runtime.default_mode = mode
        pose_key = robot_config_id or recipe.deployment_id.partition("--")[0]
        if not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", pose_key):
            raise ValueError("robot_config_id 非法")
        item = DeploymentOrchestration(
            recipe,
            self.root / recipe.deployment_id,
            recording_root=self.recording_root,
            pose_path=self.pose_root / f"{pose_key}.json",
        )
        with self._lock:
            active = [
                existing
                for existing in self._items.values()
                if existing.recipe.deployment_id == recipe.deployment_id
                and (
                    existing.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}
                    or existing.has_active_components()
                )
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
            return [
                item.snapshot(include_preview=False, include_model_images=False)
                for item in self._items.values()
            ]

    def stop_all(self) -> None:
        with self._lock:
            items = list(self._items.values())
        for item in items:
            if (
                item.state not in {OrchestrationState.STOPPED, OrchestrationState.FAULT}
                or item.has_active_components()
            ):
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
