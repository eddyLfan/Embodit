# Robot Deployment Guide

**English** · [中文](README.zh-CN.md)

This guide covers model, robot, and safety integration. The architecture has been exercised on real hardware; each new device still requires explicit SDK/ROS interfaces, units, joint order, limits, and lifecycle operations.

## 1. Runtime architecture

Embodit is the control plane, not part of the real-time observation/action path:

```text
Workstation [Embodit]
  ├─ local/SSH → Model Host [Model Runner :8000]
  └─ SSH → Robot Host
             ├─ SSH local-forward: localhost:8000 → Model Host:8000
             ├─ ROS Bringup
             └─ Robot Client: observation → model → validation → controller
```

The model may run on the Embodit workstation or a separate GPU host. The robot must currently be managed over SSH and must be able to reach the model host's SSH address for the restricted tunnel.

Deployment configuration has three parts:

- Robot Config: host, ROS, bring-up, readiness, lifecycle, initial pose, Robot Client, limits;
- Model Config: host, provider, checkpoint, Python environment, endpoint;
- Recipe v2: the single runtime document composed from both configs.

Templates: [robot](../../config/deployment/robot.example.json), [Python model](../../config/deployment/models/python.example.json), [OpenPI](../../config/deployment/models/openpi.example.json), [LeRobot](../../config/deployment/models/lerobot.example.json), and [StarVLA](../../config/deployment/models/starvla.example.json).

## 2. Prerequisites

Workstation:

- Linux, Python 3.10+, uv, OpenSSH client;
- SSH access to robot and, when remote, model host;
- writable project directory for `.embodit_cache/deploy/`;
- for a local model, `host.user` must equal the user running Embodit.

Robot host:

- Python 3, OpenSSH client, `ssh-keygen`, `ssh-keyscan`;
- systemd and `systemd-run`;
- configured ROS setup files and robot bring-up;
- typed topics/services/actions or a vendor Python SDK;
- independent hardware limits, command expiry, fault handling, and emergency stop.

Model host:

- systemd, provider Python/CUDA environment, checkpoint, and Embodit checkout;
- SSH for a remote model; local execution for a workstation model;
- keep `endpoint.bind` on `127.0.0.1` unless there is a specific secured reason not to.

Initialize built-in provider sources once:

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive
git submodule status --recursive
```

Use an isolated environment per provider. See [`../../third_party/README.md`](../../third_party/README.md).

## 3. Minimal integration workflow

```bash
mkdir -p config/local/models
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/models/my-model.json
```

After editing both files:

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'
export MODEL_SSH_PASSWORD='<model-password>'  # remote model only

bash embodit.sh recipe-compose \
  config/local/my-robot.json \
  config/local/models/my-model.json \
  --output /tmp/my-deployment.json

bash embodit.sh recipe-validate /tmp/my-deployment.json
```

Start `bash embodit.sh start`, open “Robot deployment,” select both configs, run the read-only preflight, prepare the model, enter a Prompt, and connect the robot. CLI equivalents:

```bash
bash embodit.sh recipe-run /tmp/my-deployment.json --mode dry_run
bash embodit.sh recipe-run /tmp/my-deployment.json --mode live
bash embodit.sh recipe-stop /tmp/my-deployment.json
bash embodit.sh recipe-stop /tmp/my-deployment.json --emergency
```

## 4. Common `host` fields

| Field | Required | Value |
|---|---:|---|
| `connection` | No | `ssh` (default) or `local`; robot must be `ssh` |
| `address` | Yes | SSH address; for local model, an address reachable from the robot |
| `port` | No | SSH port; default `22` |
| `user` | Yes | Target user; no spaces or `@` |
| `auth` | SSH only | See below; forbidden for `local` |
| `connect_timeout_s` | No | `1..60`, default `8` |
| `host_key_policy` | No | `accept-new` or `strict` |
| `service_manager` | No | `system` or `user` systemd |

Auth forms:

```json
{"type": "key", "identity_file": "/home/user/.ssh/id_ed25519"}
{"type": "password_env", "environment_variable": "ROBOT_SSH_PASSWORD"}
{"type": "password", "password": "..."}
```

| Field | Value |
|---|---|
| `type` | `key`, `password_env`, or `password` |
| `identity_file` | Embodit-host private-key path for `key` |
| `environment_variable` | Variable name for `password_env` |
| `password` | Plaintext secret for `password` |

Prefer `key` or `password_env`. Never commit credentials.

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

Topic fields are `name`, exact `type`, `minimum_rate_hz` (`0` disables rate check), `sample_seconds` (`0..15`), and optional ROS2 `maximum_age_ms`. Do not configure freshness for message types without a header.

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

```json
{"$synthetic":"vector","length":6,"value":0}
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

`workdir` points to an Embodit checkout containing `third_party/models/<provider>`. Install each provider at the pinned source revision.

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
| `default_mode` | `dry_run` or `live` |
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
→ dry_run / running
```

With `auto_rollback=true`, any failure is recorded and cleaned up in reverse order.

## 11. Dry Run, Live, and stop behavior

- Dry Run executes observation, tunnel, model, and action validation without sending controller/adapter actions.
- Live executes real actions after readiness.
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

Configs are reusable components; Recipe is the runtime protocol. Do not maintain a second execution configuration.
