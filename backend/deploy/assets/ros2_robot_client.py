#!/usr/bin/env python3
"""Generic ROS2 robot-side runtime asset for Embodit Recipe v2.

The process runs on the robot host.  It reads standard ROS2 observations,
calls the model through the robot-local SSH tunnel, validates actions locally,
and sends FollowJointTrajectory goals only in live mode.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import signal
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


class RobotClient:
    def __init__(self, config: dict[str, Any]):
        import rclpy
        from control_msgs.action import FollowJointTrajectory
        from rclpy.action import ActionClient
        from rclpy.executors import SingleThreadedExecutor
        from rclpy.qos import qos_profile_sensor_data
        from sensor_msgs.msg import CompressedImage, Image, JointState
        from std_msgs.msg import String

        self.rclpy = rclpy
        self.follow_joint_trajectory = FollowJointTrajectory
        self.config = config
        self.mode = os.environ.get("EMBODIT_DEPLOYMENT_MODE", config.get("mode", "dry_run"))
        if self.mode not in {"dry_run", "live"}:
            raise ValueError("EMBODIT_DEPLOYMENT_MODE 必须是 dry_run 或 live")
        self.running = True
        self.latest: dict[str, tuple[int, Any]] = {}
        self.condition = threading.Condition()
        self.goal_handles: list[Any] = []
        self.sequence = 0
        self.last_success_ns: int | None = None

        if not rclpy.ok():
            rclpy.init(args=None)
        self.node = rclpy.create_node(config.get("node_name", "vla_robot_client"))
        self.status_publisher = self.node.create_publisher(String, config.get("status_topic", "/embodit/deployment_status"), 10)
        message_classes = {
            "sensor_msgs/msg/JointState": JointState,
            "sensor_msgs/msg/Image": Image,
            "sensor_msgs/msg/CompressedImage": CompressedImage,
        }
        for name, observation in config["observations"].items():
            message_type = observation["type"]
            if message_type not in message_classes:
                raise ValueError(f"不支持的观测类型：{message_type}")
            callback = self._callback(name, observation)
            self.node.create_subscription(message_classes[message_type], observation["topic"], callback, qos_profile_sensor_data)

        controller = config["controller"]
        self.action_client = ActionClient(self.node, FollowJointTrajectory, controller["action"])
        self.executor = SingleThreadedExecutor()
        self.executor.add_node(self.node)
        self.spin_thread = threading.Thread(target=self.executor.spin, daemon=True, name="embodit-ros-spin")
        self.spin_thread.start()

    def _callback(self, name: str, observation: dict[str, Any]):
        def receive(message: Any) -> None:
            try:
                value = self._convert_observation(message, observation)
                with self.condition:
                    self.latest[name] = (time.monotonic_ns(), value)
                    self.condition.notify_all()
            except Exception as error:  # noqa: BLE001
                self.publish_status("observation_error", observation=name, error=str(error))
        return receive

    @staticmethod
    def _convert_observation(message: Any, config: dict[str, Any]) -> Any:
        kind = config["type"]
        if kind == "sensor_msgs/msg/JointState":
            names = list(message.name)
            positions = list(message.position)
            if len(names) != len(positions):
                raise ValueError("JointState name/position 长度不一致")
            values = dict(zip(names, positions))
            joints = config.get("joints") or names
            missing = [joint for joint in joints if joint not in values]
            if missing:
                raise ValueError("JointState 缺少关节：" + ", ".join(missing))
            return [float(values[joint]) for joint in joints]
        header = getattr(message, "header", None)
        stamp = getattr(header, "stamp", None)
        stamp_ns = None if stamp is None else int(stamp.sec) * 1_000_000_000 + int(stamp.nanosec)
        if kind == "sensor_msgs/msg/CompressedImage":
            return {
                "encoding": str(message.format),
                "stampNs": stamp_ns,
                "frameId": str(getattr(header, "frame_id", "")),
                "$binary": base64.b64encode(bytes(message.data)).decode("ascii"),
            }
        return {
            "encoding": str(message.encoding),
            "height": int(message.height),
            "width": int(message.width),
            "step": int(message.step),
            "stampNs": stamp_ns,
            "frameId": str(getattr(header, "frame_id", "")),
            "$binary": base64.b64encode(bytes(message.data)).decode("ascii"),
        }

    def wait_observations(self) -> dict[str, Any]:
        maximum_age_ms = float(self.config.get("maximum_observation_age_ms", 500))
        timeout_s = float(self.config.get("observation_timeout_s", 3))
        required = set(self.config["observations"])
        deadline = time.monotonic() + timeout_s
        with self.condition:
            while not required.issubset(self.latest):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("等待 ROS2 观测超时：" + ", ".join(sorted(required - set(self.latest))))
                self.condition.wait(remaining)
            now = time.monotonic_ns()
            stale = [name for name in required if (now - self.latest[name][0]) / 1_000_000 > maximum_age_ms]
            if stale:
                raise TimeoutError("ROS2 观测过期：" + ", ".join(sorted(stale)))
            return {name: self.latest[name][1] for name in required}

    def infer(self, observations: dict[str, Any]) -> tuple[list[list[float]], float]:
        model = self.config["model"]
        endpoint = model["endpoint"].rstrip("/") + model.get("infer_path", "/infer")
        payload = {
            "protocolVersion": 2,
            "deploymentId": self.config.get("deployment_id"),
            "mode": self.mode,
            "sequence": self.sequence,
            "capturedMonotonicNs": time.monotonic_ns(),
            "observations": observations,
        }
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if model.get("token_env"):
            token = os.environ.get(model["token_env"])
            if not token:
                raise ValueError(f"模型 token 环境变量未设置：{model['token_env']}")
            headers["Authorization"] = f"Bearer {token}"
        request = urllib.request.Request(endpoint, data=body, headers=headers)
        started = time.perf_counter()
        try:
            request_timeout = min(
                float(model.get("timeout_s", 10)),
                float(self.config.get("watchdog_timeout_s", 1)),
            )
            with urllib.request.urlopen(request, timeout=request_timeout) as response:
                maximum_bytes = int(model.get("maximum_response_bytes", 10_000_000))
                response_body = response.read(maximum_bytes + 1)
                if len(response_body) > maximum_bytes:
                    raise ValueError("模型响应超过 maximum_response_bytes")
                result = json.loads(response_body)
        except urllib.error.HTTPError as error:
            detail = error.read().decode("utf-8", errors="replace")[-1000:]
            raise RuntimeError(f"模型 HTTP {error.code}: {detail}") from error
        latency_ms = (time.perf_counter() - started) * 1000
        action = result.get("action", result)
        values = action.get("values") if isinstance(action, dict) else None
        return self.validate_action(values, observations), latency_ms

    def validate_action(self, values: Any, observations: dict[str, Any]) -> list[list[float]]:
        action = self.config["action"]
        joints = action["joints"]
        width = len(joints)
        horizon = int(action.get("horizon", 1))
        if not isinstance(values, list) or len(values) != horizon:
            raise ValueError(f"动作 horizon 必须是 {horizon}")
        normalized: list[list[float]] = []
        for row in values:
            if not isinstance(row, list) or len(row) != width:
                raise ValueError(f"动作宽度必须是 {width}")
            if any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in row):
                raise ValueError("动作元素必须是 JSON number")
            converted = [float(value) for value in row]
            if any(not math.isfinite(value) for value in converted):
                raise ValueError("动作包含 NaN 或 Inf")
            normalized.append(converted)

        limits = action["limits"]
        minimum = limits["minimum"]
        maximum = limits["maximum"]
        max_step = limits["max_step"]
        if not all(len(value) == width for value in (minimum, maximum, max_step)):
            raise ValueError("动作限位维度与 joints 不一致")
        if any(
            not math.isfinite(float(value))
            for limits_row in (minimum, maximum, max_step)
            for value in limits_row
        ):
            raise ValueError("动作限位包含 NaN 或 Inf")
        if any(low >= high for low, high in zip(minimum, maximum)):
            raise ValueError("动作 minimum 必须小于 maximum")
        if any(step <= 0 for step in max_step):
            raise ValueError("动作 max_step 必须大于 0")
        baseline_name = action["baseline_observation"]
        baseline = observations.get(baseline_name)
        if not isinstance(baseline, list) or len(baseline) != width:
            raise ValueError(f"缺少动作基线观测：{baseline_name}")
        if any(isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) for value in baseline):
            raise ValueError("动作基线观测不是有限数值向量")
        previous = [float(value) for value in baseline]
        for row_index, row in enumerate(normalized):
            for index, value in enumerate(row):
                if value < minimum[index] or value > maximum[index]:
                    raise ValueError(f"动作第 {row_index + 1} 帧第 {index + 1} 维超过绝对限位")
                if abs(value - previous[index]) > max_step[index]:
                    raise ValueError(f"动作第 {row_index + 1} 帧第 {index + 1} 维超过 max_step")
            previous = row
        return normalized

    def send_action(self, values: list[list[float]]) -> None:
        from builtin_interfaces.msg import Duration
        from trajectory_msgs.msg import JointTrajectoryPoint

        controller = self.config["controller"]
        action = self.config["action"]
        timeout_s = float(controller.get("server_timeout_s", 3))
        if not self.action_client.wait_for_server(timeout_sec=timeout_s):
            raise TimeoutError("FollowJointTrajectory action server 不可用")
        goal = self.follow_joint_trajectory.Goal()
        goal.trajectory.joint_names = list(action["joints"])
        rate_hz = float(action["rate_hz"])
        for index, row in enumerate(values):
            point = JointTrajectoryPoint()
            point.positions = list(row)
            total_ns = int(round((index + 1) / rate_hz * 1_000_000_000))
            point.time_from_start = Duration(sec=total_ns // 1_000_000_000, nanosec=total_ns % 1_000_000_000)
            goal.trajectory.points.append(point)
        future = self.action_client.send_goal_async(goal)
        deadline = time.monotonic() + timeout_s
        while not future.done() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not future.done() or future.result() is None or not future.result().accepted:
            raise RuntimeError("FollowJointTrajectory goal 被拒绝")
        self.goal_handles = [future.result()]

    def cancel_goals(self) -> None:
        for handle in self.goal_handles:
            try:
                handle.cancel_goal_async()
            except Exception:  # noqa: BLE001
                pass
        self.goal_handles.clear()

    def publish_status(self, status: str, **details: Any) -> None:
        from std_msgs.msg import String

        message = String()
        message.data = json.dumps(
            {"status": status, "mode": self.mode, "sequence": self.sequence, "timeNs": time.time_ns(), **details},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        self.status_publisher.publish(message)

    def run(self) -> None:
        rate_hz = float(self.config.get("loop_rate_hz", 10))
        watchdog_s = float(self.config.get("watchdog_timeout_s", max(1, 3 / rate_hz)))
        self.publish_status("starting")
        while self.running:
            started = time.monotonic()
            try:
                observations = self.wait_observations()
                values, latency_ms = self.infer(observations)
                if self.mode == "live":
                    self.send_action(values)
                self.last_success_ns = time.monotonic_ns()
                self.publish_status("ready", inferenceMs=round(latency_ms, 3), actionSent=self.mode == "live")
                self.sequence += 1
            except Exception as error:  # noqa: BLE001
                self.cancel_goals()
                self.publish_status("fault", error=str(error))
                raise
            elapsed = time.monotonic() - started
            if elapsed > watchdog_s:
                self.cancel_goals()
                raise TimeoutError(f"Robot Client watchdog：单步耗时 {elapsed:.3f}s")
            remaining = 1 / rate_hz - elapsed
            if remaining > 0:
                time.sleep(remaining)

    def close(self) -> None:
        self.running = False
        self.cancel_goals()
        self.publish_status("stopped")
        self.executor.shutdown(timeout_sec=1)
        self.spin_thread.join(timeout=1)
        self.node.destroy_node()
        if self.rclpy.ok():
            self.rclpy.shutdown()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, required=True)
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    client = RobotClient(config)

    def stop(*_: object) -> None:
        client.running = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    try:
        client.run()
    finally:
        client.close()


if __name__ == "__main__":
    main()
