# 模型接入与 ROS Client 配置示例

Recipe v2 的模型推荐从 [`my_vla.py`](my_vla.py) 开始：用户只实现 `load(checkpoint)` 和 `predict(observations)`，在 Recipe 中填写 `provider: python`、`entrypoint` 和 `checkpoint`。内部 Model Runner 由 Embodit 自动上传和管理，普通用户不需要运行或修改它，也不需要实现 `/health`、`/infer`。

Recipe v2 默认使用 Embodit 内置的 ROS2 Client。它运行在本体端，通过本地 SSH 隧道请求模型，在本体侧校验动作并调用标准 `FollowJointTrajectory`；配置见 [`ros2_robot_client.example.json`](ros2_robot_client.example.json)。厂商 SDK 不提供标准 ROS 接口时，在该 Client 与 SDK 之间增加薄 ROS Bridge。
