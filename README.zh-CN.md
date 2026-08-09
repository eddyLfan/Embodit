<div align="center">
  <img src="images/Embodit_logo.png" alt="Embodit" width="180">
  <h1>Embodit</h1>
  <p><strong>面向具身数据治理与机器人模型部署的本地优先工作台。</strong></p>
  <p><a href="README.md">English</a> · <strong>中文</strong></p>
  <p>
    <a href="docs/data/README.zh-CN.md">数据指南</a> ·
    <a href="docs/deployment/README.zh-CN.md">部署指南</a> ·
    <a href="CONTRIBUTING.md">参与贡献</a> ·
    <a href="SECURITY.md">安全策略</a>
  </p>
</div>

> [!IMPORTANT]
> Embodit 目前是 pre-1.0 工程工具。数据工作台面向可复现的本地处理；机器人部署仍属实验功能，不是实时控制器或硬件安全系统。请备份重要数据、逐设备验证真实限位，并在 Live 实验中确保硬件急停始终可触达。

## 项目定位

真机模型开发是一条循环链路：检查采集数据、发现质量问题、准备下一版训练集、加载模型、验证输入与动作、执行受控评测，再把结果带回数据治理。这些步骤通常散落在不同脚本与终端。Embodit 将它们统一到一个本地服务和一套可复现配置中。

![Embodit workflow](images/Flowchart.png)

Embodit **不替代**数采 SDK、训练框架、机器人驱动、访问控制网关或独立硬件安全链路。

## 主要能力

| 领域 | 已包含能力 |
|---|---|
| 数据检查 | LeRobot v2.1/v3、已识别的 RoboMimic/Astribot 风格 HDF5、MCAP；统一时间轴相机、任务文本、state 与 action 时序 |
| 数据治理 | `pass/review/quarantine` 决策、Episode/区间标签、可配置原因、自动 QC、Finding 复核、CSV 报告 |
| 数据处理 | 原生子集导出、保真度感知转换、严格同格式合并、内置亮度增强、可选 SAM3 辅助颜色增强 |
| 模型接入 | 自定义 Python 模型、OpenPI、LeRobot、StarVLA 或已有兼容服务；仓库不附带模型权重 |
| 机器人部署 | 可组合本体/模型配置、Recipe v2、SSH/systemd 编排、受限模型隧道、ROS readiness、Dry Run、离线单帧评测、Live、监控、回滚与软件急停 |

### 支持的数据格式

| 格式 | 浏览 / 适用项 QC | 原生子集 | 跨格式转换 | 严格合并 |
|---|:---:|:---:|:---:|:---:|
| LeRobot v2.1 | ✓ | ✓ | ✓ | ✓ |
| LeRobot v3 | ✓ | ✓ | ✓ | ✓ |
| HDF5（`.h5`、`.hdf5`） | ✓ | ✓ | ✓ | ✓ |
| MCAP 文件或目录 | ✓ | ✓ | ✓ | ✓ |

原生子集保留源格式；跨格式转换可能重建 metadata、转码媒体、合成时间戳或舍弃源格式专属 topic。每次转换都会生成报告说明已知损失，详见[数据保真说明](docs/data/README.zh-CN.md#5-子集导出与保真边界)。

## 环境要求

核心工作台需要：

- Linux 与 Bash；
- Python 3.10+，且 `python3` 位于 `PATH`；
- [uv](https://docs.astral.sh/uv/)；
- 现代桌面浏览器；
- Git（用于克隆仓库和可选模型子模块）。

机器人部署还要求受管主机可通过 OpenSSH 访问，并具备可用的 systemd manager。ROS、CUDA、SAM3、模型专用 Python 环境、Checkpoint 和厂商 SDK 都是按工作流单独准备的可选组件。

Embodit 是应用仓库（`tool.uv.package = false`），不是常规 PyPI 库；`embodit.sh` 是受支持的服务与部署入口。

## 快速开始

```bash
git clone https://github.com/eddyLfan/Embodit.git
cd Embodit

# DATA_ROOT 是本地工作台初次打开的目录。
bash embodit.sh start /path/to/datasets
```

首次启动会先同步 lockfile 中的**核心依赖**，完成后再启动服务；`pyproject.toml` 和 `uv.lock` 未变化时，后续启动会跳过同步。终端会输出形如 `http://localhost:8765/?token=...` 的地址；首次请求把 Token 换成 30 天 HttpOnly Cookie，并重定向到不含 Token 的 URL。

只准备依赖、不启动服务：

```bash
bash embodit.sh setup
```

需要可信软件源镜像时：

```bash
EMBODIT_PYPI_MIRROR=tsinghua bash embodit.sh setup

# 或任意可信的 PEP 503 Simple Index。
EMBODIT_PYPI_MIRROR=https://mirror.example/simple bash embodit.sh setup
```

软件包版本和哈希仍由 `uv.lock` 固定；也支持 uv 原生 Index 配置和 `EMBODY_PROXY`。

### 服务命令

```bash
bash embodit.sh status
bash embodit.sh logs 100
bash embodit.sh logs -f
bash embodit.sh restart /path/to/datasets
bash embodit.sh stop

# 真正清理前必须停止服务。
bash embodit.sh clean --expired --dry-run
bash embodit.sh clean --expired
bash embodit.sh clean --cache
bash embodit.sh clean --all
```

清理命令只处理 Embodit 管理的缓存路径，不删除数据输出、标签、Review 文件、部署状态、虚拟环境或服务 Token/日志。

### 常用环境变量

| 变量 | 默认值 | 作用 |
|---|---|---|
| `EMBODY_ROOT` | 当前目录 | `start` 未传路径时的初始数据根目录 |
| `EMBODY_HOST` | `127.0.0.1` | 监听地址 |
| `EMBODY_PORT` | `8765` | Web 端口 |
| `EMBODY_PUBLIC_HOST` | `localhost` | 浏览器 URL 中显示的主机 |
| `EMBODY_TOKEN` | 自动生成并持久化 | 显式 Bearer Token |
| `EMBODY_PROXY` | 未设置 | 准备环境时使用的 HTTP(S) 代理 |
| `EMBODIT_SANDBOX` | 非本地监听自动开启 | 将客户端提交的数据路径限制在 `DATA_ROOT` |
| `EMBODIT_STATE_DIR` | `.embodit/` | PID、URL、Token、环境指纹和服务日志 |
| `EMBODIT_CACHE_DIR` | `.embodit_cache/` | 媒体缓存、任务、QC 报告与部署状态 |
| `EMBODIT_REVIEW_CONFIG` | `config/data/review.json` | 人工复核原因配置 |

数据相关环境变量全表见[数据指南](docs/data/README.zh-CN.md#1-启动与路径范围)。

## 数据工作流

1. 使用同时包含数据集和预期输出位置的目录启动 Embodit。
2. 检查 Episode metadata、相机、任务文本和 state/action 信号。
3. 运行自动 QC，再复核 Finding 和最终 Episode 决策。
4. Review 进度保存为 `*.review.json`；标签固定使用数据集 sidecar：目录数据集为 `labels.jsonl`，单文件数据集为 `<文件名>.labels.jsonl`。
5. 导出选中 Episode、转换格式、严格合并兼容数据集，或先预览再把增强结果写入新路径。

QC、转换、合并和增强运行在独立 worker 中，关闭浏览器不会停止任务。格式细节、保真边界、阈值和清理策略见[数据指南](docs/data/README.zh-CN.md)。

## 模型与机器人工作流

只初始化实际需要的 Provider 源码；独立自定义 Python Provider 或已有外部模型服务可以跳过。例如使用 LeRobot：

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive third_party/models/lerobot
git submodule status --recursive
```

将本体和模型模板都复制到非递归发现目录：

```bash
mkdir -p config/local
chmod 700 config/local
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/my-model.json
chmod 600 config/local/my-robot.json config/local/my-model.json
```

仓库模板只包含文档专用地址和 `/path/to/...` 占位路径。必须替换主机、SSH、ROS 接口、工作目录、Checkpoint、观测映射、动作维度、生命周期操作和物理限位。不要把模板原样用于真机。

组合并校验 Recipe：

```bash
export ROBOT_SSH_PASSWORD='<robot-password>'

bash embodit.sh recipe-compose \
  config/local/my-robot.json \
  config/local/my-model.json \
  --output /tmp/my-deployment.json

bash embodit.sh recipe-validate /tmp/my-deployment.json
bash embodit.sh recipe-run /tmp/my-deployment.json --mode dry_run
```

`recipe-run --mode live` 会发送真实动作。它要求交互式终端，始终先进入 Dry Run，并且必须在 60 秒内原样输入服务端生成的一次性短语才能提升为 Live。只有在只读预检、模型准备、动作形状/限位检查和独立硬件安全演练全部通过后才应使用。完整流程见[部署指南](docs/deployment/README.zh-CN.md)。

## 安全与隐私

- 服务没有内建 TLS 或多用户 RBAC。请仅在 localhost 或可信私网使用，禁止把 HTTP 端口直接暴露到公网；远程访问应使用带身份认证的 TLS 反向代理或 SSH 隧道。
- localhost 模式下，`DATA_ROOT` 只是初始浏览位置，不是安全边界；非本地监听默认自动开启路径保护，除非被显式关闭。
- 任何持有 Bearer Token 的人都能使用数据与部署 API。请保护 `.embodit/token`，怀疑泄露后轮换 `EMBODY_TOKEN`，并把 `.embodit/`、`.embodit_cache/`、日志、报告和 Recipe 视为敏感信息。
- 核心工作台不包含分析埋点或自动云上传。部署期间，观测、图像、状态和 Prompt 会通过已配置链路发送到选定模型主机。
- Recipe、自定义 Python Adapter/Provider、Checkpoint、子模块、数据集和原生媒体解析器都应视为可信输入，并以最小权限运行。

局域网访问或真机部署前，请先阅读 [SECURITY.md](SECURITY.md)。

## 文档

| 文档 | 内容 |
|---|---|
| [数据指南](docs/data/README.zh-CN.md) | 格式、Review、标签、QC、转换、合并、增强、任务与清理 |
| [部署指南](docs/deployment/README.zh-CN.md) | 本体/模型配置、Recipe 生命周期、安全、离线评测、Dry Run 与 Live |
| [第三方组件](third_party/README.md) | 固定源码集成、模型/SAM3 归属和许可证边界 |
| [参与贡献](CONTRIBUTING.md) | 开发环境、检查命令与 Pull Request 要求 |
| [安全策略](SECURITY.md) | 支持版本、漏洞报告、威胁模型与真机安全 |
| [变更记录](CHANGELOG.md) | 版本级变更 |

## 开发

开发环境、模块边界、完整验证矩阵和 Pull Request 要求统一见
[CONTRIBUTING.md](CONTRIBUTING.md)。

## 许可证与第三方软件

Embodit 自有源码采用 [MIT License](LICENSE)。Git 子模块、模型权重、Checkpoint、数据集、SAM3、FFmpeg 构建和其他第三方资产继续适用各自许可证及使用条款。Embodit 不分发 Provider Checkpoint 或 SAM3 权重。使用或再分发前，请阅读 [third_party/README.md](third_party/README.md) 和每项资产附带的许可证。
