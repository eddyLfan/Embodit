# Deployment Integration Examples

**English** · [中文](README.zh-CN.md)

For OpenPI, LeRobot, and StarVLA, begin with the matching template under
`config/deployment/models/`. Prepare the pinned provider source and an isolated
runtime, then replace the checkpoint and host placeholders before composing it
with a Robot Config.

For a custom model, copy [`my_vla.py`](my_vla.py), implement
`load(checkpoint)` and `predict(observations)`, and select `provider: python`
with its `entrypoint`. Embodit supplies and manages the internal `/health` and
`/infer` Model Runner; custom providers do not need to recreate that server.

The standard ROS2 client uses the robot-side model tunnel, validates actions,
and sends `FollowJointTrajectory`; start from
[`ros2_robot_client.example.json`](ros2_robot_client.example.json). If a vendor
SDK does not expose a compatible ROS interface, use the generic Python adapter:
[`python_robot_client.example.json`](python_robot_client.example.json) plus
[`python_robot_adapter.py`](python_robot_adapter.py).

Examples contain placeholders, not verified device settings. Copy private
Robot/Model Configs directly to the non-recursive `config/local/` directory,
keep that directory at mode `0700` and its JSON files at mode `0600`, replace
units, mappings, interfaces, and limits, and complete controlled preflight/Dry
Run before Live. See the
[deployment guide](../../docs/deployment/README.md) and
[third-party notices](../../third_party/README.md).
