# Real-robot deployment guide

**English** · [中文](README.zh-CN.md)

Configuration management uses independently saved robot and model configs. Select one of each for a test and Embodit composes the runtime Recipe. The model can run on an independent SSH host or directly on the Embodit machine. The robot creates an SSH Local Forward to the model endpoint; ROS, hardware lifecycle operations, initial pose, and the Robot Client run on the robot host. Embodit is the control plane and is not in the real-time observation/action data path.

## 1. Runtime topology

```text
Workstation [Embodit]
  ├─ SSH → Model Host [Inference Service :8000]
  └─ SSH → Robot Host [SSH Tunnel + ROS Bringup + Robot Client]
                         └─ localhost:8000 → Model Host:8000
```

When Embodit and the model share a machine, model management runs locally and does not SSH back into the same host:

```text
Embodit + Local Model [Inference Service :8000]
  └─ SSH → Robot Host [SSH Tunnel + ROS Bringup + Robot Client]
                         └─ localhost:8000 → Embodit Host:8000
```

The robot still uses a deployment key and the model-side SSH server for restricted port forwarding. `host.address`/`port` must therefore be reachable from the robot, and `host.user` must match the account running Embodit.

Long-running remote processes are managed as systemd transient units:

```text
embodit-model-<deployment-id>.service
embodit-tunnel-<deployment-id>.service
embodit-ros-<deployment-id>.service
embodit-client-<deployment-id>.service
```

A brief workstation disconnect does not directly terminate them. Tunnel and process restart behavior follows the Recipe, except that the Client is forced to `restart=no` in Live mode so an action-validation or robot-call fault cannot automatically resume physical motion.

## 2. Responsibility boundary

| User/robot integrator supplies | Embodit supplies |
|---|---|
| An SSH-reachable robot plus either a separate model host or the Embodit machine address, and ROS setup paths | Local/SSH execution, host validation, process supervision, logs, ordered startup and rollback |
| A common model provider and checkpoint, or a custom Python entrypoint/advanced external service | OpenPI/LeRobot/StarVLA adapters, Model Runner, localhost HTTP protocol, health checks, tensor/JSON normalization |
| ROS topic/service/action mapping and robot-specific limits | Graph/type/rate/freshness readiness and built-in ROS2 Client validation |
| Vendor driver/bridge, hardware limits, e-stop, safe power/hold/stop behavior | Action validation, execution logs, pause/resume, goal cancellation, configured hold/stop/power-off calls |

Embodit cannot infer safe joint limits, initial poses, controller rates, or lifecycle service semantics. Those values must come from the robot manufacturer and be validated at low speed with hardware e-stop access. A software stop is not a replacement for a certified hardware safety system.

## 3. Separate, composable configuration

The deployment workspace manages two component types:

- robot config (`version: 1, kind: robot`): robot SSH, ROS, bringup/readiness, lifecycle operations, initial pose, Robot Client, safety limits, the robot side of the tunnel, and exit policy;
- model config (`version: 1, kind: model`): local/SSH model connection, provider, checkpoint, environment, and endpoint; only custom `python` providers need an entrypoint;
- composition metadata: only the run-specific deployment ID and name. A robot can be tested with multiple models, and a model can be reused across multiple robots.

Start from [`../../config/deployment/robot.example.json`](../../config/deployment/robot.example.json) and [`../../config/deployment/models/python.example.json`](../../config/deployment/models/python.example.json). Provider templates are also available for [OpenPI](../../config/deployment/models/openpi.example.json), [LeRobot](../../config/deployment/models/lerobot.example.json), and [StarVLA](../../config/deployment/models/starvla.example.json).

Component configs do not manage internal `robot` / `model` host references. Composition creates canonical aliases, connects the robot tunnel to the selected model endpoint, and runs full Recipe validation. Storage is separated by config type and retains `0600` file / `0700` directory permissions.

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

The UI imports individual component configs, bundles containing both, and legacy full Recipe documents (which it splits automatically). A bundle export preserves both source configs and the generated recipe for reproducibility.

### 3.1 Full Recipe

The runtime orchestrator continues to consume the full Recipe shown in [`../../config/deployment/recipe.example.json`](../../config/deployment/recipe.example.json). Its principal sections are:

- `hosts.model`, `hosts.robot`: management connection, address, port, user, authentication, host-key policy, and system/user service manager; models support `connection: local`, while robots currently require `ssh`;
- `model`: Python entrypoint, checkpoint, interpreter, working directory, environment, or an advanced external command;
- `tunnel`: robot-local listening address/port, model-side target, SSH keepalive and restart policy;
- `robot.ros`: ROS 1/2, setup scripts, Domain ID/Master URI, RMW;
- `robot.bringup`: vendor driver and controller bring-up command;
- `robot.readiness`: required nodes and typed topics/services/actions, topic rate and freshness;
- lifecycle operations: `power_on`, `power_off`, `hold`, `stop`;
- `initial_pose`: FollowJointTrajectory pose or custom command plus measured tolerance;
- `robot.client`: `ros2_standard`, the vendor-neutral `python_adapter`, or an advanced custom command;
- `runtime`: default mode, rollback, model shutdown, power-off policy, and component-monitor timing/thresholds.

Authentication may be managed directly in the Recipe or through an environment variable/key:

```json
{"type":"password","password":"..."}
{"type":"password_env","environment_variable":"ROBOT_SSH_PASSWORD"}
{"type":"key","identity_file":"/home/user/.ssh/id_ed25519"}
```

Omitting `connection` defaults to `ssh`, preserving existing configs. For a model on the Embodit machine use:

```json
{
  "connection": "local",
  "address": "192.168.10.10",
  "port": 22,
  "user": "embodit",
  "service_manager": "user"
}
```

Local connections omit `auth`; the address, port, and user are still used by the robot-side model tunnel. `service_manager: user` avoids system-unit privileges. With `system`, the Embodit account must be allowed to manage system units.

Stored Recipe files and directories use `0600` and `0700` permissions. Passwords are not placed in SSH argv, orchestration logs, or the downloaded manifest. A password in a config file is still a secret: keep the file local, permission-restricted, and out of Git.

## 4. Checkpoint-only model providers

After the model host initializes the pinned submodules once and creates an isolated environment according to each upstream project, OpenPI, LeRobot, and StarVLA deployments need only a provider and checkpoint. No `entrypoint`, launch command, `/health`, or `/infer` implementation is required:

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

The UI's common-model/checkpoint row generates this configuration. `workdir` points to the Embodit checkout on the model host, and the adapter loads upstream code through the pinned `third_party/models/<provider>` gitlink. Set an absolute `source_path` for a checkout in another location. See [`../../third_party/README.md`](../../third_party/README.md) for sources, pinned commits, license boundaries, initialization, and reviewed upgrades.

The checkpoint must contain the metadata its upstream runtime requires: a complete LeRobot `save_pretrained` directory, StarVLA model config and normalization statistics, or the matching OpenPI training config. Official OpenPI directory names are normally inferred; renamed/custom checkpoints set `load_kwargs.config_name`. These are checkpoint-format details, not user entrypoint code.

### 4.1 Custom Python model interface

With the default `python` provider, the user supplies only a model class and checkpoint on the model host:

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

The entrypoint may also be a factory `create_model(checkpoint, **load_kwargs) -> model`; the returned object implements `predict`.

Input contract:

- each observation key matches `robot.client.config.observations`;
- `JointState` becomes a Python list ordered by the configured `joints` list; the model and Client-side action validation must still treat non-finite sensor input as a fault;
- `Image`/`CompressedImage` becomes a dictionary containing decoded `data: bytes`, encoding, timestamp/frame ID, and raw-image dimensions where applicable;
- `predict_kwargs` are appended to every call.

Output contract:

- return a Python nested list, NumPy array, or Torch tensor shaped exactly `[horizon][joint_count]`;
- values must be finite numbers in the same units and joint order as the configured controller action;
- the built-in Client rejects missing/extra horizon rows or joint dimensions; it does not reshape or reorder model output.

Recipe example:

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

`entrypoint`, `checkpoint`, `workdir`, and the interpreter are resolved on the model host. Embodit uploads a generic Model Runner, binds `/health` and `/infer` to model-host localhost, limits request size (50 MB by default), decodes binary observations, normalizes tensor/array output, reports exceptions, and supervises the process. These internal HTTP endpoints are not the normal user integration surface.

Set `provider: external` only for an already managed service. In that mode the user owns its command, health check, and compatible inference protocol. A minimal template is available at [`../../examples/deployment/my_vla.py`](../../examples/deployment/my_vla.py).

## 5. Tunnel authentication automation

On first start, Embodit:

1. logs into the robot and, for a remote model, the model host; a local model is managed directly;
2. creates a deployment-specific tunnel key under `~/.embodit/deployments/<id>/keys/` on the robot;
3. installs the public key through model management: directly for a local model or over SSH for a remote model;
4. restricts that authorized key to forwarding the Recipe’s model port;
5. writes deployment-specific `known_hosts` on the robot;
6. starts SSH with `ExitOnForwardFailure` and configured keepalives.

Remote deployments need workstation-accessible credentials for both endpoints; a local model needs no management credentials. Neither topology requires manual robot-to-model key installation.

## 6. Exact startup and readiness standard

The startup state machine is fixed:

```text
SSH/systemd preflight
→ coordinate tunnel credentials
→ start Model Runner and load checkpoint through its provider
→ direct model health
→ start robot-side SSH tunnel
→ model health through robot-local tunnel
→ ROS bringup
→ ROS graph/type/freshness/rate readiness
→ power on
→ initial pose and measured tolerance check
→ Robot Client
→ first complete inference readiness
→ dry_run/running
```

Readiness is a contract, not a delay:

| Check | Pass standard |
|---|---|
| Connection/systemd | SSH hosts accept configured logins; a local model user matches the Embodit process account; each endpoint can manage transient units |
| Managed model | Runner `/health` returns 2xx and does not report `ready:false`/`ok:false` after provider checkpoint load |
| Tunnel | The same model health succeeds through `robot localhost:<local_port>` |
| ROS node | Every configured node name appears exactly in the graph |
| Topic/service/action | Every configured name exists with the exact configured type |
| Topic rate | Parsed average over `sample_seconds` is ≥ `minimum_rate_hz`; `0` disables rate checking |
| ROS2 freshness | A sample arrives and its header timestamp is within ±`maximum_age_ms`; configuring freshness for a headerless type fails |
| ROS1 freshness | At least one sample arrives; ROS1 currently does not calculate header age |
| Initial pose | Every named joint is present in `JointState` and maximum absolute position error ≤ `tolerance` (default 0.03) |
| Built-in Client | Its status topic reports `ready` only after observation collection, tunneled inference, response parsing, and action validation all succeed |

Readiness retries every `interval_s` until `timeout_s`. A missing type, stale/slow topic, pose error, or Client `fault` fails startup; configured rollback then runs in reverse order.

## 7. Built-in ROS2 Client safety standard

`builtin: ros2_standard` supports `sensor_msgs/msg/JointState`, `sensor_msgs/msg/Image`, `sensor_msgs/msg/CompressedImage`, and sends `control_msgs/action/FollowJointTrajectory`. The generated Client config is uploaded automatically. See [`../../examples/deployment/ros2_robot_client.example.json`](../../examples/deployment/ros2_robot_client.example.json).

Before any Live goal is sent, the Client enforces:

- all configured observations arrived within `observation_timeout_s` (default 3 s) and their local receipt age is ≤ `maximum_observation_age_ms` (default 500 ms);
- inference HTTP timeout is the smaller of model timeout (default 10 s) and watchdog timeout (default 1 s);
- response body is at most `maximum_response_bytes` (default 10 MB) and valid JSON;
- action shape equals configured horizon × joint count, with numeric finite values only;
- each value lies within the per-joint absolute `minimum`/`maximum`;
- the first horizon row differs from the configured baseline observation by no more than `max_step`, and every later row differs from the previous row by no more than `max_step`;
- the FollowJointTrajectory server accepts the goal within its timeout;
- one full loop finishes within `watchdog_timeout_s`; on any exception it cancels tracked goals, publishes `fault`, and exits.

These are software guards at the Robot Client boundary. The ROS Bridge/vendor controller must independently enforce hardware limits, command validity/age, controller state, collision/speed constraints, and e-stop behavior. For a nonstandard SDK, implement a thin ROS Bridge that maps its state, trajectory, power, hold, stop, and fault codes to the configured ROS contract.

### 7.1 Generic Python Robot Adapter

For vendor SDKs that do not map cleanly to standard ROS, use `builtin: python_adapter`. Embodit uploads and owns the generic Client, model protocol, prompt, Dry Run, action validation, timing, readiness, logs, and fault handling. The robot integrator supplies only an object with `observe()` and `apply_action(row)`, plus optional `start()` and `stop()` methods.

`adapter.entrypoint` uses `module:ClassName`. Use robot-side `source_path` for an installed adapter, or an absolute control-host `source_file` for a one-file adapter that Embodit uploads automatically; `python_executable` is resolved on the robot. Dry Run uses declarative synthetic observations and never imports the adapter or initializes the vendor SDK. The action config defines width, horizon, baseline observation, and per-dimension minimum, maximum, and max-step limits. `numerical_tolerance` may be a scalar or a width-sized array; only boundary noise within that tolerance is clamped to the legal range. In Live, an invalid model chunk is discarded and replaced with a validated hold chunk built from the latest measured model state, while the Client continues requesting inference instead of exiting. See [`../../examples/deployment/python_robot_client.example.json`](../../examples/deployment/python_robot_client.example.json) and [`../../examples/deployment/python_robot_adapter.py`](../../examples/deployment/python_robot_adapter.py).

`action.horizon` is the number of steps the model must return per request. `control.action_steps` selects how many steps Embodit consumes from each chunk (the full horizon by default), and `control.rate_hz` sets the Live action rate. `control.inference_mode` accepts `synchronous` or `asynchronous`. Synchronous mode requests the next chunk only after the current chunk finishes. Asynchronous mode starts the next request at `control.asynchronous.request_after_steps` while the rest of the current chunk continues. The prefetch point may be a fixed integer or `"auto"`; automatic mode uses the latest round-trip latency, action rate, and `latency_margin_ms` to reserve enough actions. Before switching chunks, Embodit revalidates the asynchronous result against the latest robot state. A late inference resumes the scheduler from the current clock instead of bursting actions to catch up with stale deadlines. A typical Pi setup is `horizon: 50`, `action_steps: 50`, and either `request_after_steps: 30` or `"auto"`.

Adapter-backed Dry Run keeps sampling and inferring after an individual action-safety rejection. It reports `dryRunSafety.passed: false` without stopping the Client or resident model. Dry Run remains available for initial integration and diagnostics, but it is not a mandatory step before each real evaluation.

Optional `telemetry` describes only how model I/O is displayed: `cameras` maps image input keys to labels, `state` names and units the model state vector, and `action` names and units each output dimension. Embodit derives this bounded view from the same `observe()` payload and validated action chunk; it does not read extra vendor, ROS, tunnel, or host state. `history_seconds` (20 seconds by default) and `history_max_points` bound camera-free history for observed state, model plans, and actually applied actions. JPEG/PNG/WebP and raw mono/RGB/BGR/RGBA/BGRA frames are supported, with at most eight images and a 750 KB per-image default limit.

## 8. Dry Run, Live, rollback, and stop

Dry Run uses the final observation → tunnel → model → validation path but does not call the trajectory controller. It is useful for initial integration and fault diagnosis.

For routine experiments, a model-ready deployment can start real evaluation directly without repeating Dry Run or typing a challenge phrase. Pausing stops physical actions and restores read-only observation while the model, tunnel, and ROS stay resident; resume is immediate. Changing the Prompt restarts only the lightweight Client, not the model. Common choices are configured with `task_prompts` in the robot Client config.

Recorded experiment poses contain only the joint state vector consumed by the model. Moving to one pauses evaluation and uses the same generic Adapter to interpolate within configured `max_step` limits, then leaves the resident model and read-only observation running.

On startup failure or a confirmed persistent component failure, `auto_rollback` stops components in reverse order and can call configured hold/power-off operations. At runtime Embodit reads structured systemd state every two seconds by default and confirms a fault only after three consecutive unhealthy checks. A single SSH probe failure or a systemd auto-restart transition therefore does not immediately terminate the deployment; recovery records a `component_recovered` event. `runtime.monitor_interval_s` and `runtime.component_failure_threshold` are configurable, although a threshold of one is discouraged. Probe failures and actual process exits produce distinct diagnostics. Emergency stop bypasses this debounce: it calls `robot.stop` on the robot side first and does not wait for the model. Because robot behavior is vendor-specific, test normal stop, hold, emergency stop, process crash, network loss, and power-off in a controlled setup before production use.

## 9. Use the UI and CLI

```bash
bash embodit.sh start
```

Open “Robot deployment”, select a robot and model, start the model, choose a Prompt, and press “Start evaluation”. The scheduler control switches between synchronous, fixed-prefetch asynchronous, and latency-adaptive asynchronous execution without restarting the model. The lower-left log panel switches between orchestration, Client, Model, Tunnel, and ROS logs, with filtering and follow controls. The right side shows only real model inputs plus bounded state, planned-action, and applied-action histories. “Disconnect” stops the robot-side Client, ROS, and tunnel while preserving the model; “Close” also stops the model service.

CLI and rescue commands:

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

`recipe-run --no-follow` exits after readiness while the remote systemd services remain active.

## 10. ROS generalization boundary

Node names are readiness signals; functional adaptation is defined by typed topics, services, and actions. Recipe orchestration supports ROS1 and ROS2. `ros2_standard` and the FollowJointTrajectory initial-pose implementation are ROS2-only. ROS1 and non-standard SDKs can use the vendor-neutral `python_adapter` contract; ROS1 readiness does not accept an `actions` declaration (check actionlib topics instead).

Changing robot SDKs should normally change only the Recipe and a thin ROS Bridge. The SSH topology, tunnel, model provider, lifecycle state machine, and UI remain unchanged.

## 11. API

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

Robot/model Config is the configuration-management interface. The composed Recipe and this Orchestration API remain the only runtime protocol.
