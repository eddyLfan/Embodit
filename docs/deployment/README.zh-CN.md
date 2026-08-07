# 真机部署层使用指南

[English](README.md) · **中文**

本指南说明如何接入模型、本体和安全配置。当前架构已完成真机链路验证；换用新设备时，必须重新确认 SDK/ROS 接口、单位、关节顺序、限位和生命周期操作。

## 1. 运行架构

Embodit 是控制面，不进入实时观测/动作链路：

```text
Workstation [Embodit]
  ├─ local/SSH → Model Host [Model Runner :8000]
  └─ SSH → Robot Host
             ├─ SSH local-forward: localhost:8000 → Model Host:8000
             ├─ ROS Bringup
             └─ Robot Client: observation → model → validation → controller
```

模型可以运行在 Embodit 工作电脑或独立 GPU 主机。本体当前必须能够被 Embodit 通过 SSH 管理，并能访问模型端 SSH 地址以建立受限隧道。

配置分为：

- 本体 Config：主机、ROS、Bringup、readiness、生命周期、初始位姿、Robot Client、限位；
- 模型 Config：主机、Provider、Checkpoint、Python 环境和服务端口；
- Recipe v2：组合两份 Config 后生成的唯一运行配置。

模板：

- [`../../config/deployment/robot.example.json`](../../config/deployment/robot.example.json)
- [`../../config/deployment/models/python.example.json`](../../config/deployment/models/python.example.json)
- [`../../config/deployment/models/openpi.example.json`](../../config/deployment/models/openpi.example.json)
- [`../../config/deployment/models/lerobot.example.json`](../../config/deployment/models/lerobot.example.json)
- [`../../config/deployment/models/starvla.example.json`](../../config/deployment/models/starvla.example.json)

## 2. 接入前准备

### 2.1 工作电脑

- Linux、Python 3.10+、uv、OpenSSH client；
- 能访问本体 SSH；模型在远端时也要能访问模型 SSH；
- 项目目录可写，用于 `.embodit_cache/deploy/` 状态；
- 本地模型配置中的 `host.user` 必须等于运行 Embodit 的用户。

### 2.2 本体端

- Python 3、OpenSSH client、`ssh-keygen`、`ssh-keyscan`；
- systemd 和 `systemd-run`；
- 配置中的 ROS setup 文件；
- Robot Bringup、topic/service/action 或厂商 Python SDK；
- 本体侧必须独立执行硬件限位、命令有效期、错误状态和急停。

### 2.3 模型端

- systemd；
- 对应模型 Python 环境、Checkpoint 和 Embodit checkout；
- 远端模型需要 SSH；本地模型由 Embodit 直接执行；
- `endpoint.bind` 默认 `127.0.0.1`，不应直接暴露到局域网。

内置 Provider 首次使用前初始化固定源码：

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive
git submodule status --recursive
```

每个 Provider 使用独立 Python/CUDA 环境。上游版本和安装边界见 [`../../third_party/README.md`](../../third_party/README.md)。

## 3. 最短接入流程

```bash
mkdir -p config/local/models
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/models/my-model.json
```

编辑两份文件后：

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
export MODEL_SSH_PASSWORD='<model-password>'  # 远端模型需要

bash embodit.sh recipe-compose \
  config/local/my-robot.json \
  config/local/models/my-model.json \
  --output /tmp/my-deployment.json

bash embodit.sh recipe-validate /tmp/my-deployment.json
```

推荐启动网页：

```bash
bash embodit.sh start
```

进入“真机部署”：

1. 选择本体和模型配置；
2. 运行“预检”；预检只读连接主机，检查 systemd、模型路径/Python、ROS setup，ROS 已运行时检查 graph/type/rate/freshness；
3. 启动模型并等待 `/health`；
4. 填写 Prompt，连接本体并进入 Dry Run 或评测；
5. 观察实际模型输入、计划动作、执行动作、延迟和日志；
6. 暂停/断开/关闭，或在危险情况下执行急停。

CLI：

```bash
bash embodit.sh recipe-run /tmp/my-deployment.json --mode dry_run
bash embodit.sh recipe-run /tmp/my-deployment.json --mode live
bash embodit.sh recipe-stop /tmp/my-deployment.json
bash embodit.sh recipe-stop /tmp/my-deployment.json --emergency
```

## 4. 通用主机字段 `host`

本体 Config 和模型 Config 都包含 `host`：

| 字段 | 必填 | 写法 |
|---|---:|---|
| `connection` | 否 | `ssh`（默认）或 `local`；本体只允许 `ssh` |
| `address` | 是 | SSH 地址；本地模型时填写本体可访问的工作电脑地址 |
| `port` | 否 | SSH 端口，默认 `22` |
| `user` | 是 | 目标主机用户名；不能含空格或 `@` |
| `auth` | SSH 是 | 见下表；`local` 禁止填写 |
| `connect_timeout_s` | 否 | SSH 连接超时，默认 `8`，范围 `1..60` |
| `host_key_policy` | 否 | `accept-new` 或 `strict`；稳定环境建议 `strict` |
| `service_manager` | 否 | `system` 或 `user`；决定使用 system/user systemd |

SSH 认证：

```json
{"type": "key", "identity_file": "/home/user/.ssh/id_ed25519"}
{"type": "password_env", "environment_variable": "ROBOT_SSH_PASSWORD"}
{"type": "password", "password": "..."}
```

| 字段 | 写法 |
|---|---|
| `type` | `key`、`password_env` 或 `password` |
| `identity_file` | `type=key` 时的 Embodit 主机私钥路径 |
| `environment_variable` | `type=password_env` 时的变量名 |
| `password` | `type=password` 时的明文密码 |

推荐 `key` 或 `password_env`。直接密码会保存在本地配置文件中；文件权限必须为 `0600`，且不能提交 Git。

## 5. 本体 Config 字段

顶层：

| 字段 | 写法 |
|---|---|
| `version` | 固定 `1` |
| `kind` | 固定 `robot` |
| `config_id` | 唯一 ID，`A-Z/a-z/0-9/_.-`，最长 64 |
| `name` | 页面显示名 |
| `host` | 本体 SSH，见第 4 节 |
| `robot` | ROS、生命周期和 Client |
| `tunnel` | 本体到模型端的本地转发 |
| `runtime` | 停止、回滚和监控策略 |

### 5.1 `robot.ros`

| 字段 | 必填 | 写法 |
|---|---:|---|
| `version` | 是 | `1` 或 `2` |
| `distro` | 是 | 如 `noetic`、`humble`；用于记录和诊断 |
| `setup` | 是 | 本体绝对路径数组，按顺序 `source` |
| `domain_id` | ROS2 可选 | `0..232`；ROS1 禁止 |
| `master_uri` | ROS1 可选 | 如 `http://127.0.0.1:11311`；ROS2 禁止 |
| `rmw_implementation` | ROS2 可选 | 如 `rmw_cyclonedds_cpp` |

### 5.2 `robot.bringup`

Bringup 是 systemd 托管命令：

| 字段 | 写法 |
|---|---|
| `command` | argv 数组，不经过 shell，如 `["ros2","launch","pkg","robot.launch.py"]` |
| `workdir` | 本体绝对工作目录，可选 |
| `setup` | 在 `robot.ros.setup` 后额外 source 的绝对路径 |
| `environment` | 环境变量对象 |
| `startup_timeout_s` | 进入 readiness 前允许的启动时间 |
| `restart` | `no`、`on-failure` 或 `always` |

需要管道、重定向或复合 shell 时，显式使用 `["bash","-lc","..."]`。

### 5.3 `robot.readiness`

| 字段 | 写法 |
|---|---|
| `timeout_s` | 整体 readiness 超时，默认 `60` |
| `interval_s` | 重试间隔，默认 `1` |
| `nodes` | 必须存在的完整节点名 |
| `topics` | topic 名、精确类型、最低频率和新鲜度 |
| `services` | `{name,type}` 数组 |
| `actions` | `{name,type}` 数组；ROS1 不接受，应检查 actionlib topics |

Topic 字段：

| 字段 | 写法 |
|---|---|
| `name` | 以 `/` 开头 |
| `type` | ROS2 如 `sensor_msgs/msg/JointState`；ROS1 如 `sensor_msgs/JointState` |
| `minimum_rate_hz` | `0` 表示不检查频率 |
| `sample_seconds` | 采样窗口，`>0` 且 `<=15` |
| `maximum_age_ms` | ROS2 header 最大年龄；无 header 的类型不要配置 |

启动阶段会检查 node、topic/service/action 类型、topic 频率和新鲜度。ROS1 新鲜度只确认收到消息，不计算 header age。

### 5.4 生命周期操作

`power_on`、`power_off`、`hold`、`stop` 使用同一结构：

| 字段 | 写法 |
|---|---|
| `type` | `none`、`command`、`ros2_service`、`ros1_service` |
| `command` | `type=command` 时的 argv |
| `name` | ROS service 名 |
| `service_type` | 精确 service 类型 |
| `request` | 请求 JSON；空请求用 `{}` |
| `timeout_s` | 调用超时 |

`hold` 应停止新动作并保持安全状态；`stop` 应执行设备定义的最快软件停止。两者不能替代硬件急停。

### 5.5 `robot.initial_pose`

| 字段 | 写法 |
|---|---|
| `type` | `none`、`command`、`follow_joint_trajectory` |
| `action` | FollowJointTrajectory action 名 |
| `command` | `type=command` 时 argv |
| `joint_state_topic` | 实测位置 topic，默认 `/joint_states` |
| `joint_names` | 控制顺序 |
| `positions` | 与 `joint_names` 等长，使用控制器原生单位 |
| `duration_s` | 轨迹时长 |
| `tolerance` | 实测最大绝对误差 |
| `timeout_s` | 动作和实测确认超时 |

`follow_joint_trajectory` 当前只支持 ROS2。示例位置不是通用安全位姿。

### 5.6 `robot.client`

通用字段：

| 字段 | 写法 |
|---|---|
| `builtin` | `ros2_standard`、`python_adapter`，或不填并提供 `command` |
| `config` | 内置 Client 配置 |
| `command/workdir/setup/environment` | 自定义 Client 使用 |
| `startup_timeout_s` | Client readiness 超时 |
| `restart` | Live 推荐 `no`；编排器会避免故障后自动恢复真实动作 |
| `health` | `http/tcp/command/ros_node`；内置 Client 通常用状态节点/topic |

自定义 Client 的 `command/workdir/setup/environment/startup_timeout_s/restart` 与 Bringup 写法相同。组件 Config 中的 `client.host` 建议省略，组合时固定为 `robot`。

`health` 字段：

| 字段 | 写法 |
|---|---|
| `type` | `http`、`tcp`、`command` 或 `ros_node` |
| `url` | `type=http` 时必填 |
| `host` / `port` | TCP 目标；`host` 默认 `127.0.0.1`，`port` 在 `type=tcp` 时必填 |
| `command` | `type=command` 时的 argv |
| `name` | `type=ros_node` 时的完整节点名 |
| `startup_timeout_s` | 等待健康的总时长，默认 `60` |
| `interval_s` | 检查间隔，默认 `1` |

## 6. 标准 ROS2 Client

适用：观测来自 `JointState/Image/CompressedImage`，动作发往 `FollowJointTrajectory`。

最小结构见 [`../../examples/deployment/ros2_robot_client.example.json`](../../examples/deployment/ros2_robot_client.example.json)。主要字段：

| 字段 | 写法 |
|---|---|
| `node_name` | Client 节点名 |
| `status_topic` | Embodit 读取的状态 topic |
| `loop_rate_hz` | 推理循环频率 |
| `watchdog_timeout_s` | 完整观测→推理→校验→发送的超时 |
| `observation_timeout_s` | 等待全部观测的超时 |
| `maximum_observation_age_ms` | 本地接收观测最大年龄 |
| `observations` | 模型输入 key 到 ROS topic/type 的映射 |
| `controller.action` | FollowJointTrajectory action |
| `controller.server_timeout_s` | action server 超时 |
| `action.joints` | 模型动作维度对应的关节顺序 |
| `action.horizon` | 模型每次必须返回的行数 |
| `action.rate_hz` | 轨迹点频率 |
| `action.baseline_observation` | 首步 `max_step` 的基准观测 key |
| `action.limits.minimum/maximum` | 逐维绝对限位 |
| `action.limits.max_step` | 第一帧相对实测、后续帧相邻之间的最大变化 |

模型输出必须严格是 `[horizon][joint_count]` 的有限数值，维度、顺序和单位与控制器一致。

## 7. 通用 Python Robot Adapter

适用：厂商 SDK 不适合封装为标准 ROS topic/action。复制 [`../../examples/deployment/python_robot_adapter.py`](../../examples/deployment/python_robot_adapter.py)，实现：

```python
class RobotAdapter:
    def __init__(self, config): ...
    def start(self): ...            # Live 前调用，可选
    def observe(self) -> dict: ...  # 返回模型观测和动作基准
    def apply_action(self, row): ...
    def stop(self): ...             # 暂停/退出/故障调用，可选
```

配置模板：[`../../examples/deployment/python_robot_client.example.json`](../../examples/deployment/python_robot_client.example.json)。

### 7.1 `config.adapter`

| 字段 | 写法 |
|---|---|
| `entrypoint` | `module:ClassName` |
| `source_file` | Embodit 主机上的单个 `.py` 绝对路径；会上传到本体 |
| `source_path` | Adapter 已安装在本体时的本体绝对目录 |
| `module_search_paths` | 本体上的额外 Python import 目录 |
| `python_executable` | 本体 Python/虚拟环境 |
| `config` | 原样传给 Adapter 构造函数 |

`source_file` 文件名必须等于 `entrypoint` 的模块名。`source_file` 和 `source_path` 按实际安装方式选用。

### 7.2 观测与 Dry Run

| 字段 | 写法 |
|---|---|
| `default_prompt` | 默认任务 Prompt |
| `task_prompts` | 页面可选 Prompt，最多 1000 项 |
| `observation_map` | 将 Adapter 返回 key 改成模型输入 key |
| `dry_run_observation_source` | `synthetic` 或 `adapter` |
| `dry_run_observations` | 合成 Dry Run 输入 |

合成值：

```json
{"$synthetic":"vector","length":6,"value":0}
{"$synthetic":"image","width":224,"height":224,"channels":3,"value":0}
```

`synthetic` 不导入厂商 Adapter，不碰本体；`adapter` 使用真实只读 `observe()`，仍不调用 `apply_action()`。

### 7.3 动作安全 `config.action`

| 字段 | 写法 |
|---|---|
| `width` | 每行动作维度 |
| `horizon` | 模型返回行数 |
| `baseline_observation` | Adapter 观测中作为首步基准的 key |
| `minimum` / `maximum` | 与 `width` 等长的逐维绝对限位 |
| `max_step` | 与 `width` 等长的逐步变化限位 |
| `numerical_tolerance` | 边界浮点容差，标量或逐维数组；不是放宽限位 |

`minimum < maximum`，`max_step > 0`，所有数值必须有限。限位必须来自设备资料和受控实测。

### 7.4 调度 `config.control`

| 字段 | 写法 |
|---|---|
| `rate_hz` | Live 下发频率 |
| `dry_run_rate_hz` | Dry Run 推理频率 |
| `watchdog_timeout_s` | 单次循环超时 |
| `max_episode_steps` | 可选最大动作步数 |
| `inference_mode` | `synchronous` 或 `asynchronous` |
| `action_steps` | 每个 horizon 实际采用步数，`1..horizon` |
| `asynchronous.request_after_steps` | `1..action_steps-1` 或 `auto` |
| `asynchronous.latency_margin_ms` | 自动预取延迟余量，默认 `30` |

异步模式在当前动作块执行期间请求下一块；切换前会基于最新实测状态重新校验限位。迟到的推理不会触发追赶式突发下发。

### 7.5 页面观测 `config.telemetry`

| 字段 | 写法 |
|---|---|
| `cameras` | 最多 8 项 `{key,label}` |
| `state` | `{key,label,names,units}` |
| `action` | `{label,names,units}`；`names` 长度应等于 `width` |
| `max_image_bytes` | 单图上限，默认 750 KB，最大 5 MB |
| `history_seconds` | 无图像历史窗口 |
| `history_max_points` | 历史点数上限 |

支持 JPEG/PNG/WebP 和 `mono8/rgb8/bgr8/rgba8/bgra8` 原始图像。

## 8. 模型 Config 字段

顶层：

| 字段 | 写法 |
|---|---|
| `version` | 固定 `1` |
| `kind` | 固定 `model` |
| `config_id` | 唯一 ID，最长 64 |
| `name` | 页面显示名 |
| `host` | 模型运行主机 |
| `model` | Provider、Checkpoint 和进程配置 |
| `endpoint` | 模型监听对象，由以下 `bind/port` 组成 |
| `endpoint.bind` | 模型监听地址，默认 `127.0.0.1` |
| `endpoint.port` | 模型端口；组合时自动写入 tunnel 远端目标 |

### 8.1 `model` 通用字段

| 字段 | 写法 |
|---|---|
| `provider` | `python/openpi/lerobot/starvla/external` |
| `host` | 组件 Config 中省略或写 `model`；组合时固定为 `model` |
| `command` | 仅 `external` 使用的 argv；其他 Provider 禁止 |
| `workdir` | 模型端绝对工作目录 |
| `setup` | 启动前 source 的绝对路径数组 |
| `environment` | 如 `CUDA_VISIBLE_DEVICES` |
| `health` | 仅 `external` 使用，字段与 5.6 节相同 |
| `entrypoint` | `python` 必填，写 `module:ClassName` 或工厂函数 |
| `checkpoint` | 模型端路径或 Provider 支持的标识 |
| `python_executable` | 模型环境 Python |
| `load_method` / `predict_method` | Python 方法名，默认 `load` / `predict` |
| `load_kwargs` | 传给加载器 |
| `predict_kwargs` | 每次推理附加参数 |
| `action_horizon` | 可选；组合时覆盖本体 Client horizon |
| `maximum_request_bytes` | 请求体上限，默认 50 MB |
| `source_path` | 固定 Provider 源码的自定义绝对路径 |
| `startup_timeout_s` | 包含权重加载时间 |
| `restart` | `no/on-failure/always` |

`python/openpi/lerobot/starvla` 的 command、health 由 Embodit 生成，不要手填。

### 8.2 自定义 Python 模型

```python
class MyVLA:
    def load(self, checkpoint, **kwargs):
        self.model = YourModel.from_pretrained(checkpoint, **kwargs)

    def predict(self, observations, **kwargs):
        return self.model.predict(observations, **kwargs)
```

`entrypoint` 写成 `module:ClassName`。也可以指向工厂函数 `create_model(checkpoint, **load_kwargs)`。默认方法名是 `load` 和 `predict`，可用 `load_method/predict_method` 修改。

输入是 Robot Client 生成的字典；二进制图像会被恢复为 bytes。输出必须为有限数值二维数组 `[horizon][width]`。Embodit 负责 `/health`、`/infer`、序列化、大小限制和 systemd 托管。

### 8.3 OpenPI / LeRobot / StarVLA

| Provider | Checkpoint 要求 | 常用 `load_kwargs` |
|---|---|---|
| `openpi` | 官方目录通常可推断训练 config | 自训练或改名目录设置 `config_name` |
| `lerobot` | 完整 `save_pretrained` 目录和 processor metadata | 特殊 feature 使用 `observation_map` |
| `starvla` | 模型配置和归一化统计与权重同目录 | 多归一化域设置 `unnorm_key` |

`workdir` 默认指向包含 `third_party/models/<provider>` 的 Embodit checkout。Provider 环境必须按固定上游提交安装。

### 8.4 `external`

仅用于已有兼容推理服务。配置 `command`、`health` 和服务环境，且服务必须实现内部 `/health`、`/infer` 契约。普通接入优先使用其他 Provider。

## 9. Tunnel 与 Runtime

本体 Config 的 `tunnel`：

| 字段 | 写法 |
|---|---|
| `local_bind` | 本体监听地址，默认 `127.0.0.1` |
| `local_port` | Robot Client 访问端口 |
| `server_alive_interval_s` | SSH keepalive 间隔 |
| `server_alive_count_max` | 最大连续失败数 |
| `restart` | `on-failure` 或 `always` |
| `health_path` | 模型健康路径，默认 `/health` |
| `startup_timeout_s` | 隧道健康超时 |

组合后的 Recipe 还包含 `source_host`（固定 `robot`）、`destination_host`（固定 `model`）、`remote_bind`（来自 `endpoint.bind`）和 `remote_port`（来自 `endpoint.port`）。组件 Config 不手填这些字段。Embodit 在本体生成部署专用 Ed25519 key，只允许转发声明的模型端口，并维护独立 `known_hosts`。

`runtime`：

| 字段 | 写法 |
|---|---|
| `default_mode` | `dry_run` 或 `live` |
| `auto_rollback` | 启动失败是否逆序回滚 |
| `stop_model_on_exit` | 完整停止时是否停止模型 |
| `power_off_on_exit` | 退出时是否调用 `power_off` |
| `monitor_interval_s` | 组件状态轮询，默认 `2` |
| `component_failure_threshold` | 连续失败多少次确认故障，默认 `3` |

## 10. 只读预检与启动顺序

网页“预检”会实际执行只读检查：

- Recipe schema；
- 本地/SSH 主机连通性、用户和基础命令；
- system/user systemd manager 可读；
- 模型 workdir、Checkpoint/source、Python 可用；
- ROS setup 和 CLI 可用；
- ROS 已运行时，检查 graph、类型、频率和新鲜度；未运行时给 warning。

预检不会创建隧道、启动服务、上电或发送动作。正式启动仍按顺序强制复检：

```text
主机/systemd 预检
→ 隧道凭据
→ Model Runner + checkpoint
→ 模型直连 health
→ 本体 SSH tunnel + tunnel health
→ ROS Bringup
→ graph/type/rate/freshness
→ power_on
→ initial_pose + 实测容差
→ Robot Client
→ 首次完整推理
→ dry_run / running
```

任何一步失败都会记录原因，并在 `auto_rollback=true` 时逆序清理。

## 11. Dry Run、Live 与停止

- `dry_run`：执行观测、隧道、模型和动作校验，不向控制器/Adapter 发送动作；
- `live`：通过全部 readiness 后执行真实动作；
- 暂停：调用 hold，停止真实动作并保持模型/观测；
- 断开：停止 Client、ROS 和 tunnel，保留模型；
- 关闭：同时停止模型；
- 急停：优先调用 `robot.stop`，不等待模型或监控防抖。

软件停止不能替代硬件急停。正式实验前应在低速、可触达急停的环境测试：正常停止、hold、急停、Client 崩溃、模型超时、网络断开、ROS 故障和下电。

## 12. 日志与故障排查

远端 unit：

```text
embodit-model-<deployment-id>.service
embodit-tunnel-<deployment-id>.service
embodit-ros-<deployment-id>.service
embodit-client-<deployment-id>.service
```

网页可切换 Orchestration、Model、Tunnel、ROS、Client 日志。CLI：

```bash
bash embodit.sh logs -f
bash embodit.sh recipe-validate /tmp/my-deployment.json
```

| 失败位置 | 首要检查 |
|---|---|
| host/systemd | SSH 认证、host key、用户、system/user manager 权限 |
| model environment | workdir、Python、Checkpoint、CUDA、Provider 依赖 |
| model health | 权重加载日志、输入 schema、端口占用 |
| tunnel | 本体到模型 SSH 地址、端口、authorized key 限制 |
| ROS readiness | setup 顺序、Domain ID/Master URI、名称、类型、频率、header 时间 |
| initial pose | 关节顺序、单位、控制器状态、实测容差 |
| client safety | action shape、NaN/Inf、绝对限位、max_step、watchdog |

## 13. HTTP API

```text
GET/POST    /api/deploy/configs/{robot|model}
GET/DELETE  /api/deploy/configs/{robot|model}/{id}
POST        /api/deploy/configs/validate
POST        /api/deploy/compose
GET/POST    /api/deploy/recipes
POST        /api/deploy/recipes/validate
POST        /api/deploy/recipes/split
POST        /api/deploy/doctor
POST        /api/deploy/robot-connection
POST        /api/deploy/orchestrations/prepare-model
POST        /api/deploy/orchestrations/{id}/start-dry-run
POST        /api/deploy/orchestrations/{id}/start-evaluation
POST        /api/deploy/orchestrations/{id}/stop-evaluation
POST        /api/deploy/orchestrations/{id}/prompt
POST        /api/deploy/orchestrations/{id}/scheduler
POST        /api/deploy/orchestrations/{id}/poses
POST        /api/deploy/orchestrations/{id}/stop
POST        /api/deploy/orchestrations/{id}/emergency-stop
```

Config 用于复用，Recipe 用于执行；不要维护两套运行配置。
