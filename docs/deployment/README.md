# Real-robot deployment guide

**English** · [中文](README.zh-CN.md)

Recipe v2 targets one explicit topology: the workstation manages a model host and a robot host over SSH; the robot host creates an SSH Local Forward to the model host; ROS, hardware lifecycle operations, initial pose, and the Robot Client run on the robot host. Embodit is the control plane and is not in the real-time observation/action data path.

## 1. Runtime topology

```text
Workstation [Embodit]
  ├─ SSH → Model Host [Inference Service :8000]
  └─ SSH → Robot Host [SSH Tunnel + ROS Bringup + Robot Client]
                         └─ localhost:8000 → Model Host:8000
```

Long-running remote processes are managed as systemd transient units:

```text
embodit-model-<deployment-id>.service
embodit-tunnel-<deployment-id>.service
embodit-ros-<deployment-id>.service
embodit-client-<deployment-id>.service
```

A brief workstation disconnect does not directly terminate them. Tunnel and process restart behavior follows the Recipe, except that the Client is forced to `restart=no` in Live mode so a safety fault cannot resume physical motion without a new confirmation.

## 2. Responsibility boundary

| User/robot integrator supplies | Embodit supplies |
|---|---|
| SSH-reachable model and robot hosts, credentials, ROS setup paths | SSH execution, host validation, process supervision, logs, ordered startup and rollback |
| A Python model entrypoint and checkpoint, or an advanced external service | Model Runner, localhost HTTP protocol, health checks, tensor/JSON normalization |
| ROS topic/service/action mapping and robot-specific limits | Graph/type/rate/freshness readiness and built-in ROS2 Client validation |
| Vendor driver/bridge, hardware limits, e-stop, safe power/hold/stop behavior | Dry Run, short-lived Live challenge, goal cancellation, configured hold/stop/power-off calls |

Embodit cannot infer safe joint limits, initial poses, controller rates, or lifecycle service semantics. Those values must come from the robot manufacturer and be validated at low speed with hardware e-stop access. A software stop is not a replacement for a certified hardware safety system.

## 3. Recipe configuration

Copy [`../../config/deployment.recipe-v2.example.json`](../../config/deployment.recipe-v2.example.json). Its principal sections are:

- `hosts.model`, `hosts.robot`: address, port, user, authentication, host-key policy, system/user service manager;
- `model`: Python entrypoint, checkpoint, interpreter, working directory, environment, or an advanced external command;
- `tunnel`: robot-local listening address/port, model-side target, SSH keepalive and restart policy;
- `robot.ros`: ROS 1/2, setup scripts, Domain ID/Master URI, RMW;
- `robot.bringup`: vendor driver and controller bring-up command;
- `robot.readiness`: required nodes and typed topics/services/actions, topic rate and freshness;
- lifecycle operations: `power_on`, `power_off`, `hold`, `stop`;
- `initial_pose`: FollowJointTrajectory pose or custom command plus measured tolerance;
- `robot.client`: built-in `ros2_standard` Client or a custom command;
- `runtime`: default mode, rollback, model shutdown, and power-off policy.

Authentication may be managed directly in the Recipe or through an environment variable/key:

```json
{"type":"password","password":"..."}
{"type":"password_env","environment_variable":"ROBOT_SSH_PASSWORD"}
{"type":"key","identity_file":"/home/user/.ssh/id_ed25519"}
```

Stored Recipe files and directories use `0600` and `0700` permissions. Passwords are not placed in SSH argv, orchestration logs, or the downloaded manifest. A password in a config file is still a secret: keep the file local, permission-restricted, and out of Git.

## 4. Minimal model interface

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

1. logs into the model and robot hosts using the configured workstation credentials;
2. creates a deployment-specific tunnel key under `~/.embodit/deployments/<id>/keys/` on the robot;
3. installs its public key in the model account through the workstation management connection;
4. restricts that authorized key to forwarding the Recipe’s model port;
5. writes deployment-specific `known_hosts` on the robot;
6. starts SSH with `ExitOnForwardFailure` and configured keepalives.

The user therefore configures only workstation-accessible credentials; no manual robot-to-model key installation is required.

## 6. Exact startup and readiness standard

The startup state machine is fixed:

```text
SSH/systemd preflight
→ coordinate tunnel credentials
→ start Model Runner and load entrypoint/checkpoint
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
| SSH/systemd | Both hosts accept the configured login and can manage transient units |
| Python model | Runner `/health` returns 2xx and does not report `ready:false`/`ok:false` after model load |
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

## 8. Dry Run, Live, rollback, and stop

Dry Run uses the final observation → tunnel → model → validation path but does not call the trajectory controller. The deployment is ready only after one complete inference succeeds.

Live requires a UI challenge phrase valid for 60 seconds. Embodit restarts the Client with `EMBODIT_DEPLOYMENT_MODE=live`; Live cannot be enabled by merely editing the Recipe default while an orchestration is running.

On startup failure or monitored component failure, `auto_rollback` stops components in reverse order and can call configured hold/power-off operations. Emergency stop calls `robot.stop` on the robot side first and does not wait for the model. Because robot behavior is vendor-specific, test normal stop, hold, emergency stop, process crash, network loss, and power-off in a controlled setup before production use.

## 9. Use the UI and CLI

```bash
bash embodit.sh start
```

Open “Robot deployment”, load the “dual-host SSH + ROS automated deployment (v2)” example, edit/save the JSON, then select “Preflight and Dry Run”. The UI exposes step state, component state, logs, controlled component restart, Live confirmation, and emergency stop.

CLI and rescue commands:

```bash
bash embodit.sh recipe-validate config/my-robot.json
bash embodit.sh recipe-run config/my-robot.json --mode dry_run
bash embodit.sh recipe-run config/my-robot.json --mode live
bash embodit.sh recipe-stop config/my-robot.json
bash embodit.sh recipe-stop config/my-robot.json --emergency
```

`recipe-run --no-follow` exits after readiness while the remote systemd services remain active.

## 10. ROS generalization boundary

Node names are readiness signals; functional adaptation is defined by typed topics, services, and actions. Recipe orchestration supports ROS1 and ROS2. The built-in Client and FollowJointTrajectory initial-pose implementation are ROS2-only; ROS1 requires a custom Client/command, and ROS1 readiness does not accept an `actions` declaration (check actionlib topics instead).

Changing robot SDKs should normally change only the Recipe and a thin ROS Bridge. The SSH topology, tunnel, model provider, lifecycle state machine, and UI remain unchanged.

## 11. API

```text
GET/POST    /api/deploy/recipes
GET/DELETE  /api/deploy/recipes/{id}
POST        /api/deploy/recipes/validate
POST        /api/deploy/doctor
POST        /api/deploy/orchestrations
GET         /api/deploy/orchestrations/{id}
POST        /api/deploy/orchestrations/{id}/arm-challenge
POST        /api/deploy/orchestrations/{id}/start-live
POST        /api/deploy/orchestrations/{id}/stop
POST        /api/deploy/orchestrations/{id}/emergency-stop
POST        /api/deploy/orchestrations/{id}/logs
POST        /api/deploy/orchestrations/{id}/components/{component}/restart
GET         /api/deploy/orchestrations/{id}/manifest
```

Recipe v2 and this Orchestration API are the only supported real-robot deployment protocol.
