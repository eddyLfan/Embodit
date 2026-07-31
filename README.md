# Embodit

<p align="center">
  <img src="images/Embodit_logo.png" alt="Embodit" width="160" />
</p>

<p align="center">
  <b>Embodied Intelligence Toolkit</b><br/>
  本地一键启动的具身智能数据工具：浏览 · 标注 · 筛选 · 转换 · 增强
</p>

<p align="center">
  <a href="#功能"><img src="https://img.shields.io/badge/formats-LeRobot%20%7C%20HDF5%20%7C%20MCAP-0ea5e9" alt="formats" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="python" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/runtime-uv-green" alt="uv" /></a>
</p>

Embodit 面向机器人 / VLA 数据采集与质检场景：在浏览器里打开本地数据集，完成人工审阅与标注，再按需导出、互转或做视觉增强。**源数据始终只读**——筛选决策、标签与导出结果都以旁路文件或新目录写出，不会改写原始样本。

## 功能

| 模块 | 说明 |
|---|---|
| **浏览** | 自动识别格式，多路相机同步时间轴预览，按 episode / 任务搜索 |
| **标注** | episode 质量分、成功/失败、标签与备注；区间多断点标注（快捷键 `B`） |
| **筛选** | 人工决策 `pass` / `review` / `quarantine`，支持批量与导出时按状态过滤 |
| **转换** | 四格式互转，后台任务不中断，产出 `conversion_report.json` 与保真级别提示 |
| **增强** | 亮度自适应 / 手动调节；基于 SAM3 的物体换色与背景替换（先预览再批量写出） |
| **导出** | 默认保持源格式子集导出（硬链/复制）；可选导出时一并转换 |

自动筛选入口已预留，规则引擎待标准确认后接入。

## 支持格式

| 格式 | 典型布局 |
|---|---|
| **LeRobot v2.1** | 目录型数据集（parquet + 视频 / 元数据） |
| **LeRobot v3** | 目录型数据集 |
| **HDF5** | 单文件或目录（RoboMimic 风格 `.hdf5` / `.h5`） |
| **MCAP** | 单文件、顶层多文件，或一层分片子目录 |

转换能力矩阵（同格式为无损子集导出；跨格式有不同程度损失，界面会标注）：

| → | LeRobot v2.1 | LeRobot v3 | HDF5 | MCAP |
|---|:---:|:---:|:---:|:---:|
| **LeRobot v2.1** | full | high | partial | partial |
| **LeRobot v3** | high | full | partial | partial |
| **HDF5** | partial | partial | full | partial |
| **MCAP** | partial | partial | partial | full |

## 快速开始

### 依赖

- Python ≥ 3.10
- [uv](https://github.com/astral-sh/uv)（`start.sh` 用它按需拉起依赖，无需手动 `pip install`）
- 颜色增强需要 [SAM3](https://github.com/facebookresearch/sam3) 权重（见下方环境变量）

### 启动

```bash
git clone <repo-url> Embodit
cd Embodit

# 将浏览根目录指向你的数据盘
bash start.sh /path/to/your/data
```

终端会打印带访问令牌的 URL，例如：

```text
http://localhost:8765/?token=<random>
```

首次打开后令牌会换成 HttpOnly Cookie，之后可直接访问 `http://localhost:8765/`。

停止服务：

```bash
bash stop.sh
```

日志：`tail -f service.log`

### 环境变量（可选）

| 变量 | 含义 | 默认 |
|---|---|---|
| `EMBODY_ROOT` | 数据浏览根目录 | 启动参数；未传时为 `/media/DATA` |
| `EMBODY_HOST` | 监听地址 | `127.0.0.1` |
| `EMBODY_PORT` | 端口 | `8765` |
| `EMBODY_PUBLIC_HOST` | 打印到终端的对外主机名 | `localhost` |
| `EMBODY_TOKEN` | 访问令牌 | 持久化在 `config/token`；否则随机生成 |
| `EMBODY_PROXY` | HTTP(S) 代理 | 内置占位 URL；**开源/外网环境请设为空字符串关闭** |
| `EMBODIT_SANDBOX` | 限制路径不得越出浏览根 | 关闭；设 `1` 开启 |
| `EMBODIT_HDF5_FPS` | HDF5 缺省帧率 | `20` |
| `EMBODIT_MCAP_GAP_S` | MCAP 按时间间隙切 episode 的秒数 | `2` |
| `AUGMENT_SAM3_CHECKPOINT` | SAM3 权重路径 | 需自行配置本地 `.pt` |

兼容旧名：`LEROBOT_*` 与对应的 `EMBODY_*` 等价。

推荐的本地启动示例：

```bash
EMBODY_PROXY= EMBODY_PORT=8765 bash start.sh ~/datasets
```

## 旁路文件约定

源目录旁路写入（不改原始 episode 内容）：

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

跨格式映射可参考 [`config/convert.example.json`](config/convert.example.json)。

## 项目结构

```text
.
├── start.sh / stop.sh     # 一键启停（nohup + uv）
├── pyproject.toml         # 项目元数据与核心依赖
├── web/                   # 浏览器 UI（中英切换）
├── backend/
│   ├── app.py             # FastAPI 入口
│   ├── settings.py        # 环境可覆盖配置
│   ├── datasets/          # 格式探测、适配器、帧读取、导出
│   ├── labels/            # 标注 schema 与 JSONL 存储
│   ├── convert/           # 互转管线与后台任务
│   ├── augment/           # 视觉增强（亮度 / SAM3 换色）
│   └── qc/                # 自动筛选预留
├── config/                # token、转换示例配置
├── images/                # Logo / favicon
└── tests/                 # pytest
```

## 开发与测试

```bash
# 安装开发依赖（可选）
uv sync --extra dev

# 跑测试
uv run --extra dev pytest tests/ -q
```

也可临时注入依赖：

```bash
uv run --with pytest --with pyarrow --with numpy --with h5py --with mcap --with pydantic \
  pytest tests/ -q
```

直接调试后端（需自行提供 token 与端口）：

```bash
uv run python backend/app.py --host 127.0.0.1 --port 8765 \
  --browse-root /path/to/data --token=dev-token
```

## 设计原则

1. **源数据只读**：审阅与标注旁路落盘；转换 / 增强写出新数据集。
2. **显式保真**：跨格式转换在 UI 中标明 `full` / `high` / `partial` 及已知损失，避免 silently wrong。
3. **长任务可脱离浏览器**：转换与增强在独立进程后台跑，关页不中断。
4. **轻量启动**：日常依赖由 `uv` 按需解析；颜色增强权重按需加载。

## 路线图

- [ ] 自动筛选规则引擎（标准确认后）
- [ ] 更完整的跨格式特征 / 标定保留
- [ ] 打包发布（PyPI / 容器镜像）与许可证定稿

## 贡献

Issue 与 Pull Request 欢迎。提交前请尽量保证 `pytest` 通过，并说明涉及的格式与复现路径。

## 许可证

开源许可证待定（计划在首次公开发布时补充 `LICENSE`）。若你 fork 使用，请暂时自行约定合规条款。
