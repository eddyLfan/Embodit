# 真机部署层使用指南

[English](README.md) · **中文**

本指南说明如何接入模型、本体和安全配置。Embodit 提供软件接入路径和自动回归测试，但这不代表某个本体、模型或 Checkpoint 已经完成安全验证。每种设备都必须在受控环境中重新确认 SDK/ROS 接口、单位、关节顺序、限位、生命周期操作和故障行为。

## 1. 先确认三个逻辑角色

部署前先确定 Embodit、本体和模型分别运行在哪台机器上：

```text
Embodit Host [Web UI / 配置 / 预检 / 编排 / 日志]
  ├─ local 或 SSH → Model Host [Model Runner / Checkpoint / GPU]
  └─ local 或 SSH → Robot Host
                        ├─ ROS Bringup 或厂商 SDK
                        ├─ Robot Client：采集 observation、校验 action、控制本体
                        └─ SSH local-forward → Model Host
```

- `local` 永远表示“与 Embodit 进程在同一台机器”，不是“在同一局域网”。
- `ssh` 表示 Embodit 通过 SSH 管理另一台机器。
- Robot Host 是运行 ROS/SDK 和 Robot Client 的计算节点；如果厂商控制器不能运行这些程序，应增加 Jetson/IPC 作为 Robot Host。
- Embodit 只做控制面，不进入实时控制闭环；Robot Client 和本体独立安全链路负责动作执行与安全。
- 当前推理链路统一由 Robot Host 主动建立到 Model Host 的 SSH 隧道。因此除了 Embodit 能管理目标主机，还必须满足 Robot Host → Model Host 的 SSH 网络可达性。

配置仍然分为两份可复用组件和一份运行配置：Robot Config、Model Config，以及组合生成的 Recipe v2。

## 2. 按设备情况选择部署模式

先按物理设备选择模式，再复制配置文件。下表中的 `local/ssh` 都是相对 Embodit Host 而言。

| 模式 | 物理位置 | `robot.host.connection` | `model.host.connection` | 适用情况 |
|---|---|---|---|---|
| A. 本体侧 Embodit + 云模型 | Embodit 与 Robot Host 同机；模型在云 GPU | `local` | `ssh` | 本体旁有 Jetson/IPC，模型使用百度云等云开发机；推荐的云模型部署 |
| B. 工作站本地模型 + 远端本体 | Embodit 与模型同工作站；Robot Host 是另一台机器 | `ssh` | `local` | 实验室工作站有 GPU，本体通过网线连接 |
| C. 三端分离 | Embodit、Robot Host、Model Host 各自独立 | `ssh` | `ssh` | 模型在独立 GPU 服务器，控制面保留在操作工作站 |
| D. 完全同机 | Embodit、Robot Host、Model Host 在同一台机器 | `local` | `local` | 本体计算机同时有足够 GPU/CPU；当前仍通过本机 SSH 隧道连接模型 |

### 2.1 模式 A：Embodit 与本体同机，模型在云端

这是“本体侧 IPC/Jetson 运行 Embodit，百度云等云开发机运行模型”的配置。

网络要求：本体侧机器能主动访问云模型机 SSH；云端不需要主动 SSH 进入本体。
如果 Embodit 运行在无显示器的 IPC/Jetson 上，从操作电脑通过 SSH 端口转发
或受保护的局域网访问 Web；不要把 Embodit Web 端口直接暴露到公网。

Robot Config 的 `host`：

```json
{
  "connection": "local",
  "address": "127.0.0.1",
  "user": "运行-Embodit-的本机用户",
  "service_manager": "user"
}
```

Model Config 的 `host`：

```json
{
  "connection": "ssh",
  "address": "云模型机域名或IP",
  "port": 22,
  "user": "model",
  "auth": {
    "type": "password_env",
    "environment_variable": "MODEL_SSH_PASSWORD"
  },
  "host_key_policy": "accept-new",
  "service_manager": "user"
}
```

只需设置模型 SSH 凭据：

```bash
export MODEL_SSH_PASSWORD='<model-password>'
```

### 2.2 模式 B：Embodit 与模型在工作站，本体独立

网络要求：Embodit 工作站能 SSH 到 Robot Host；Robot Host 也能 SSH 回 Embodit 工作站，以建立模型隧道。工作站需要 OpenSSH server。

Robot Config 的 `host`：

```json
{
  "connection": "ssh",
  "address": "本体或本体侧IPC的IP",
  "port": 22,
  "user": "robot",
  "auth": {
    "type": "password_env",
    "environment_variable": "ROBOT_SSH_PASSWORD"
  },
  "host_key_policy": "accept-new",
  "service_manager": "user"
}
```

Model Config 的 `host`：

```json
{
  "connection": "local",
  "address": "本体可访问的工作站局域网IP",
  "port": 22,
  "user": "运行-Embodit-的工作站用户",
  "service_manager": "user"
}
```

不要把这里的 `address` 写成 `127.0.0.1`；Robot Host 会使用这个地址 SSH 到工作站。只需设置本体 SSH 凭据，例如：

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
```

### 2.3 模式 C：Embodit、本体和模型三端分离

Robot Config 和 Model Config 都使用 `connection: ssh`。网络必须同时满足：

- Embodit Host → Robot Host SSH；
- Embodit Host → Model Host SSH；
- Robot Host → Model Host SSH。

Robot Config 使用模式 B 的 SSH `host`，Model Config 使用模式 A 的 SSH
`host`，分别填写两台目标主机的地址、用户和认证，然后设置对应凭据：

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
export MODEL_SSH_PASSWORD='<model-password>'
```

### 2.4 模式 D：Embodit、本体和模型完全同机

两份 Config 的 `host` 都使用 `connection: local`、`address: 127.0.0.1`、相同的本机用户，并删除 `auth`。

当前版本仍用 SSH local-forward 连接 Model Runner，因此还必须：

1. 在本机启用 OpenSSH server；
2. 将 Robot Config 的 `tunnel.local_port` 与 Model Config 的 `endpoint.port` 配成不同端口，例如 `8001` 和 `8000`；
3. 确认 GPU、ROS、Robot Client 和 Embodit 不会争抢关键资源。

模式 D 示例端口：

Robot Config：

```json
{"tunnel":{"local_bind":"127.0.0.1","local_port":8001}}
```

Model Config：

```json
{"endpoint":{"bind":"127.0.0.1","port":8000}}
```

如果设备资源紧张，优先使用模式 A，将模型移到独立 GPU 主机。

### 2.5 所有模式共同的设备要求

| 设备 | 必需条件 |
|---|---|
| Embodit Host | Linux、Python 3.10+、uv、OpenSSH client、可写项目目录 |
| Robot Host | Python 3、systemd/systemd-run、OpenSSH client、ssh-keygen/ssh-keyscan、ROS 或厂商 SDK、独立硬件限位与急停 |
| 远端 Robot Host | 在上述条件之外，还需 OpenSSH server，允许 Embodit 登录 |
| Model Host | systemd、Provider 运行环境、Checkpoint；远端模型还需 OpenSSH server |
| 本地目标 | Config 中的 `host.user` 必须等于运行 Embodit 的用户；使用 user systemd 时 `systemctl --user` 和 `systemd-run --user` 必须可用 |

模型 `endpoint.bind` 默认保持 `127.0.0.1`，不要为了跨机访问而改成公网监听；跨机访问由受限 SSH 隧道完成。

## 3. 按选定模式准备配置并启动

### 3.1 复制配置模板

```bash
mkdir -p config/local
chmod 700 config/local
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/my-model.json
chmod 600 config/local/my-robot.json config/local/my-model.json
```

模型模板按 Provider 选择：`python.example.json`、`openpi.example.json`、`lerobot.example.json` 或 `starvla.example.json`。仓库模板使用文档地址和 `/path/to/...` 占位符，不能原样运行。

页面只发现 `config/local/*.json`，两份文件必须直接放在该目录根部。子目录、仓库示例和 `.embodit_cache/deploy/configs/` 缓存不会出现在选择列表中。

### 3.2 修改 Robot Config

先按第 2 节替换 `host`，再逐项修改：

| 区域 | 必须按真实设备填写 |
|---|---|
| `robot.ros` | ROS 版本、distro、setup、Domain ID/Master URI |
| `robot.bringup` / `readiness` | 启动命令、节点、topic/service/action 类型、频率和新鲜度 |
| `power_on/power_off/hold/stop` | 厂商命令或 ROS service；没有时明确使用 `none` |
| `initial_pose` | 真实安全位姿、关节顺序、单位和容差 |
| `robot.client` | `ros2_standard`、`python_adapter` 或自定义 Client，以及观测映射和控制器 |
| `action.limits` | 设备真实绝对限位、逐步变化限位和动作维度 |
| `tunnel.local_port` | Robot Client 访问模型的本地端口；模式 D 必须避开模型端口 |

### 3.3 修改 Model Config

先按第 2 节替换 `host`，再填写 Provider、`workdir`、Checkpoint、Python 环境、加载参数和 `endpoint.port`。内置 OpenPI、LeRobot、StarVLA 首次使用前初始化固定源码：

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive
git submodule status --recursive
```

每个 Provider 使用独立 Python/CUDA 环境；安装和版本边界见 [`../../third_party/README.md`](../../third_party/README.md)。

### 3.4 组合、校验与运行

只导出所选模式实际需要的 `password_env` 变量，然后组合 Recipe：

```bash
bash embodit.sh recipe-compose \
  config/local/my-robot.json \
  config/local/my-model.json \
  --output /tmp/my-deployment.json

bash embodit.sh recipe-validate /tmp/my-deployment.json
bash embodit.sh start
```

进入“真机部署”后：

1. 选择本体和模型配置；
2. 运行只读预检；
3. 启动模型并等待 `/health`；
4. 填写 Prompt，连接本体并进入 Dry Run；
5. 检查模型输入、计划动作、执行动作、延迟和日志；
6. 所有检查通过后再进入 Live；
7. 使用暂停、断开、关闭或急停结束运行。

CLI 等价命令：

```bash
bash embodit.sh recipe-run /tmp/my-deployment.json --mode dry_run
bash embodit.sh recipe-run /tmp/my-deployment.json --mode live
bash embodit.sh recipe-stop /tmp/my-deployment.json
bash embodit.sh recipe-stop /tmp/my-deployment.json --emergency
```

Embodit 使用明文 HTTP 和 Bearer Token，不内置 TLS。不要把 Web 或模型端口直接暴露到公网；跨网访问使用可信私网/VPN、防火墙和带认证的 TLS 反向代理。启用局域网或真机控制前阅读[安全策略](../../SECURITY.md)。

## 4. 通用主机字段 `host`

本体 Config 和模型 Config 都包含 `host`：

| 字段 | 必填 | 写法 |
|---|---:|---|
| `connection` | 否 | `ssh`（默认）或 `local`；本体和模型都支持 `local` |
| `address` | 是 | SSH 地址；本地本体填写 `127.0.0.1`；本地模型且本体远端时，填写本体可访问的工作电脑地址 |
| `port` | 否 | SSH 端口，默认 `22` |
| `user` | 是 | 目标主机用户名；不能含空格或 `@` |
| `auth` | SSH 是 | 见下表；`local` 禁止填写 |
| `connect_timeout_s` | 否 | SSH 连接超时，默认 `8`，范围 `1..60` |
| `host_key_policy` | 否 | `accept-new` 或 `strict`；稳定环境建议 `strict` |
| `service_manager` | 否 | `system` 或 `user`；决定使用 system/user systemd |

SSH 认证三选一。

密钥：

```json
{"type": "key", "identity_file": "/home/user/.ssh/id_ed25519"}
```

环境变量密码：

```json
{"type": "password_env", "environment_variable": "ROBOT_SSH_PASSWORD"}
```

配置文件内明文密码：

```json
{"type": "password", "password": "..."}
```

| 字段 | 写法 |
|---|---|
| `type` | `key`、`password_env` 或 `password` |
| `identity_file` | `type=key` 时的 Embodit 主机私钥路径 |
| `environment_variable` | `type=password_env` 时的变量名 |
| `password` | `type=password` 时的明文密码 |

推荐 `key` 或 `password_env`。直接密码会保存在本地配置文件中；文件权限必须为 `0600`，且不能提交 Git。

`connection: local` 不配置 `auth`，并且只允许使用运行 Embodit 的当前用户。
本地模式中的命令、文件访问和 systemd 操作都以该用户身份直接执行。

## 5. 本体 Config 字段

顶层：

| 字段 | 写法 |
|---|---|
| `version` | 固定 `1` |
| `kind` | 固定 `robot` |
| `config_id` | 唯一 ID，`A-Z/a-z/0-9/_.-`，最长 64 |
| `name` | 页面显示名 |
| `host` | 本体本地/SSH 执行目标，见第 2、4 节 |
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

合成向量：

```json
{"$synthetic":"vector","length":6,"value":0}
```

合成图像：

```json
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

可从最小示例 [`my_vla.py`](../../examples/deployment/my_vla.py) 开始，或直接实现相同接口：

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

对于内置 OpenPI、LeRobot、StarVLA Provider，`workdir` 必须指向包含 `third_party/models/<provider>` 的 Embodit checkout。Provider 环境必须按固定上游提交安装。

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
| `default_mode` | 启动后请求的模式，可为 `dry_run` 或 `live`；`live` 不能绕过 Dry Run 解锁门控 |
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
→ dry_run
```

任何一步失败都会记录原因，并在 `auto_rollback=true` 时逆序清理。

## 11. 离线评测、Dry Run、Live 与停止

### 11.1 离线单帧评测

离线单帧评测是面向已录制数据集的纯模型检查。先单独准备模型，并等待 Orchestration 进入 `MODEL_READY`；此时本体观测/控制链路、ROS Bringup、Tunnel 和 Robot Client 都不应连接。选择数据集的 Episode 和帧后，Embodit 读取该帧已记录的 state、相机图像和任务 Prompt，直接请求已驻留模型，再将预测动作块与时间对齐后的记录 action 对比，返回逐维和整体误差指标。

该路径不会给本体上电，不会读取真实本体观测，不会启动控制 Client，也绝不会发送动作。单次模型推理超时为 30 秒。每个 Orchestration 一次只允许一个离线请求（single-flight）；并发请求或在请求未结束时切换其他运行模式会被拒绝。

离线评测不等于 Dry Run 或 Live：

- 离线单帧评测只读取一帧记录数据，且只与 `MODEL_READY` 模型服务交互；
- Dry Run 运行已配置的观测/Tunnel/模型/动作校验链路，可能持续使用合成观测或只读 Adapter 观测，但不发送控制器/Adapter 动作；
- Live 连接本体控制链路，并且只在全部 readiness 和解锁要求通过后执行真实动作。

### 11.2 Dry Run 到 Live 的解锁门控

所有 Orchestration 都从 Dry Run 启动，包括 `runtime.default_mode=live` 的 Recipe。Web 与 CLI 都只能在 Dry Run 就绪且最近动作安全检查通过后提升到 Live。网页将点击“进入 Live”作为明确的切换请求，不再要求额外手工输入短语；CLI 仍要求操作者在 60 秒有效期内原样输入服务端生成的一次性短语：

```text
LIVE <deployment_id> <6 位大写十六进制 token>
```

Web 只会从已就绪且安全检查通过的 Dry Run 发起 Live 切换。CLI 的 `recipe-run --mode live`（以及默认模式为 `live` 的 Recipe）会在启动任何服务前检查交互式 TTY，随后先等待 Dry Run，再打印 Challenge 并读取一整行精确输入；`--no-follow` 也必须完成确认后才退出。短语过期或不匹配时，Orchestration 保持 Dry Run。系统不存在绕过 Dry Run 直达 Live 的路径。

### 11.3 运行与停止行为

- `dry_run`：执行观测、隧道、模型和动作校验，不向控制器/Adapter 发送动作；
- `live`：通过全部 readiness 和 Dry Run 解锁门控后才执行真实动作；
- 暂停：调用 hold，停止真实动作并保持模型/观测；
- 断开：停止 Client、ROS 和 tunnel，保留模型；
- 关闭：同时停止模型；
- 急停：优先调用 `robot.stop`，不等待模型或监控防抖。

软件停止不能替代硬件急停。正式实验前应在低速、可触达急停的环境测试：正常停止、hold、急停、Client 崩溃、模型超时、网络断开、ROS 故障和下电。

## 12. 日志与故障排查

受管 systemd unit（可能位于本机或远端）：

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
| host/systemd | local/SSH 连接方式、SSH 认证与 host key、用户、system/user manager 权限 |
| model environment | workdir、Python、Checkpoint、CUDA、Provider 依赖 |
| model health | 权重加载日志、输入 schema、端口占用 |
| tunnel | 本体到模型 SSH 地址、端口、authorized key 限制 |
| ROS readiness | setup 顺序、Domain ID/Master URI、名称、类型、频率、header 时间 |
| initial pose | 关节顺序、单位、控制器状态、实测容差 |
| client safety | action shape、NaN/Inf、绝对限位、max_step、watchdog |

## 内部 HTTP 路由

Web UI 与 `embodit.sh` 是受支持的操作入口。`/api/...` 路由属于 pre-1.0
内部实现，可能不提供兼容保证；请勿基于这些路由构建外部控制客户端。
