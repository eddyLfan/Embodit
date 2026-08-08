# 配置指南

[English](README.md) · **中文**

```text
config/
├── data/
│   ├── review.json                 # 默认人工隔离原因
│   ├── qc.example.json             # 自动 QC 参数模板
│   └── convert.example.json        # 跨格式映射模板
├── deployment/
│   ├── recipe.example.json         # Recipe v2 完整字段参考
│   ├── robot.example.json          # 可复用本体 Config
│   └── models/
│       ├── python.example.json     # 自定义 Python 模型
│       ├── openpi.example.json     # OpenPI Checkpoint Provider
│       ├── lerobot.example.json    # LeRobot Checkpoint Provider
│       └── starvla.example.json    # StarVLA Checkpoint Provider
└── local/                          # 私有本机配置，按需创建
```

仓库中的 `*.example.json` 使用文档专用地址和 `/path/to/...` 占位路径，只是 Schema 参考，不能直接用于真机。请把本体和模型 Config 直接复制到 `config/local/` 根目录，替换全部占位内容，并先运行只读预检：

```bash
mkdir -p config/local
chmod 700 config/local
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/my-model.json
chmod 600 config/local/my-robot.json config/local/my-model.json
```

项目配置发现有意保持非递归，只读取 `config/local/*.json`；放在 `config/local/models/` 的文件不会出现在 Web 选择器中。通过 Web UI 保存且通过校验的本体/模型 Config 私密存放在 `.embodit_cache/deploy/configs/`；若与项目配置使用相同 `config_id`，保存版本优先。保存的 Recipe 位于 `.embodit_cache/deploy/recipes/`。

`config/local/`、`.embodit/` 与 `.embodit_cache/` 均被 Git 忽略，并可能包含凭据、私有主机、路径、Prompt 与部署状态；不要发布或附加到公开 Issue。SSH 认证优先使用 Key 或 `password_env`，不要内嵌明文密码。Git 忽略规则不是访问控制：`config/local/` 应保持 `0700`，其中的私有 JSON 文件应保持 `0600`。

`data/review.json` 是应用默认读取的版本化配置，Fork 可直接定制；需要把本机原因配置留在仓库外时设置 `EMBODIT_REVIEW_CONFIG`。详见[部署指南](../docs/deployment/README.zh-CN.md)与[安全策略](../SECURITY.md)。
