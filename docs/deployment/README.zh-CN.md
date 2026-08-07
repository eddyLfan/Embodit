# 真机部署层详细指南

[English](README.md) · **中文**

配置管理采用“本体配置 + 模型配置”的组合方式：两部分独立保存、版本化和复用，测试时各选择一份，Embodit 再生成运行所需的 Recipe。模型既可以位于独立 SSH 主机，也可以直接位于运行 Embodit 的机器；本体建立到模型端的 SSH Local Forward，ROS、本体生命周期操作、初始位姿和 Robot Client 全部在本体端运行。Embodit 是控制面，不进入实时观测/动作数据链路。

## 1. 运行架构

```text
Workstation [Embodit]
  ├─ SSH → Model Host [Inference Service :8000]
  └─ SSH → Robot Host [SSH Tunnel + ROS Bringup + Robot Client]
                         └─ localhost:8000 → Model Host:8000
```

Embodit 与模型端同机时，模型管理链路改为本地执行，不需要“本机 SSH 回连本机”；实时数据链路保持不变：

```text
Embodit + Local Model [Inference Service :8000]
  └─ SSH → Robot Host [SSH Tunnel + ROS Bringup + Robot Client]
                         └─ localhost:8000 → Embodit Host:8000
```

本体仍通过部署专用密钥连接模型端 SSH 服务来建立受限端口转发。因此 `host.address`/`port` 必须是本体可访问的 Embodit 地址和 SSH 端口；`host.user` 必须与 Embodit 进程的运行用户一致。模型进程、文件、systemd unit 和日志都归该用户管理。

远端长期进程使用 systemd transient unit 托管：

```text
embodit-model-<deployment-id>.service
embodit-tunnel-<deployment-id>.service
embodit-ros-<deployment-id>.service
embodit-client-<deployment-id>.service
```

工作电脑短暂断开不会直接终止这些服务。隧道和进程按 Recipe 的策略重启；Live 模式下 Client 会被强制设为 `restart=no`，动作校验或本体调用故障后不会自行恢复真实动作。

## 2. 用户与工具的责任边界

| 用户/本体集成方提供 | Embodit 提供 |
|---|---|
| SSH 可达的本体，以及独立模型主机或 Embodit 本机地址、ROS setup 路径 | 本地/SSH 执行、主机校验、进程托管、日志、顺序启动与回滚 |
| 常用模型 Provider 与 Checkpoint，或自定义 Python 入口/高级外部服务 | OpenPI/LeRobot/StarVLA 适配器、Model Runner、localhost HTTP 协议、健康检查、Tensor/JSON 归一化 |
| ROS topic/service/action 映射和本体专用限位 | graph/type/rate/freshness readiness 与内置 ROS2 Client 校验 |
| 厂商驱动/Bridge、硬件限位、急停、安全上下电/hold/stop 行为 | 动作校验、执行日志、暂停/继续、goal 取消、配置的 hold/stop/power-off 调用 |

Embodit 无法自动推断安全关节限位、初始位姿、控制频率或生命周期服务语义。这些值必须来自厂商资料，并在可触达硬件急停、低速条件下验证。软件 stop 不能替代经过认证的硬件安全系统。

## 3. 分离配置与自由组合

网页工作台默认管理两类组件配置：

- 本体配置（`version: 1, kind: robot`）：本体 SSH、ROS、Bringup、readiness、上下电/hold/stop、初始位姿、Robot Client、安全限位、本体侧 tunnel 和退出策略；
- 模型配置（`version: 1, kind: model`）：模型端的本地/SSH 连接、Provider、Checkpoint、运行环境和模型端点；只有自定义 `python` Provider 需要入口；
- 组合信息：仅包含本次运行的 `deployment_id` 和名称。同一份本体配置可以依次搭配多个模型，同一模型也可以在多个本体上做兼容性测试。

可直接复制 [`../../config/deployment/robot.example.json`](../../config/deployment/robot.example.json) 和 [`../../config/deployment/models/python.example.json`](../../config/deployment/models/python.example.json) 开始配置；常用模型另有 [OpenPI](../../config/deployment/models/openpi.example.json)、[LeRobot](../../config/deployment/models/lerobot.example.json) 和 [StarVLA](../../config/deployment/models/starvla.example.json) 模板。

两类配置内部不需要管理 `robot` / `model` 主机引用。组合时 Embodit 自动建立规范别名、把本体 tunnel 接到所选模型端点，并执行完整 Recipe 交叉校验。保存目录按类型隔离，配置文件和目录权限仍分别为 `0600`、`0700`。

```json
{
  "version": 1,
  "kind": "model",
  "config_id": "my-vla",
  "name": "My VLA",
  "host": {"connection": "local", "address": "192.168.10.10", "user": "root", "service_manager": "system"},
  "model": {"provider": "python", "entrypoint": "my_vla:MyVLA", "checkpoint": "/root/checkpoints/my-vla", "workdir": "/root/vla"},
  "endpoint": {"bind": "127.0.0.1", "port": 8000}
}
```

工作台可导入单个本体/模型配置、包含两者的组合包，也能导入旧的完整 Recipe 并自动拆分。导出组合会同时保留两份源配置和生成后的 Recipe，便于复现测试。

### 3.1 完整 Recipe

底层编排器继续使用 [`../../config/deployment/recipe.example.json`](../../config/deployment/recipe.example.json) 所示的完整 Recipe。主要部分为：

- `hosts.model`、`hosts.robot`：管理连接、地址、端口、用户、认证、host-key 策略、system/user service manager；模型端支持 `connection: local`，本体端必须为 `ssh`；
- `model`：Python 入口、Checkpoint、解释器、工作目录、环境，或高级外部命令；
- `tunnel`：本体监听地址/端口、模型目标、SSH keepalive 与重启策略；
- `robot.ros`：ROS 1/2、setup、Domain ID/Master URI、RMW；
- `robot.bringup`：厂商驱动和控制器启动命令；
- `robot.readiness`：必需 node 和带类型的 topic/service/action、topic 频率与新鲜度；
- 生命周期操作：`power_on`、`power_off`、`hold`、`stop`；
- `initial_pose`：FollowJointTrajectory 或自定义命令，以及实测容差；
- `robot.client`：标准 ROS2 的 `ros2_standard`、非标准 SDK 的通用 `python_adapter`，或高级自定义命令；
- `runtime`：默认模式、回滚、停止模型、下电策略，以及组件监控间隔与连续失败阈值。

认证既可以直接通过配置管理，也可以使用环境变量或密钥：

```json
{"type":"password","password":"..."}
{"type":"password_env","environment_variable":"ROBOT_SSH_PASSWORD"}
{"type":"key","identity_file":"/home/user/.ssh/id_ed25519"}
```

`connection` 省略时默认是 `ssh`，与旧配置完全兼容。模型与 Embodit 同机时使用：

```json
{
  "connection": "local",
  "address": "192.168.10.10",
  "port": 22,
  "user": "embodit",
  "service_manager": "user"
}
```

本地连接不配置 `auth`；这里的地址、端口和用户仍供本体建立模型隧道使用。`service_manager: user` 不需要系统级 systemd 权限；若使用 `system`，启动 Embodit 的用户必须有管理 system unit 的权限。

保存的 Recipe 文件和目录权限分别为 `0600`、`0700`。密码不会进入 SSH argv、编排日志或下载的 manifest。配置文件中的密码仍属于秘密，应仅本地保存、限制权限并排除出 Git。

## 4. Checkpoint 即用模型

模型主机一次性初始化固定版本的子仓库并按各上游说明创建独立环境后，OpenPI、LeRobot 和 StarVLA 部署只需选择 Provider、填写 Checkpoint；无需 `entrypoint`、启动命令、`/health` 或 `/infer`：

```json
{
  "model": {
    "provider": "lerobot",
    "checkpoint": "/root/checkpoints/lerobot-policy",
    "workdir": "/root/Embodit",
    "python_executable": "/root/miniconda3/envs/lerobot/bin/python"
  }
}
```

网页中的“常用模型 + Checkpoint”栏可生成这部分配置。`workdir` 指向模型主机上的 Embodit checkout，适配器会从其 `third_party/models/<provider>` 固定 gitlink 加载上游代码。若 checkout 在别处，可设置绝对 `source_path`。第三方来源、固定 commit、许可证边界、初始化和升级审查流程见 [`../../third_party/README.md`](../../third_party/README.md)。

Checkpoint 必须包含对应上游正常推理所需的元数据：LeRobot 需要完整 `save_pretrained` 目录；StarVLA 需要模型配置与归一化统计；OpenPI 官方目录通常可推断训练 config，自训练或重命名目录需补充 `load_kwargs.config_name`。这三项是 checkpoint 格式信息，不需要用户实现代码入口。

### 4.1 自定义 Python 模型接口

使用默认 `python` provider 时，用户只需在模型服务器提供模型类和 Checkpoint：

```python
class MyVLA:
    def load(self, checkpoint, **kwargs):
        self.model = YourModel.from_pretrained(checkpoint, **kwargs)

    def predict(self, observations, **kwargs):
        return self.model.predict(
            image=observations["wrist_camera"],
            state=observations["joint_position"],
            **kwargs,
        )
```

入口也可以是工厂函数 `create_model(checkpoint, **load_kwargs) -> model`，返回对象实现 `predict`。

输入契约：

- observation key 与 `robot.client.config.observations` 完全对应；
- `JointState` 按配置的 `joints` 顺序变成 Python list；模型和 Client 动作校验仍应将非有限传感器输入视为故障；
- `Image`/`CompressedImage` 变成字典，包含已解码的 `data: bytes`、encoding、时间戳/frame ID，原始 Image 还包含尺寸；
- 每次调用会附加 `predict_kwargs`。

输出契约：

- 返回形状严格为 `[horizon][joint_count]` 的 Python 二维 list、NumPy array 或 Torch tensor；
- 数值必须有限，并与 controller action 使用相同单位和关节顺序；
- 内置 Client 会拒绝 horizon 或关节维度不符的输出，不自动 reshape 或重新排列。

Recipe 示例：

```json
{
  "model": {
    "host": "model",
    "provider": "python",
    "entrypoint": "my_vla:MyVLA",
    "checkpoint": "/root/checkpoints/my-vla",
    "workdir": "/root/vla",
    "python_executable": "/root/miniconda3/envs/vla/bin/python",
    "environment": {"CUDA_VISIBLE_DEVICES": "0"},
    "load_kwargs": {},
    "predict_kwargs": {},
    "startup_timeout_s": 180,
    "restart": "on-failure"
  }
}
```

`entrypoint`、`checkpoint`、`workdir` 和解释器均在模型服务器解析。Embodit 上传通用 Model Runner，在模型服务器 localhost 绑定 `/health`、`/infer`，限制请求大小（默认 50 MB）、解码二进制观测、归一化 Tensor/array、报告异常并托管进程。内部 HTTP 端点不是普通用户的接入面。

仅已有独立推理服务时设置 `provider: external`；此时用户负责命令、健康检查和兼容的推理协议。最小模板见 [`../../examples/deployment/my_vla.py`](../../examples/deployment/my_vla.py)。

## 5. 隧道认证自动化

首次启动时 Embodit 会：

1. 使用配置的工作电脑凭据登录本体；模型为独立主机时登录模型服务器，为本机时直接执行；
2. 在本体 `~/.embodit/deployments/<id>/keys/` 生成部署专用 tunnel key；
3. 通过模型管理连接安装公钥；本机模型直接写入当前用户，独立模型通过 SSH 写入；
4. 将该 authorized key 限制为只能转发 Recipe 声明的模型端口；
5. 在本体写入部署专用 `known_hosts`；
6. 使用 `ExitOnForwardFailure` 和配置的 keepalive 启动 SSH。

因此远端场景只需配置工作电脑能够访问的两端凭据；本机模型不需要管理连接凭据。两种场景都不再手工安装“本体 → 模型服务器”密钥。

## 6. 精确启动与 readiness 标准

启动状态机固定为：

```text
SSH/systemd 预检
→ 协调隧道凭据
→ 启动 Model Runner 并通过 Provider 加载 checkpoint
→ 模型直连 health
→ 本体侧 SSH 隧道
→ 经本体 localhost 隧道检查模型 health
→ ROS Bringup
→ ROS graph/type/freshness/rate readiness
→ 上电
→ 初始位姿与实测容差检查
→ Robot Client
→ 首次完整推理 readiness
→ dry_run/running
```

Readiness 是契约，不是固定 sleep：

| 检查 | 通过标准 |
|---|---|
| 连接/systemd | SSH 主机接受配置的登录方式；本地模型用户与 Embodit 运行用户一致；各端能够管理 transient unit |
| Python 模型 | 模型加载后 Runner `/health` 返回 2xx，且不报告 `ready:false`/`ok:false` |
| 隧道 | 通过 `robot localhost:<local_port>` 能完成相同模型 health |
| ROS node | 所有配置的 node 名准确出现在 graph 中 |
| Topic/service/action | 所有配置的名称存在，且类型与配置完全一致 |
| Topic 频率 | `sample_seconds` 内解析出的平均频率 ≥ `minimum_rate_hz`；设为 `0` 时不检查频率 |
| ROS2 新鲜度 | 收到一条消息，且 header 时间戳在 ±`maximum_age_ms` 内；无 header 类型配置该项会失败 |
| ROS1 新鲜度 | 至少收到一条消息；当前 ROS1 不计算 header age |
| 初始位姿 | `JointState` 包含全部指定关节，最大绝对位置误差 ≤ `tolerance`（默认 0.03） |
| 内置 Client | 状态 topic 在观测采集、隧道推理、响应解析、动作校验全部成功后报告 `ready` |

Readiness 每隔 `interval_s` 重试，直至 `timeout_s`。类型缺失、topic 过期/低频、位姿超差或 Client 报告 `fault` 都会让启动失败，随后按配置逆序回滚。

## 7. 内置 ROS2 Client 安全标准

`builtin: ros2_standard` 支持 `sensor_msgs/msg/JointState`、`sensor_msgs/msg/Image`、`sensor_msgs/msg/CompressedImage`，通过 `control_msgs/action/FollowJointTrajectory` 发送动作。生成后的 Client 配置会自动上传，示例见 [`../../examples/deployment/ros2_robot_client.example.json`](../../examples/deployment/ros2_robot_client.example.json)。

Live 发送任何 goal 之前，Client 强制执行：

- 所有配置观测在 `observation_timeout_s`（默认 3 秒）内到达，本地接收年龄 ≤ `maximum_observation_age_ms`（默认 500 ms）；
- 推理 HTTP timeout 取模型 timeout（默认 10 秒）和 watchdog timeout（默认 1 秒）的较小值；
- 响应体不超过 `maximum_response_bytes`（默认 10 MB），且是有效 JSON；
- action 形状严格等于 horizon × joint 数，只含有限数值；
- 各维位于对应关节绝对 `minimum`/`maximum` 内；
- horizon 第一帧相对配置的 baseline observation 不超过 `max_step`，后续每帧相对上一帧不超过 `max_step`；
- FollowJointTrajectory server 在超时内接受 goal；
- 单次完整 loop 不超过 `watchdog_timeout_s`；任一异常都会取消已跟踪 goal、发布 `fault` 并退出。

这些是 Robot Client 边界的软件保护。本体 ROS Bridge/厂商控制器仍必须独立执行硬件限位、命令有效期、控制器状态、碰撞/速度限制和急停逻辑。对于非标准 SDK，应实现薄 ROS Bridge，将状态、轨迹、上下电、hold、stop 和错误码映射为配置的 ROS 契约。

### 7.1 通用 Python Robot Adapter

当厂商 SDK 不适合转换为标准 ROS 接口时，使用 `builtin: python_adapter`。Embodit 仍负责上传通用 Client、模型 HTTP 协议、任务 Prompt、Dry Run、动作校验、频率、readiness、日志和故障退出；本体集成方只提供一个薄对象：

```python
class RobotAdapter:
    def __init__(self, config): ...
    def start(self): ...                 # 可选；仅 Live 调用
    def observe(self) -> dict: ...       # 返回模型观测与动作 baseline
    def apply_action(self, row): ...     # 接收一帧已校验动作
    def stop(self): ...                  # 可选；退出/故障时调用
```

配置中的 `adapter.entrypoint` 使用 `module:ClassName`。适配器已安装在本体时填写本体绝对目录 `source_path`；使用 Embodit 主机上的单文件薄适配器时填写绝对路径 `source_file`，编排器会自动上传并改写运行目录。`python_executable` 在本体上解析。`dry_run_observations` 支持普通 JSON，以及 `{"$synthetic":"vector","length":N}`、`{"$synthetic":"image","width":W,"height":H,"channels":3}`。Dry Run 不导入 Adapter、不初始化厂商 SDK，只跑合成观测 → 隧道 → 模型 → 动作校验。

`action` 统一声明 `width`、`horizon`、`baseline_observation`、逐维 `minimum`/`maximum`/`max_step`。`numerical_tolerance` 可以是标量或与 `width` 等长的数组；只对处于容差内的边界噪声钳到合法范围，其他维度仍保持严格限位。Live 中每个动作块都先完成形状、有限值、绝对限位和相邻步长校验，之后才逐帧调用 `apply_action`；某个动作块未通过时，Client 丢弃该块、保持最新实测关节状态并继续请求下一次推理，不再因为单次模型越界退出。`control.watchdog_timeout_s` 约束单次调用耗时。模板见 [`../../examples/deployment/python_robot_client.example.json`](../../examples/deployment/python_robot_client.example.json)，薄 Adapter 接口见 [`../../examples/deployment/python_robot_adapter.py`](../../examples/deployment/python_robot_adapter.py)。

`action.horizon` 是模型每次必须输出的动作步数，`control.action_steps` 是 Embodit 从每个动作块实际采用的步数（默认采用整个 horizon），`control.rate_hz` 是 Live 动作下发频率。`control.inference_mode` 可选 `synchronous` 或 `asynchronous`：同步模式执行完当前动作块后才请求下一次推理；异步模式在执行到 `control.asynchronous.request_after_steps` 时启动下一次推理，同时继续执行当前块剩余动作。`request_after_steps` 可填写固定步数，也可设为 `"auto"`；自动模式根据最近一次模型往返延迟、控制频率和 `latency_margin_ms` 动态保留足够的剩余动作。异步结果在切块前会使用切换时的最新本体状态再次执行绝对限位与 `max_step` 校验；如果推理晚于当前块结束，调度时钟从当前时刻恢复，不会为追赶旧 deadline 突发连发动作。典型 Pi 配置可使用 `horizon: 50`、`action_steps: 50`、`request_after_steps: 30`，或使用 `"auto"` 适配不同推理端。

Adapter-backed Dry Run 会持续采样和推理。单次动作越界会记录为 `dryRunSafety.passed: false`，但不会关闭 Client 或常驻模型。它保留为只读诊断工具，不再是每次真机评测前的强制步骤。

可选的 `telemetry` 只描述模型 I/O 的显示方式：`cameras` 指定实际送入模型的图像 key 与名称，`state` 指定状态向量 key、逐维名称和单位，`action` 指定模型输出动作的逐维名称和单位。Embodit 从同一份 `observe()` 结果和已校验 action chunk 生成有界预览，不额外读取厂商状态，也不展示 ROS、隧道或主机信息。`history_seconds`（默认 20 秒）和 `history_max_points` 控制无图像历史缓存；网页可分别观察最近的输入关节状态、模型计划动作、实际下发动作和当前动作块。图像支持 JPEG/PNG/WebP，以及 `mono8`/`rgb8`/`bgr8`/`rgba8`/`bgra8` 原始帧；单图默认最大 750 KB，最多 8 路。

## 8. Dry Run、Live、回滚与停止

Dry Run 使用最终的观测 → 隧道 → 模型 → 校验链路，但不调用轨迹控制器，可用于首次接入或故障诊断。

日常实验中，模型就绪后可直接开始真机评测，无需重复 Dry Run 或输入 challenge 短语。暂停评测只停止真实动作并恢复只读观测，模型、隧道和 ROS 保持常驻；点击继续即可快速恢复。切换 Prompt 只重启轻量 Client，不重新加载模型。`task_prompts` 可在本体 Client 配置中声明常用 Prompt 列表。

“实验位姿”记录的只是模型输入状态向量对应的关节。回位时 Embodit 自动暂停评测，通过同一个通用 Adapter 按配置的 `max_step` 插值移动，完成后保持模型和只读观测运行。

启动失败或监控确认组件持续故障时，`auto_rollback` 按逆序停止组件，并可调用配置的 hold/power-off。运行期默认每 2 秒读取一次结构化 systemd 状态，连续 3 次异常才确认故障；单次 SSH 探测抖动或 systemd 自动重启过渡不会立即终止部署，恢复时会记录 `component_recovered` 事件。可用 `runtime.monitor_interval_s` 和 `runtime.component_failure_threshold` 调整，但不建议把阈值设为 1。探测失败与组件实际退出会生成不同错误信息。急停不经过该防抖，仍首先在本体调用 `robot.stop`，不等待模型。由于最终行为取决于厂商实现，正式使用前必须在受控环境验证正常停止、hold、急停、进程崩溃、网络断开和下电。

## 9. 网页与 CLI 使用

```bash
bash embodit.sh start
```

进入“真机部署”，选择本体和模型并启动模型，然后选择 Prompt 后直接点击“开始评测”。动作调度区可在同步、异步固定预取和异步自动预取之间切换，切换只重启轻量 Client。左下执行日志可切换编排、Client、Model、Tunnel 和 ROS，并支持筛选、跟随和清屏；右侧只展示实际模型输入、最近状态/计划/执行轨迹及逐维输出。评测可暂停/继续、快速切换 Prompt，并可记录模型关节位姿后执行一键回位。“断开”只停止本体侧 Client、ROS 与隧道并保留模型；“关闭”同时结束模型服务。

CLI/救援命令：

```bash
bash embodit.sh recipe-compose \
  config/deployment/robot.example.json \
  config/deployment/models/python.example.json \
  --deployment-id robot-a--my-vla \
  --output /tmp/robot-a--my-vla.json
bash embodit.sh recipe-validate config/my-robot.json
bash embodit.sh recipe-run config/my-robot.json --mode dry_run
bash embodit.sh recipe-run config/my-robot.json --mode live
bash embodit.sh recipe-stop config/my-robot.json
bash embodit.sh recipe-stop config/my-robot.json --emergency
```

`recipe-run --no-follow` 会在 readiness 完成后退出，远端 systemd 服务继续运行。

## 10. ROS 泛化边界

节点名只作为 readiness 信号，功能适配由带类型的 topic、service、action 定义。Recipe 编排支持 ROS1 和 ROS2；`ros2_standard` 与 FollowJointTrajectory 初始位姿只支持 ROS2。ROS1 或非标准 SDK 可使用厂商无关的 `python_adapter` 契约；ROS1 readiness 不接受 `actions` 声明，应检查对应 actionlib topics。

切换本体 SDK 时，通常只应修改 Recipe 和薄 ROS Bridge；SSH 拓扑、隧道、模型 Provider、生命周期状态机和 UI 保持不变。

## 11. HTTP API

```text
GET/POST    /api/deploy/recipes
GET/DELETE  /api/deploy/recipes/{id}
POST        /api/deploy/recipes/validate
POST        /api/deploy/recipes/split
GET/POST    /api/deploy/configs/{robot|model}
GET/DELETE  /api/deploy/configs/{robot|model}/{id}
POST        /api/deploy/configs/validate
POST        /api/deploy/compose
POST        /api/deploy/doctor
POST        /api/deploy/orchestrations
POST        /api/deploy/orchestrations/prepare-model
GET         /api/deploy/orchestrations/{id}
POST        /api/deploy/orchestrations/{id}/start-evaluation
POST        /api/deploy/orchestrations/{id}/stop-evaluation
POST        /api/deploy/orchestrations/{id}/prompt
POST        /api/deploy/orchestrations/{id}/poses
POST        /api/deploy/orchestrations/{id}/poses/{pose_id}/move
DELETE      /api/deploy/orchestrations/{id}/poses/{pose_id}
POST        /api/deploy/orchestrations/{id}/arm-challenge
POST        /api/deploy/orchestrations/{id}/start-live
POST        /api/deploy/orchestrations/{id}/stop
POST        /api/deploy/orchestrations/{id}/emergency-stop
POST        /api/deploy/orchestrations/{id}/logs
POST        /api/deploy/orchestrations/{id}/components/{component}/restart
GET         /api/deploy/orchestrations/{id}/manifest
```

本体/模型 Config 是配置管理接口；组合产物 Recipe 和上述 Orchestration API 仍是唯一运行协议。
