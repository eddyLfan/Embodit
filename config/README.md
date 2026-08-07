# 配置目录

配置按使用场景分组，避免运行时文件、数据工作流和真机部署模板混在一起。

```text
config/
├── data/
│   ├── review.json                 # 审核“不合格原因”的默认配置
│   ├── qc.example.json             # 自动质检完整参数示例
│   └── convert.example.json        # 跨格式转换字段映射示例
├── deployment/
│   ├── recipe.example.json         # 完整字段参考；仅用于校验和复制
│   ├── robot.example.json          # 可复用的本体 Config
│   └── models/
│       ├── python.example.json     # 自定义 Python 模型（默认示范 Embodit 同机）
│       ├── openpi.example.json     # OpenPI checkpoint
│       ├── lerobot.example.json    # LeRobot checkpoint
│       └── starvla.example.json    # StarVLA checkpoint
└── local/                           # 本机私有配置（按需创建，不提交）
```

`*.example.json` 是使用 RFC 文档专用地址和 `/path/to/...` 占位路径的字段模板，不是可直接部署的配置。请先复制到已被 Git 忽略的 `config/local/`，再填写真实主机、用户、凭据、工作目录、Python、checkpoint、ROS 接口和安全限位。只有复制后的本地配置通过预检后才可运行；不要直接运行仓库内的示例 Recipe。`data/review.json` 是应用默认读取的版本化配置，可直接修改；也可通过 `EMBODIT_REVIEW_CONFIG` 指向外部文件。

本地 token、服务 PID、访问 URL 和日志统一保存在 `.embodit/`，缓存与任务产物保存在 `.embodit_cache/`，两者都不会提交到 Git。
