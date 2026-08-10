# Robot Deployment Guide

**English** · [中文](README.zh-CN.md)

This guide covers model, robot, and safety integration. Embodit provides software integration paths and automated regression coverage, but that is not evidence that a particular robot, model, or checkpoint is safe or validated. Commission every device in a controlled environment and explicitly verify its SDK/ROS interfaces, units, joint order, limits, lifecycle operations, and failure behavior.

## 1. Identify the three logical roles

Before editing configuration, decide which physical machine runs each role:

```text
Embodit Host [Web UI / config / preflight / orchestration / logs]
  ├─ local or SSH → Model Host [Model Runner / checkpoint / GPU]
  └─ local or SSH → Robot Host
                        ├─ ROS bring-up or vendor SDK
                        ├─ Robot Client: observe, validate actions, control robot
                        └─ SSH local-forward → Model Host
```

- `local` always means the exact machine running the Embodit process, not merely another host on the same LAN.
- `ssh` means Embodit manages a different machine over SSH.
- Robot Host means the compute node running ROS/SDK and Robot Client. If the vendor controller cannot run these processes, add a Jetson or IPC and treat that computer as Robot Host.
- Embodit is only the control plane. Robot Client and the robot's independent safety chain own execution and safety.
- Robot Host currently initiates a restricted SSH tunnel to Model Host for every deployment. In addition to Embodit reaching managed targets, Robot Host must therefore reach Model Host over SSH.

Configuration consists of reusable Robot and Model Configs plus the Recipe v2 composed from them.

## 2. Choose a deployment mode from the available devices

Choose the physical layout first, then copy templates. The `local/ssh` values below are relative to Embodit Host.

| Mode | Physical placement | `robot.host.connection` | `model.host.connection` | Use when |
|---|---|---|---|---|
| A. Robot-side Embodit + cloud model | Embodit and Robot Host share one machine; model runs on a cloud GPU | `local` | `ssh` | A Jetson/IPC is beside the robot and inference runs on a cloud development host; recommended cloud layout |
| B. Workstation model + separate robot | Embodit and model share a workstation; Robot Host is separate | `ssh` | `local` | The lab workstation has a GPU and connects to the robot over Ethernet |
| C. Three separate hosts | Embodit, Robot Host, and Model Host are all separate | `ssh` | `ssh` | Inference runs on a separate GPU server while the control plane stays on the operator workstation |
| D. Fully colocated | All three roles share one machine | `local` | `local` | The robot computer has enough GPU/CPU; the current implementation still uses a localhost SSH tunnel |

### 2.1 Mode A: Embodit and robot colocated, model in the cloud

Use this for Embodit on a robot-side IPC/Jetson and a model on a cloud development host such as Baidu Cloud.

Network requirement: the robot-side machine can initiate SSH to the cloud model
host. The cloud host does not need to initiate SSH into the robot network. When
Embodit runs on a headless IPC/Jetson, access the Web UI from the operator
computer through SSH port forwarding or a protected LAN; never expose the Web
port directly to the public Internet.

Robot Config `host`:

```json
{
  "connection": "local",
  "address": "127.0.0.1",
  "user": "user-running-embodit",
  "service_manager": "user"
}
```

Model Config `host`:

```json
{
  "connection": "ssh",
  "address": "cloud-model-hostname-or-ip",
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

Only model SSH credentials are needed:

```bash
export MODEL_SSH_PASSWORD='<model-password>'
```

### 2.2 Mode B: Embodit and model on a workstation, robot separate

Network requirements: Embodit workstation can SSH to Robot Host, and Robot Host can SSH back to the workstation for the model tunnel. The workstation needs an OpenSSH server.

Robot Config `host`:

```json
{
  "connection": "ssh",
  "address": "robot-or-robot-side-ipc-ip",
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

Model Config `host`:

```json
{
  "connection": "local",
  "address": "workstation-lan-ip-reachable-from-robot",
  "port": 22,
  "user": "user-running-embodit",
  "service_manager": "user"
}
```

Do not use `127.0.0.1` for this `address`: Robot Host uses it to SSH into the workstation. Only robot SSH credentials are needed, for example:

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
```

### 2.3 Mode C: Embodit, robot, and model on separate hosts

Both configs use `connection: ssh`. The network must allow all three paths:

- Embodit Host → Robot Host SSH;
- Embodit Host → Model Host SSH;
- Robot Host → Model Host SSH.

Use the Mode B SSH `host` for Robot Config and the Mode A SSH `host` for Model
Config. Set the address, user, and authentication for both targets, then export
both credentials:

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
export MODEL_SSH_PASSWORD='<model-password>'
```

### 2.4 Mode D: Embodit, robot, and model fully colocated

Both Config `host` objects use `connection: local`, `address: 127.0.0.1`, the same local user, and no `auth`.

The current release still reaches Model Runner through an SSH local-forward. Therefore:

1. enable an OpenSSH server on the local machine;
2. use different values for Robot Config `tunnel.local_port` and Model Config `endpoint.port`, for example `8001` and `8000`;
3. verify that Embodit, GPU inference, ROS, and Robot Client cannot starve one another of critical resources.

Example Mode D ports:

Robot Config:

```json
{"tunnel":{"local_bind":"127.0.0.1","local_port":8001}}
```

Model Config:

```json
{"endpoint":{"bind":"127.0.0.1","port":8000}}
```

If the device has limited resources, prefer Mode A and move inference to a separate GPU host.

### 2.5 Requirements shared by all modes

| Device | Requirements |
|---|---|
| Embodit Host | Linux, Python 3.10+, uv, OpenSSH client, writable project directory |
| Robot Host | Python 3, systemd/systemd-run, OpenSSH client, ssh-keygen/ssh-keyscan, ROS or vendor SDK, independent hardware limits and emergency stop |
| Remote Robot Host | Above requirements plus an OpenSSH server that accepts the Embodit login |
| Model Host | systemd, provider runtime, checkpoint; a remote model also needs an OpenSSH server |
| Local target | Config `host.user` equals the user running Embodit; for user systemd, both `systemctl --user` and `systemd-run --user` work |

Keep model `endpoint.bind` on `127.0.0.1`. Do not expose it publicly to make cross-host access work; use the restricted SSH tunnel.

## 3. Prepare configuration for the selected mode

### 3.1 Copy templates

```bash
mkdir -p config/local
chmod 700 config/local
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/my-model.json
chmod 600 config/local/my-robot.json config/local/my-model.json
```

Choose the model template for the provider: `python.example.json`, `openpi.example.json`, `lerobot.example.json`, or `starvla.example.json`. Committed templates contain documentation addresses and `/path/to/...` placeholders and are not runnable unchanged.

The Web workspace only discovers `config/local/*.json`; keep both files directly in that directory. Nested files, committed examples, and `.embodit_cache/deploy/configs/` cache entries do not appear in the selectors.

### 3.2 Edit Robot Config

Replace `host` according to section 2, then edit every device-specific value:

| Area | Required device-specific values |
|---|---|
| `robot.ros` | ROS version, distro, setup, Domain ID/Master URI |
| `robot.bringup` / `readiness` | launch command, nodes, topic/service/action types, rates, freshness |
| `power_on/power_off/hold/stop` | vendor command or ROS service; explicitly use `none` when unavailable |
| `initial_pose` | verified safe pose, joint order, units, tolerance |
| `robot.client` | `ros2_standard`, `python_adapter`, or custom Client; observation mapping and controller |
| `action.limits` | real absolute limits, per-step limits, action dimensions |
| `tunnel.local_port` | robot-local model access port; in Mode D it must differ from the model port |

### 3.3 Edit Model Config

Replace `host` according to section 2, then set provider, `workdir`, checkpoint, Python runtime, load arguments, and `endpoint.port`. Initialize pinned sources before first use of built-in OpenPI, LeRobot, or StarVLA:

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive
git submodule status --recursive
```

Use an isolated Python/CUDA environment for each provider. See [`../../third_party/README.md`](../../third_party/README.md) for installation and version boundaries.

### 3.4 Compose, validate, and run

Export only the `password_env` variables required by the selected mode, then compose the Recipe:

```bash
bash embodit.sh recipe-compose \
  config/local/my-robot.json \
  config/local/my-model.json \
  --output /tmp/my-deployment.json

bash embodit.sh recipe-validate /tmp/my-deployment.json
bash embodit.sh start
```

In “Robot deployment”:

1. select Robot and Model Configs;
2. run the read-only preflight;
3. start the model and wait for `/health`;
4. enter a Prompt, connect the robot, and start Dry Run;
5. inspect actual model inputs, planned and executed actions, latency, and logs;
6. enter Live only after all checks pass;
7. finish with pause, disconnect, close, or emergency stop.

CLI equivalents:

```bash
bash embodit.sh recipe-run /tmp/my-deployment.json --mode dry_run
bash embodit.sh recipe-run /tmp/my-deployment.json --mode live
bash embodit.sh recipe-stop /tmp/my-deployment.json
bash embodit.sh recipe-stop /tmp/my-deployment.json --emergency
```

Embodit uses plain HTTP with a bearer token and has no built-in TLS. Do not expose the Web or model ports directly to the public Internet. Use a trusted private network/VPN, firewall rules, and an authenticated TLS reverse proxy. Read the [Security Policy](../../SECURITY.md) before LAN or robot control.

## 4. Common `host` fields

| Field | Required | Value |
|---|---:|---|
| `connection` | No | `ssh` (default) or `local`; both robot and model support `local` |
| `address` | Yes | SSH address; use `127.0.0.1` for a local robot; for a local model with a remote robot, use a workstation address reachable from the robot |
| `port` | No | SSH port; default `22` |
| `user` | Yes | Target user; no spaces or `@` |
| `auth` | SSH only | See below; forbidden for `local` |
| `connect_timeout_s` | No | `1..60`, default `8` |
| `host_key_policy` | No | `accept-new` or `strict` |
| `service_manager` | No | `system` or `user` systemd |

Choose one SSH authentication form.

Key:

```json
{"type": "key", "identity_file": "/home/user/.ssh/id_ed25519"}
```

Password from an environment variable:

```json
{"type": "password_env", "environment_variable": "ROBOT_SSH_PASSWORD"}
```

Inline password:

```json
{"type": "password", "password": "..."}
```

| Field | Value |
|---|---|
| `type` | `key`, `password_env`, or `password` |
| `identity_file` | Embodit-host private-key path for `key` |
| `environment_variable` | Variable name for `password_env` |
| `password` | Plaintext secret for `password` |

Prefer `key` or `password_env`. An inline password is stored in the local config:
keep the file at mode `0600` and never commit it.

Do not configure `auth` for `connection: local`. Local commands, file access,
and systemd operations run directly as the current Embodit user.

## 5. Robot Config reference

Top-level fields: `version: 1`, `kind: robot`, unique `config_id` (`A-Z/a-z/0-9/_.-`, max 64), display `name`, `host`, `robot`, `tunnel`, and `runtime`.

### 5.1 `robot.ros`

| Field | Value |
|---|---|
| `version` | `1` or `2` |
| `distro` | e.g. `noetic` or `humble` |
| `setup` | Ordered absolute paths sourced on robot |
| `domain_id` | ROS2 only, `0..232` |
| `master_uri` | ROS1 only |
| `rmw_implementation` | Optional ROS2 RMW |

### 5.2 `robot.bringup`

| Field | Value |
|---|---|
| `command` | argv array, e.g. `["ros2","launch","pkg","robot.launch.py"]` |
| `workdir` | Optional absolute robot path |
| `setup` | Additional absolute setup files |
| `environment` | Environment object |
| `startup_timeout_s` | Startup allowance before readiness |
| `restart` | `no`, `on-failure`, or `always` |

Use `['bash','-lc','...']` explicitly for shell pipelines or redirection.

### 5.3 `robot.readiness`

| Field | Value |
|---|---|
| `timeout_s` | Overall timeout, default `60` |
| `interval_s` | Retry interval, default `1` |
| `nodes` | Required full node names |
| `topics` | Exact name/type plus rate/freshness |
| `services` | `{name,type}` entries |
| `actions` | `{name,type}` entries; unavailable for ROS1 readiness |

Topic fields are `name`, exact `type`, `minimum_rate_hz` (`0` disables rate check), `sample_seconds` (`>0` and `<=15`), and optional ROS2 `maximum_age_ms`. Do not configure freshness for message types without a header.

### 5.4 Lifecycle operations

`power_on`, `power_off`, `hold`, and `stop` share:

| Field | Value |
|---|---|
| `type` | `none`, `command`, `ros2_service`, or `ros1_service` |
| `command` | argv for `command` |
| `name` / `service_type` | Exact ROS service contract |
| `request` | JSON request; `{}` for empty |
| `timeout_s` | Operation timeout |

`hold` must stop new motion while preserving a safe state. `stop` is the fastest device-defined software stop. Neither replaces a hardware E-stop.

### 5.5 `robot.initial_pose`

| Field | Value |
|---|---|
| `type` | `none`, `command`, or `follow_joint_trajectory` |
| `action` | FollowJointTrajectory action |
| `command` | argv when type is `command` |
| `joint_state_topic` | Measured state topic |
| `joint_names` / `positions` | Equal-length ordered arrays in controller units |
| `duration_s` | Motion duration |
| `tolerance` | Maximum measured absolute error |
| `timeout_s` | Command and measurement timeout |

FollowJointTrajectory initial pose is ROS2-only. Example positions are not universal safe values.

### 5.6 `robot.client`

| Field | Value |
|---|---|
| `builtin` | `ros2_standard`, `python_adapter`, or omitted with `command` |
| `config` | Built-in client configuration |
| `command/workdir/setup/environment` | Custom client process |
| `startup_timeout_s` | Client readiness timeout |
| `restart` | Prefer `no` for Live behavior |
| `health` | `http`, `tcp`, `command`, or `ros_node` check |

For a custom Client, `command/workdir/setup/environment/startup_timeout_s/restart` follow the Bringup rules. Omit `client.host` in a component Config; composition sets it to `robot`.

`health` fields:

| Field | Value |
|---|---|
| `type` | `http`, `tcp`, `command`, or `ros_node` |
| `url` | Required for `http` |
| `host` / `port` | TCP target; host defaults to `127.0.0.1`, port is required for `tcp` |
| `command` | argv required for `command` |
| `name` | Full node name required for `ros_node` |
| `startup_timeout_s` | Overall wait; default `60` |
| `interval_s` | Probe interval; default `1` |

## 6. Standard ROS2 Client

Use this when observations are `JointState`, `Image`, or `CompressedImage`, and actions use `FollowJointTrajectory`. See [`../../examples/deployment/ros2_robot_client.example.json`](../../examples/deployment/ros2_robot_client.example.json).

| Field | Value |
|---|---|
| `node_name` / `status_topic` | Client identity and status output |
| `loop_rate_hz` | Inference loop rate |
| `watchdog_timeout_s` | Complete observe→infer→validate→send deadline |
| `observation_timeout_s` | Wait for all observations |
| `maximum_observation_age_ms` | Maximum local receive age |
| `observations` | Model key → ROS topic/type, plus `joints` for JointState |
| `controller.action` | FollowJointTrajectory action |
| `controller.server_timeout_s` | Action-server timeout |
| `action.joints` | Model output joint order |
| `action.horizon` / `rate_hz` | Required rows and trajectory frequency |
| `action.baseline_observation` | Observation used for first-step validation |
| `action.limits.minimum/maximum/max_step` | Per-dimension absolute and step limits |

Model output must be finite `[horizon][joint_count]` values in controller order and units.

## 7. Python Robot Adapter

Use this for a vendor SDK that does not map cleanly to standard ROS actions. Implement:

```python
class RobotAdapter:
    def __init__(self, config): ...
    def start(self): ...
    def observe(self) -> dict: ...
    def apply_action(self, row): ...
    def stop(self): ...
```

See [adapter source](../../examples/deployment/python_robot_adapter.py) and [client config](../../examples/deployment/python_robot_client.example.json).

### 7.1 Adapter fields

| Field | Value |
|---|---|
| `adapter.entrypoint` | `module:ClassName` |
| `adapter.source_file` | Absolute `.py` path on Embodit host; uploaded to robot |
| `adapter.source_path` | Absolute robot path when already installed |
| `adapter.module_search_paths` | Additional robot import paths |
| `adapter.python_executable` | Robot Python environment |
| `adapter.config` | Passed unchanged to constructor |

`source_file` basename must match the entrypoint module.

### 7.2 Observations and Dry Run

`default_prompt` sets the initial Prompt; `task_prompts` provides up to 1000 choices; `observation_map` renames adapter keys for the model. `dry_run_observation_source` is `synthetic` or `adapter`.

Synthetic vector:

```json
{"$synthetic":"vector","length":6,"value":0}
```

Synthetic image:

```json
{"$synthetic":"image","width":224,"height":224,"channels":3,"value":0}
```

Synthetic Dry Run does not import the vendor adapter. Adapter-backed Dry Run calls read-only `observe()` but never `apply_action()`.

### 7.3 Action safety

| Field | Value |
|---|---|
| `action.width` | Row width |
| `action.horizon` | Required output rows |
| `action.baseline_observation` | Observation key for first-step comparison |
| `action.minimum/maximum` | Per-dimension absolute limits |
| `action.max_step` | Per-dimension change limits |
| `action.numerical_tolerance` | Scalar or per-dimension floating tolerance, not extra range |

All arrays must match `width`; values must be finite; `minimum < maximum`; `max_step > 0`.

### 7.4 Scheduling

| Field | Value |
|---|---|
| `control.rate_hz` / `dry_run_rate_hz` | Live and Dry Run rates |
| `control.watchdog_timeout_s` | Loop deadline |
| `control.max_episode_steps` | Optional maximum steps |
| `control.inference_mode` | `synchronous` or `asynchronous` |
| `control.action_steps` | Used rows per horizon, `1..horizon` |
| `control.asynchronous.request_after_steps` | `1..action_steps-1` or `auto` |
| `control.asynchronous.latency_margin_ms` | Auto-prefetch margin; default `30` |

Asynchronous output is revalidated against the latest measured state before switching chunks. Late inference never causes burst catch-up commands.

### 7.5 Telemetry

`telemetry.cameras` contains up to eight `{key,label}` items. `telemetry.state` and `telemetry.action` may define `label`, dimension `names`, and `units`. `max_image_bytes` defaults to 750 KB and is capped at 5 MB. `history_seconds` and `history_max_points` bound history. Images support JPEG/PNG/WebP and `mono8/rgb8/bgr8/rgba8/bgra8`.

## 8. Model Config reference

Top-level fields:

| Field | Value |
|---|---|
| `version` / `kind` | Fixed to `1` / `model` |
| `config_id` | Unique ID, at most 64 `A-Z/a-z/0-9/_.-` characters |
| `name` | Display name |
| `host` | Model execution host from section 4 |
| `model` | Provider and managed-process configuration |
| `endpoint` | Model-listener object containing the following bind/port fields |
| `endpoint.bind` | Model bind address; normally `127.0.0.1` |
| `endpoint.port` | Model port; composition maps it to the tunnel destination |

### 8.1 Common `model` fields

| Field | Value |
|---|---|
| `provider` | `python`, `openpi`, `lerobot`, `starvla`, or `external` |
| `host` | Omit or use `model` in a component Config; composition sets `model` |
| `command` | argv for `external` only; forbidden for managed providers |
| `workdir` | Absolute model-host working directory |
| `setup` | Absolute scripts sourced in order |
| `environment` | Process environment variables |
| `health` | `external` only; fields are defined in section 5.6 |
| `entrypoint` | Required for Python: `module:ClassName` or a factory |
| `checkpoint` | Host path or provider-supported identifier |
| `python_executable` | Provider Python environment |
| `load_method` / `predict_method` | Python method names; defaults `load` / `predict` |
| `load_kwargs` / `predict_kwargs` | Loader and inference arguments |
| `action_horizon` | Optional override composed into Robot Client horizon |
| `maximum_request_bytes` | Default 50 MB |
| `source_path` | Optional absolute provider source override |
| `startup_timeout_s` | Must include checkpoint load time |
| `restart` | `no`, `on-failure`, or `always` |

Do not set `command` or `health` for managed `python/openpi/lerobot/starvla` providers.

### 8.2 Custom Python model

Start from the minimal [`my_vla.py`](../../examples/deployment/my_vla.py)
example, or implement the same contract directly:

```python
class MyVLA:
    def load(self, checkpoint, **kwargs):
        self.model = YourModel.from_pretrained(checkpoint, **kwargs)

    def predict(self, observations, **kwargs):
        return self.model.predict(observations, **kwargs)
```

Set `entrypoint` to `module:ClassName`, or use a factory `create_model(checkpoint, **load_kwargs)`. `load_method` and `predict_method` override method names. Output must be finite `[horizon][width]`. Embodit generates `/health`, `/infer`, serialization, request limits, and systemd supervision.

### 8.3 Built-in checkpoint providers

| Provider | Checkpoint requirement | Typical option |
|---|---|---|
| OpenPI | Official directory normally identifies training config | `load_kwargs.config_name` for renamed/custom checkpoints |
| LeRobot | Complete `save_pretrained` directory and processors | `observation_map` for special feature names |
| StarVLA | Model config and normalization statistics beside weights | `load_kwargs.unnorm_key` for multiple domains |

For built-in OpenPI, LeRobot, and StarVLA providers, `workdir` must point to an
Embodit checkout containing `third_party/models/<provider>`. Install each provider
at the pinned source revision.

### 8.4 External provider

Use `external` only for an existing compatible service. Supply `command`, `health`, and runtime environment; the service must implement the internal `/health` and `/infer` contract.

## 9. Tunnel and runtime

Robot `tunnel` fields:

| Field | Value |
|---|---|
| `local_bind` / `local_port` | Robot-side endpoint used by Client |
| `server_alive_interval_s` / `server_alive_count_max` | SSH keepalive policy |
| `restart` | `on-failure` or `always` |
| `health_path` | Default `/health` |
| `startup_timeout_s` | Tunnel health timeout |

The composed Recipe also contains `source_host` (`robot`), `destination_host` (`model`), `remote_bind` (from `endpoint.bind`), and `remote_port` (from `endpoint.port`); component Configs do not set them. Embodit creates a deployment-specific Ed25519 key on the robot, restricts it to that forwarding destination, and maintains a separate `known_hosts` file.

Runtime fields:

| Field | Value |
|---|---|
| `default_mode` | Requested post-start mode, `dry_run` or `live`; `live` never bypasses the Dry Run arming gate |
| `auto_rollback` | Reverse rollback on startup failure |
| `stop_model_on_exit` | Stop model on full shutdown |
| `power_off_on_exit` | Call `power_off` on exit |
| `monitor_interval_s` | Component poll interval; default `2` |
| `component_failure_threshold` | Consecutive failures before fault; default `3` |

## 10. Read-only preflight and startup

“Run preflight” performs actual read-only checks: schema, host connectivity/user/tools, systemd manager access, model workdir/checkpoint/source/Python, ROS setup/CLI, and—when ROS is already running—graph, types, rates, and freshness. A stopped ROS graph produces a warning because managed bring-up happens later.

Preflight never starts services, creates a tunnel, powers the robot, or sends actions. Runtime repeats mandatory checks in order:

```text
host/systemd
→ tunnel credentials
→ Model Runner + checkpoint
→ direct model health
→ robot tunnel + tunneled health
→ ROS bring-up
→ graph/type/rate/freshness
→ power_on
→ initial pose + measured tolerance
→ Robot Client
→ first complete inference
→ dry_run
```

With `auto_rollback=true`, any failure is recorded and cleaned up in reverse order.

## 11. Offline evaluation, Dry Run, Live, and stop behavior

### 11.1 Offline single-frame evaluation

Offline single-frame evaluation is a model-only check against a recorded dataset.
Prepare the model and wait until the orchestration reaches `MODEL_READY`; the
robot observation/control path, ROS bring-up, tunnel, and Robot Client must not be
connected. Select a dataset episode and frame. Embodit reads that frame's recorded
state, camera images, and task prompt, sends the observations directly to the
resident model, and compares the predicted action chunk with the aligned recorded
action. The result reports per-dimension and overall error metrics.

This path never powers the robot, reads live robot observations, starts a control
client, or sends an action. Model inference has a 30-second timeout. Each
orchestration permits only one offline request at a time (single-flight); a
concurrent request or an attempt to enter another mode while the request is active
is rejected.

Offline evaluation is not Dry Run or Live:

- Offline single-frame evaluation reads one recorded frame and talks only to a
  `MODEL_READY` model service.
- Dry Run runs the configured observation/tunnel/model/action-validation path,
  potentially using repeated synthetic or read-only adapter observations, but
  does not send controller/adapter actions.
- Live connects the robot control path and executes real actions only after all
  readiness and arming requirements succeed.

### 11.2 Dry Run-to-Live gate

Every orchestration starts in Dry Run, including a Recipe whose
`runtime.default_mode` is `live`. Both the Web UI and CLI may promote it to Live
only after Dry Run is ready and the latest action safety check passes. The Web
UI treats the explicit Enter Live click as the transition request and does not
ask for a second manual phrase. The CLI still requires the exact server-issued
one-time phrase within its 60-second validity window:

```text
LIVE <deployment_id> <6-character uppercase hexadecimal token>
```

The Web flow can request Live only from a ready, safety-passing Dry Run. CLI
`recipe-run --mode live` (and a Recipe default of `live`) requires an interactive
TTY before starting any service, waits for Dry Run, prints the challenge, and
reads one exact line. `--no-follow` still requires confirmation before it exits.
An expired or mismatched phrase leaves the orchestration in Dry Run. There is no
path that bypasses Dry Run to reach Live.

### 11.3 Runtime behavior

- Dry Run executes observation, tunnel, model, and action validation without sending controller/adapter actions.
- Live executes real actions only after readiness and the Dry Run arming gate.
- Pause calls hold and keeps model/observations resident.
- Disconnect stops Client, ROS, and tunnel but keeps the model.
- Close also stops the model.
- Emergency stop prioritizes `robot.stop` without monitor debounce or waiting for the model.

Before normal experiments, test stop, hold, E-stop, Client crash, model timeout, network loss, ROS failure, and power-off at low speed with hardware E-stop reachable.

## 12. Diagnostics

Managed units:

```text
embodit-model-<deployment-id>.service
embodit-tunnel-<deployment-id>.service
embodit-ros-<deployment-id>.service
embodit-client-<deployment-id>.service
```

| Failure | First checks |
|---|---|
| host/systemd | SSH auth, host key, user, system/user manager access |
| model environment | workdir, Python, checkpoint, CUDA, provider deps |
| model health | load logs, input schema, port conflict |
| tunnel | robot-to-model SSH address/port and key restriction |
| ROS readiness | setup order, Domain ID/Master URI, names, types, rates, header time |
| initial pose | joint order, units, controller state, measured tolerance |
| client safety | action shape, NaN/Inf, absolute limits, max step, watchdog |

## Internal HTTP routes

The Web UI and `embodit.sh` are the supported operator interfaces. Routes under
`/api/...` are pre-1.0 implementation details and may change without a
compatibility guarantee; do not build an external control client against them.
