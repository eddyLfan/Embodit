# 项目架构与二次开发边界

## 目录结构

```text
project/
├── backend/
│   ├── app.py                 # FastAPI 组合入口
│   ├── datasets/              # 多格式统一读取、媒体与导出
│   ├── qc/                    # 质检规则、评分、报告与 worker
│   ├── augment/               # 增强算法、预览、任务与写出
│   ├── convert/               # 转换能力、pipeline、报告与 worker
│   ├── merge/                 # 数据集合并预检与执行
│   ├── labels/                # 标签 schema 与 sidecar 存储
│   └── deploy/                # Recipe v2 真机部署控制面
│       ├── assets/            # 自动上传到远端的独立运行时
│       ├── cli.py             # 部署 CLI
│       ├── recipe.py          # 严格配置 schema
│       ├── store.py           # 本地安全配置存储
│       ├── transport.py       # 工作电脑 SSH 传输
│       └── orchestrator.py    # 启动、监控、Live 与回滚
├── web/                       # 单页 Web 工作台
├── config/                    # 可提交的配置示例
├── examples/deployment/       # 用户模型与 ROS Client 配置示例
├── docs/data/                 # 数据层使用指南
├── docs/deployment/           # 真机部署层使用指南
├── tests/                     # 与模块对应的回归测试
└── embodit.sh                 # 唯一用户脚本入口
```

## 分层原则

1. `datasets` 只负责统一读取和格式语义，不感知 Web。
2. `qc`、`augment`、`convert`、`merge` 通过 datasets 接口访问数据，不分别实现格式解析。
3. 后台长任务使用各模块 jobs/worker，HTTP 请求不承载长时间计算。
4. `deploy` 是控制面；实时观测和动作链路位于机器人与模型服务器之间。
5. 真机部署只保留 Recipe v2，不再为旧 Profile/Session 维护双实现。
6. `deploy/assets` 是 Embodit 管理的远端程序；`examples` 只放用户需要复制或参考的内容。
7. 配置 schema、传输、编排、远端运行时相互分离，避免把 SSH、ROS 和模型预处理混在一个脚本中。

## 依赖方向

```text
web → backend/app.py → feature modules

qc/augment/convert/merge → datasets

deploy/orchestrator → recipe + store + transport + deploy/assets
deploy/assets → 远端 Python/ROS 环境（独立运行，不导入 Embodit 后端）
```

模块不应反向导入 `app.py`。远端资产必须保持可单文件上传运行，不能依赖工作电脑的源码路径。

## 测试与提交

功能变更至少包含对应单元测试；跨模块工作流增加 API 或 pipeline 回归。真机相关测试默认使用 FakeRunner，不连接真实主机、不上电、不发送动作。

```bash
UV_CACHE_DIR=/tmp/embodit-uv-cache uv run pytest -q
python3 -m compileall -q backend
bash -n embodit.sh
git diff --check
```
