<h1 align="center">Embodit</h1>

<p align="center">
  <img src="images/Embodit_logo.png" alt="Embodit Logo" width="160" />
</p>

<p align="center">
  <b>Embodied Intelligence Toolkit</b><br />
  面向机器人与 VLA 数据集的本地可视化工作台
</p>

<p align="center">
  <a href="#功能"><img src="https://img.shields.io/badge/formats-LeRobot%20%7C%20HDF5%20%7C%20MCAP-0ea5e9" alt="formats" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="python" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/runtime-uv-green" alt="uv" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> | <b>中文</b>
</p>

Embodit 可在浏览器中直接打开本地数据集，完成 episode 浏览、人工质检、标注、筛选、格式转换和视觉增强。**原始样本内容始终只读**：审阅与标注写入旁路文件，导出、转换和增强结果写入新目录。

> 数据默认留在本机，无需上传到第三方服务。

**文档导航：** [功能](#功能) · [典型工作流](#典型工作流) · [支持格式](#支持格式) · [快速开始](#快速开始) · [配置](#配置) · [数据写入约定](#数据写入约定) · [更新情况](#更新情况)

## 功能

| 模块 | 说明 |
|---|---|
| **浏览** | 自动识别格式，多路相机同步时间轴预览，按 episode / 任务搜索 |
| **标注** | episode 质量分、成功/失败、标签与备注；区间多断点标注（快捷键 `B`） |
| **筛选** | 人工决策 `pass` / `review` / `quarantine`，支持批量与导出时按状态过滤 |
| **转换** | 四种格式互转，任务在后台运行，产出 `conversion_report.json` 与保真级别提示 |
| **增强** | 亮度自适应 / 手动调节；基于 SAM3 的物体换色与背景替换（先预览再批量写出） |
| **导出** | 默认保持源格式子集导出（硬链/复制）；可选导出时一并转换 |

自动筛选入口已预留，规则引擎待标准确认后接入。

## 典型工作流

1. **打开数据集**：指定本地数据目录，Embodit 自动识别数据格式与 episode。
2. **浏览与质检**：同步查看多路相机、状态和动作数据，记录质量分与备注。
3. **标注与筛选**：添加区间标签，将 episode 标记为 `pass`、`review` 或 `quarantine`。
4. **导出或转换**：按筛选状态导出子集，并按需转换为目标格式。
5. **可选增强**：预览亮度、换色或背景替换效果，再批量写入新目录。

## 支持格式

| 格式 | 典型布局 |
|---|---|
| **LeRobot v2.1** | 目录型数据集（parquet + 视频 / 元数据） |
| **LeRobot v3** | 目录型数据集 |
| **HDF5** | 单文件或目录（RoboMimic 风格 `.hdf5` / `.h5`） |
| **MCAP** | 单文件、顶层多文件，或一层分片子目录 |

转换能力矩阵（行是源格式，列是目标格式）：

| → | LeRobot v2.1 | LeRobot v3 | HDF5 | MCAP |
|---|:---:|:---:|:---:|:---:|
| **LeRobot v2.1** | full | high | partial | partial |
| **LeRobot v3** | high | full | partial | partial |
| **HDF5** | partial | partial | full | partial |
| **MCAP** | partial | partial | partial | full |

保真级别：`full` 表示同格式无损子集导出；`high` 表示主体数据可保留；`partial` 表示存在字段、标定或元数据损失。实际损失会显示在界面并写入 `conversion_report.json`。

## 快速开始

### 1. 准备环境

仅需：

- Python 3.10 或更高版本
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# 如未安装 uv
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/eddyLfan/Embodit.git
cd Embodit
```

### 2. 启动

```bash
bash start.sh /path/to/your/data
```

首次启动会根据 `pyproject.toml` 和 `uv.lock` 创建 `.venv` 并安装依赖。服务就绪后，终端会打印带访问令牌的 URL：

```text
http://localhost:8765/?token=<random>
```

在浏览器中打开该 URL。首次访问时令牌会换成 HttpOnly Cookie，随后可直接访问 `http://localhost:8765/`。

### 3. 管理服务

```bash
bash stop.sh          # 停止服务
tail -f service.log   # 查看日志
bash start.sh --help  # 查看启动参数
```

浏览 / 标注 / 筛选 / 转换开箱即用。**增强**（亮度 / SAM3 换色）为可选项，见下方 [可选：数据增强](#可选数据增强)。

## 配置

### 依赖环境

| 层级 | 说明 |
|---|---|
| **核心运行时** | 写在 `pyproject.toml`，锁定在 `uv.lock`。`start.sh` 执行 `uv sync` 再 `uv run`。 |
| **开发依赖** | `uv sync --extra dev`（可选）。 |
| **增强（可选）** | 外部算法目录 + 可选 Torch/CUDA 环境，不进核心安装。 |

### 环境变量

| 变量 | 含义 | 默认 |
|---|---|---|
| `EMBODY_ROOT` | 数据浏览根目录 | 启动参数；否则当前目录 |
| `EMBODY_HOST` | 监听地址 | `127.0.0.1` |
| `EMBODY_PORT` | 端口 | `8765` |
| `EMBODY_PUBLIC_HOST` | 打印到终端的对外主机名 | `localhost` |
| `EMBODY_TOKEN` | 访问令牌 | 持久化在 `config/token`；否则随机生成 |
| `EMBODY_PROXY` | 供 `uv` 下载用的 HTTP(S) 代理 | 关闭（空） |
| `EMBODIT_SANDBOX` | 限制路径不得越出浏览根 | 关闭；设 `1` 开启 |
| `EMBODIT_HDF5_FPS` | HDF5 缺省帧率 | `20` |
| `EMBODIT_MCAP_GAP_S` | MCAP 按时间间隙切 episode 的秒数 | `2` |

兼容旧名：`LEROBOT_*` 与对应的 `EMBODY_*` 等价。

默认仅监听 `127.0.0.1`。如需局域网访问，可设置 `EMBODY_HOST=0.0.0.0`，同时应设置固定的高强度 `EMBODY_TOKEN`，并通过防火墙或反向代理限制访问：

```bash
EMBODY_HOST=0.0.0.0 \
EMBODY_PUBLIC_HOST=<server-ip> \
EMBODY_TOKEN=<strong-random-token> \
bash start.sh /path/to/your/data
```

### 可选：数据增强

亮度 / 换色算法来自 `EMBODIT_AUGMENT_ROOT` 指向的外部包。

```bash
export EMBODIT_AUGMENT_ROOT=/path/to/data_strengthen   # 内含 augment/
export AUGMENT_PYTHON=/path/to/torch-env/bin/python    # SAM3 换色需要
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt        # 或放到 checkpoints/sam3.pt
bash start.sh /path/to/your/data
```

未配置时，浏览、标注、筛选和转换仍可正常使用，仅「增强」页不可用。

## 数据写入约定

Embodit 不改写原始 episode 内容，但会在源目录旁写入审阅和标注旁路文件：

```text
<source>/
  <name>.review.json      # 人工筛选决策（目录数据集）
  labels.jsonl            # 打分与标注
  # 单文件 HDF5 / MCAP：
  #   <file>.review.json
  #   <file>.labels.jsonl
```

导出 / 转换 / 增强结果写入**新路径**，例如：

```text
export_or_converted/
  selection_manifest.json
  conversion_report.json   # 转换时：映射、已知损失等
```

跨格式字段与 topic 映射可参考 [`config/convert.example.json`](config/convert.example.json)。建议将输出路径设在源数据集之外，避免与原始数据混淆。

## 项目结构

```text
.
├── start.sh / stop.sh     # 一键启停（uv sync + uv run）
├── pyproject.toml         # 核心依赖
├── uv.lock                # 锁定版本
├── web/                   # 浏览器 UI（中英切换）
├── backend/
│   ├── app.py             # FastAPI 入口
│   ├── settings.py        # 环境可覆盖配置
│   ├── datasets/          # 格式探测、适配器、帧读取、导出
│   ├── labels/            # 标注 schema 与 JSONL 存储
│   ├── convert/           # 互转管线与后台任务
│   ├── augment/           # 视觉增强（可选外部算法）
│   └── qc/                # 自动筛选预留
├── config/                # token（已 gitignore）、转换示例配置
├── checkpoints/           # 可选 SAM3 权重
├── images/                # Logo 等文档资源
└── LICENSE
```

## 开发

```bash
uv sync
uv run python backend/app.py --host 127.0.0.1 --port 8765 \
  --browse-root /path/to/data --token=dev-token
```

## 设计原则

1. **源数据只读**：审阅与标注旁路落盘；转换 / 增强写出新数据集。
2. **显式保真**：跨格式转换在 UI 中标明 `full` / `high` / `partial` 及已知损失，避免静默产生错误结果。
3. **长任务可脱离浏览器**：转换与增强在独立进程后台跑，关页不中断。
4. **轻量启动**：核心依赖由 `uv sync` 安装；SAM3 / Torch 仅在配置增强时加载。

## 更新情况

当前版本：**v0.1.0**（首次公开发布）。

| 版本 | 日期 | 主要内容 |
|---|---|---|
| **v0.1.0** | 2026-07 | 支持 LeRobot v2.1 / v3、HDF5、MCAP 的浏览、标注、筛选、转换与导出；可选亮度 / SAM3 视觉增强；中英文 UI 与文档；基于 `uv` 的一键启动（`start.sh`） |

后续变更会继续写在本节（有正式 Release 时也会同步到 GitHub Releases）。

## 贡献

Issue 与 Pull Request 欢迎。请说明涉及的格式与复现路径。

## 许可证

本项目采用 [MIT License](LICENSE)。
