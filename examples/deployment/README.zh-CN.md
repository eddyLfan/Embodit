# 模型接入与 ROS Client 配置示例

OpenPI、LeRobot、StarVLA 优先使用 `config/deployment/models/<provider>.example.json`：固定版本源码和独立环境一次性准备完成后，只填写 Provider 与 Checkpoint，再与任意本体 Config 组合。自定义模型从 [`my_vla.py`](my_vla.py) 开始，实现 `load(checkpoint)` 和 `predict(observations)` 并配置 `provider: python`、`entrypoint`。两种方式的内部 Model Runner 都由 Embodit 自动上传和管理，用户不需要实现 `/health`、`/infer`；第三方版本与许可证说明见 [`../../third_party/README.md`](../../third_party/README.md)。

Recipe 默认使用 Embodit 内置的 ROS2 Client。它运行在本体端，通过本地 SSH 隧道请求模型，在本体侧校验动作并调用标准 `FollowJointTrajectory`；配置见 [`ros2_robot_client.example.json`](ros2_robot_client.example.json)。厂商 SDK 不提供标准 ROS 接口时，可使用通用 `python_adapter`：配置见 [`python_robot_client.example.json`](python_robot_client.example.json)，薄适配接口见 [`python_robot_adapter.py`](python_robot_adapter.py)。
