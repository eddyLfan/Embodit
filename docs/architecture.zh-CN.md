# 项目架构与扩展边界

[English](architecture.md) · **中文**

Embodit 由本地 FastAPI 服务、无依赖静态 Web 客户端、数据格式适配器、独立数据 Worker 和机器人部署控制面组成。本文描述模块归属与依赖方向；用户行为以数据与部署指南为准。

## 仓库结构

```text
Embodit/
├── backend/
│   ├── app.py                  # HTTP、认证、路径边界与路由组合
│   ├── settings.py             # 环境变量派生的服务设置
│   ├── jobs_common.py          # 后台任务共享原子状态
│   ├── cache_manager.py        # 保留策略、清理与旧目录迁移
│   ├── datasets/               # 识别、统一视图、读写与发布
│   ├── qc/                     # Detector、评分、SQLite 报告与 Worker
│   ├── convert/                # 保真矩阵、Pipeline、报告与 Worker
│   ├── merge/                  # 严格预检与同格式合并
│   ├── augment/                # 预览、效果、SAM3 桥接、写出与 Worker
│   ├── labels/                 # 标签 Schema 与固定 JSONL sidecar
│   └── deploy/                 # Config/Recipe 校验与编排
│       └── assets/             # 上传到受管主机的独立运行时
├── web/                        # 静态 HTML/CSS/JS 客户端与回归测试
├── config/                     # 版本化数据/部署模板
├── examples/deployment/        # 可复制的模型与本体接入示例
├── docs/                       # 中英文用户与架构指南
├── tests/                      # 合成 Python 回归测试
├── third_party/models/         # 固定上游 gitlink，不含权重
└── embodit.sh                  # 受支持的服务/部署入口
```

运行状态与源码边界分离：

- `.embodit/`：PID、URL、Bearer Token、环境指纹和服务日志；
- `.embodit_cache/`：媒体、预览、任务、QC 报告和部署状态；
- `config/local/*.json`：非递归发现的私有本体/模型 Config。

三处路径均被 Git 忽略，并可能包含敏感信息。

## 依赖方向

```text
web → backend/app.py → 业务模块

qc / convert / merge / augment → datasets
业务 Worker → jobs_common + 对应 Pipeline

deploy/orchestrator → recipe + store + transport + deploy/assets
deploy/assets → 远端 Python/ROS/Provider 环境
```

- 业务模块不得反向导入 `app.py`。
- 格式专属解析与发布位于 `datasets/`；通用工作流消费统一 View/Payload。
- `app.py` 负责认证、客户端路径限制、请求级校验与分发，不重复实现业务逻辑。
- 浏览器是视图与控制器，不是任务、QC 或硬件安全状态的权威存储。
- 上传的部署 Asset 必须独立运行，不能依赖工作电脑 Python import 路径。
- Provider 接入引用固定上游子模块；Embodit 不复制其实现，也不附带 Checkpoint。

## 数据与状态模型

数据适配器暴露统一 `DatasetView`、Episode Payload、媒体和同格式导出入口。跨格式转换只写出已识别或显式映射字段，并在报告中说明保真边界，不得暗示容器级无损。

三类用户状态相互独立：

| 状态 | 归属模块 | 持久化位置 |
|---|---|---|
| 浏览页决定 | Review API/Web 工作台 | 与数据集绑定的 `*.review.json` |
| QC 决定/Finding | `qc/store.py` | 扫描专属 SQLite 报告 |
| 语义标签 | `labels/` | 数据集固定 JSONL sidecar |

显式工作流可以把其中一种状态用作筛选输入，但模块不得暗中同步或覆盖另外两种。

## 任务与发布模型

转换、QC、合并和增强批处理在独立 Worker 中执行。`jobs_common.py` 提供带锁原子状态更新，避免迟到 Worker 把已取消或终态任务复活。请求级检查与媒体物化仍可能进行有界文件系统或 Codec 工作；阻塞操作不应占用异步事件循环。

Writer 必须拒绝已有输出、校验路径组成，并在支持时使用任务专属 staging，验证后再发布。各格式应明确自己的原子性边界；调用方不能假定所有容器 Writer 都具备事务性。

## 部署边界

部署模块是控制面。Embodit 将可复用本体/模型 Config 组合为 Recipe，执行预检，管理 SSH/systemd/隧道并记录编排状态。Dry Run 或 Live 中的实时观测—推理—动作循环位于本体 Client 与 Model Runner 之间，不经过 FastAPI。

离线评测是明确的旁路流程：它把一帧已记录数据发送给已准备模型，与记录动作比较，不连接机器人控制；同时复用编排中的模型 single-flight 保护。

Recipe、Config、Provider 与 Adapter 都属于可信的可执行控制材料。软件校验、限位和急停编排只能补充，不能替代独立硬件安全链路。

## 扩展入口

| 目标 | 主要位置 | 必须同步完成 |
|---|---|---|
| 数据格式 | `backend/datasets/` | 识别、View/Payload、保真文档、真实格式测试 |
| QC Detector | `backend/qc/detectors/` | 版本化配置、证据 Schema、标定测试/文档 |
| 转换路径 | `backend/convert/` | 能力矩阵、损失报告、失败/发布测试 |
| 增强能力 | `backend/augment/` | 预览一致性、Manifest、资源与保真说明 |
| 本体/模型接入 | `backend/deploy/` 或 `examples/deployment/` | Config 校验、FakeRunner 测试、安全接入说明 |
| Web 工作流 | `web/` | 中英文文案、键盘状态、前端回归 |

保持路由处理器精简，维持已公开持久化 Schema；确实无法兼容时必须提供迁移。

## 验证

```bash
uv sync --frozen --extra dev
uv run --no-sync pytest -q
python3 -m compileall -q backend
bash -n embodit.sh

node --check web/app.js
node --check web/i18n.js
node --check web/utils.js
node --test web/frontend-regressions.test.js

git diff --check
```

真机相关自动化回归默认使用 FakeRunner：测试不得连接真实主机、给机器人上电或发送动作。更多要求见 [CONTRIBUTING.md](../CONTRIBUTING.md) 与仓库 CI 工作流。
