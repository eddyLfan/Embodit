<h1 align="center">Embodit</h1>

<p align="center">
  <img src="images/Embodit_logo.png" alt="Embodit Logo" width="160" />
</p>

<p align="center">
  <b>Embodied Intelligence Toolkit</b><br />
  面向机器人与 VLA 数据集的本地可视化工作台
</p>

<p align="center">
  <a href="#支持格式"><img src="https://img.shields.io/badge/formats-LeRobot%20%7C%20HDF5%20%7C%20MCAP-0ea5e9" alt="formats" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="python" /></a>
  <a href="#快速开始"><img src="https://img.shields.io/badge/runtime-uv-green" alt="uv" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license" /></a>
</p>

<p align="center">
  <a href="README.md">English</a> | <b>中文</b>
</p>

Embodit 在浏览器中直接打开本地机器人数据集，提供 episode 浏览、人工质检、标注、筛选、格式转换、导出和视觉增强。数据默认留在本机，无需上传到第三方服务。

<!-- > 原始样本内容保持只读。审阅与标注写入旁路文件；导出、转换和增强结果写入新目录。 -->

## 核心能力

| 模块 | 能力 |
|---|---|
| **浏览** | 自动识别数据格式；多相机同步预览；按 episode 或任务检索 |
| **质检与标注** | 质量评分、成功/失败、标签、备注和区间标注（快捷键 `B`） |
| **自动质检与筛选** | 完整性硬检查、视频/动作/操作行为检测、区间证据、0–100 评分、人工复核和按条件导出 |
| **转换与导出** | 四种格式互转；同格式子集导出；后台任务与保真报告 |
| **视觉增强** | 自动/手动亮度；基于 SAM3 的物体换色和背景替换；预览确认后批量写出 |

### 自动质检与可视化复核

在**筛选**工作台点击“自动质检”启动后台扫描。扫描完成后，结果会直接回显到筛选页，而不是要求用户阅读一份长文本报告：

- Episode 列表显示合格时长比例和问题数，标题区显示当前 Episode 的质检摘要；
- 主时间轴以颜色块标出不合格区间，点击色块会从区间起点播放并在终点自动停止；
- 问题卡显示 issue code、严重度、时间、相机/信号、置信度和简要原因；
- 选中问题后才展开原始指标与实际阈值，可直接“确认问题”或标记为“误报”；
- 全局结果仍可按 issue、完整性、自动/人工决定、最低质量分和最低合格时长筛选，并导出数据集或 CSV。

```text
Episode 12 · 质量分 86.4 · 合格时长 93.1% · 待审
00:00  ────────── [ motion/jitter ] ───── [ visual/blur ] ─────  00:28
                       点击播放该段             点击查看证据
```

#### 结果状态如何理解

自动质检不是简单的“合格/不合格”二分类。一个 Episode 同时具有以下状态：

| 字段 | 含义 |
|---|---|
| `integrityStatus` | `valid` 表示数据结构可用；`invalid` 表示命中硬性完整性问题，导出时强制剔除 |
| `severity` | `warning` 为提示，`error` 为明确的区间质量问题，`fatal` 为严重问题 |
| `usableRatio` | 未被 `error` / `fatal` 区间覆盖的时长百分比；硬性不可用时固定为 `0` |
| `qualityScore` | 由问题严重度、置信度和覆盖时长计算的 `0–100` 分；硬性不可用时固定为 `0` |
| `coverage` | 已成功执行的 detector 权重占比；字段缺失导致 detector 跳过时会降低覆盖率 |
| `autoDecision` | 自动决定：`pass`、`review` 或 `quarantine`；可被人工决定覆盖 |

`warning` 或候选问题不等于自动隔离；普通 `error` 默认进入 `review`，只有硬性完整性问题或 `fatal` 才会自动进入 `quarantine`。

#### 硬性不可用判定

下表中的命中会设置 `hard_invalid=true`、`integrityStatus=invalid`、`qualityScore=0`、`usableRatio=0` 和 `autoDecision=quarantine`。此类 Episode 即使人工改成通过，也不会进入自动质检筛选导出。

| Issue code | 默认判定标准 |
|---|---|
| `integrity/missing_action` | `requirements.action=true`（默认）但不存在 `action` |
| `integrity/missing_state` | 用户启用“将 state 设为必需”但不存在 `observation.state` |
| `integrity/invalid_signal_shape` / `empty_signal` / `non_numeric_signal` | action/state 无法转成二维时序、任一维为空或不是数值信号 |
| `integrity/non_finite_signal` | action/state 任意位置包含 `NaN` 或 `Inf`；报告会记录时间范围和维度 |
| `integrity/signal_read_error` | 数据适配器无法读取该 Episode 的 action/state 时序 |
| `integrity/signal_length_mismatch` | 信号长度与 Episode 帧数的差值大于 `max(2 帧, Episode 帧数 × 10%)` |
| `integrity/action_state_length_mismatch` | action 与 state 的长度差大于 `max(2 帧, 较长信号长度 × 10%)` |
| `integrity/missing_required_camera` | 相机数少于 `minimumCameras=1`，或缺少 `requiredCameraKeys` 中的相机 |
| `integrity/missing_required_camera_pattern` | 没有相机 key 匹配用户配置的 `requiredCameraPatterns` |
| `integrity/video_source_unavailable` / `video_decode_error` / `empty_video` | 无法建立视频帧源、解码过程报错或没有任何可解码帧 |
| `integrity/video_frame_count_mismatch` | 可解码帧数 / 期望帧数小于 `minimumDecodedRatio=0.9` |
| `integrity/video_frozen` | 先按下表的冻结规则定位区间；所有冻结区间总时长达到 Episode 时长的 `50%` |

#### 区间质量与行为判定

这些问题会在时间轴上显示证据区间。除特别说明外，它们不会使数据结构变为 `invalid`，而是影响合格时长、质量分和自动决定。

| Issue code | 严重度 | 默认判定标准 | 扫描档位 |
|---|:---:|---|---|
| `visual/frozen` | error | 相邻采样灰度帧平均绝对像素差 `≤ 0.75`，持续 `≥ 2.0s`；冻结总时长低于 Episode 的 `50%` | 全部 |
| `visual/dark` | warning | 灰度均值 `< 40`，持续 `≥ 0.5s` | 标准、深度 |
| `visual/overexposed` | warning | 灰度均值 `> 245`，持续 `≥ 0.5s` | 标准、深度 |
| `visual/blur` | warning | 灰度图 Laplacian 方差 `< 35`，持续 `≥ 0.5s` | 标准、深度 |
| `visual/camera_shake` | error | 静态相机的全局光流向量变化 `> 4.0`，且一致运动像素比例 `≥ 0.45` | 标准、深度 |
| `motion/jitter` | error | 默认只检测 `action`；无 action 时回退到 state。维度的 P99–P1 量程须 `≥ 0.001`，同一维度 acceleration robust-z `> 8` 且相对量程 `> 0.15`，jerk robust-z `> 8` 且相对量程 `> 0.30`，二者在 `±1` 帧内共同命中并持续 `≥ 0.08s` | 全部 |
| `motion/stationary` | warning | 对齐后的 action/state 每帧最大绝对变化 `≤ 0.0005`，持续 `≥ 3.0s` | 全部 |
| `motion/near_zero_episode` | error | 整条 Episode 中所有可用 action/state 维度的最大活动范围 `< 0.01` | 全部 |
| `manipulation/gripper_chatter` | error | 名称匹配 `gripper` / `finger` / `jaw` 的维度，单帧变化 `≥ 0.2` 的次数超过 `4 次/s` | 全部 |
| `manipulation/regrasp_candidate` | warning | `5s` 窗口内出现至少三次夹爪变化，且方向呈“闭合–张开–再闭合”等交替模式；`allowMultipleGrasps=false` 时启用 | 全部 |

补充说明：

- jitter 使用“统计异常 + 相对信号量程 + acceleration/jerk 联合命中”，避免近静止维度的浮点噪声被放大；默认不把真实 state 动力学变化重复判为控制指令抖动。
- 相机抖动只检查配置为静态的相机。未配置 `staticCameraKeys` 时，会推断名称含 `base`、`head`、`main`、`front`、`overhead` 且不含 `wrist`、`hand`、`eef` 的相机。
- 夹爪检测依赖字段名。使用 `command.0` 之类匿名维度的数据集应通过 `gripper.namePatterns` 补充名称模式，否则 detector 会显示为 skipped。
- 默认阈值是跨数据集的保守起点，不是与机器人型号无关的物理安全标准。位置单位、控制频率或相机曝光差异较大时，应使用已知好/坏样本校准。

#### 评分与自动决定

区间按并集计算，重叠问题不会重复扣除合格时长：

```text
usableRatio = 100 × (Episode 时长 - error/fatal 区间并集时长) / Episode 时长
qualityScore = 100 - 按严重度、置信度和持续时长计算的区间罚分 - 全局问题罚分
```

默认严重度权重为 `info=0`、`warning=0.25`、`error=1.0`、`fatal=1.0`；同一时段有多个问题时取最大权重而不是累加。没有明确区间的全局 `warning` / `error` / `fatal` 分别最多产生 `5` / `25` / `100` 分罚分。

| 自动决定 | 条件 |
|---|---|
| `pass` | `qualityScore ≥ 80`、`usableRatio ≥ 90%`、`coverage ≥ 80%`，并且没有任何 `error` |
| `quarantine` | 命中硬性不可用或存在 `fatal` |
| `review` | 除上述两种情况外；包括 warning、普通 error、覆盖率不足或分数未达标 |

人工可确认/驳回单个 finding，或覆盖 Episode 决定。原始检测结果、人工复核和调整后的区间会分开保留，便于审计。

#### 扫描档位、性能与缓存

| 档位 | 区别 |
|---|---|
| 快速 | 完整解码验证视频；冻结采样 `3 FPS / 128px`；跳过画质和光流相机抖动；默认 `4 × 2` workers |
| 标准 | 冻结 `5 FPS / 160px`，画质 `4 FPS / 320px`，相机抖动 `2 FPS / 320px`；默认 `2 × 2` workers |
| 深度 | 冻结 `10 FPS / 240px`，画质 `8 FPS / 480px`，相机抖动 `4 FPS / 480px`；默认 `2 × 2` workers |

所有档位都会完整执行信号完整性、视频可解码性、冻结、动作和夹爪检查。MP4 未采样帧无需转换为 RGB；实际 Episode 并行度会根据 CPU 预算自动下调，SQLite 始终由主线程串行写入。worker 数可在界面或 `runtime.episodeWorkers` / `runtime.cameraWorkers` 中以 `1–16` 覆盖。

每次扫描保存为独立 SQLite 报告，默认位于 `.embodit_cache/reports/qc/<dataset-id>/`。只有数据集指纹和完整配置哈希同时匹配时才会复用缓存；检测器阈值或配置版本变化后必须重新扫描。可用 `EMBODIT_CACHE_DIR` 更改统一缓存根目录，完整示例见 [`config/qc.example.json`](config/qc.example.json)。

#### 缓存目录与清理

所有任务状态和可再生成文件统一放在 `.embodit_cache/`：

```text
.embodit_cache/
├── jobs/{convert,augment,qc}/
├── previews/augment/
├── media/{hdf5,mcap}/
├── reusable/sam_tracks/
└── reports/qc/
```

服务每次启动时会将旧的 `.convert_jobs`、`.augment_jobs`、`.augment_previews`、`.augment_cache` 和 `/tmp/embody-*-video` 内容移动到统一缓存目录，不保留兼容软链接，然后执行一次保留策略；持续运行时默认每 24 小时再执行一次。默认清理 7 天未使用的预览和播放视频、30 天未使用的 SAM3 缓存、30 天前结束的任务，并为每个数据集保留最近 5 份未被任务引用的 QC 报告。运行中的任务及其报告不会被删除。

```bash
bash embodit.sh clean --dry-run  # 查看默认策略将删除什么，不写磁盘
bash embodit.sh clean            # 清理过期内容
bash embodit.sh clean --cache    # 清空所有可重新生成的预览、媒体和 SAM 缓存
bash embodit.sh clean --all      # 清空统一目录，包括任务历史和 QC 报告
```

实际清理前需要先停止服务。上述命令不会删除数据集输出、标注、人工复核、`.venv`、服务日志或访问令牌。输出目录旁的 `.*.building-*` staging 为保证同文件系统原子提交而不会移入缓存根目录；正常成功、失败或取消时会自动清理。

## 效果展示

### 数据集概览

在统一工作台中查看数据集信息、episode 列表、多相机画面以及状态与动作轨迹。

<p align="center">
  <img src="images/overview.png" alt="Embodit 数据集概览界面" width="100%" />
</p>

<table>
  <tr>
    <td width="50%"><img src="images/annotation.png" alt="Embodit 数据标注界面" /></td>
    <td width="50%"><img src="images/augmentation.png" alt="Embodit 数据增强预览界面" /></td>
  </tr>
  <tr>
    <td align="center"><b>质检与标注</b><br />记录评分、标签、备注和区间标注</td>
    <td align="center"><b>数据增强</b><br />对比原始与增强结果，确认后批量写出</td>
  </tr>
</table>

## 快速开始

### 环境要求

- Python 3.10 或更高版本
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# 安装 uv（如尚未安装）
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/eddyLfan/Embodit.git
cd Embodit
```

### 启动服务

```bash
bash embodit.sh start /path/to/your/data
```

首次启动会根据 `pyproject.toml` 和 `uv.lock` 创建 `.venv` 并安装依赖。服务就绪后，终端会输出访问地址：

```text
http://localhost:8765/?token=<random>
```

首次访问会将令牌换成 HttpOnly Cookie，之后可直接打开 `http://localhost:8765/`。

```bash
bash embodit.sh stop     # 停止服务
bash embodit.sh logs -f  # 持续查看日志
bash embodit.sh help     # 查看所有命令
```

## 使用流程

1. 指定数据路径，由 Embodit 自动识别格式和 episode。
2. 在筛选页运行自动质检，通过时间轴异常区间逐段播放和复核证据。
3. 同步查看相机、状态与动作，记录评分、标签和备注。
4. 将 episode 标记为 `pass`、`review` 或 `quarantine`。
5. 按筛选结果导出子集，或转换为目标格式。
6. 可选：预览视觉增强效果，确认后批量写入新数据集。

## 支持格式

| 格式 | 支持的典型布局 |
|---|---|
| **LeRobot v2.1** | parquet、视频与元数据目录 |
| **LeRobot v3** | LeRobot v3 目录数据集 |
| **HDF5** | RoboMimic 风格 `.hdf5` / `.h5` 单文件或目录 |
| **MCAP** | 单文件、顶层多文件或一层分片目录 |

转换能力矩阵（行：源格式；列：目标格式）：

| → | LeRobot v2.1 | LeRobot v3 | HDF5 | MCAP |
|---|:---:|:---:|:---:|:---:|
| **LeRobot v2.1** | full | high | partial | partial |
| **LeRobot v3** | high | full | partial | partial |
| **HDF5** | partial | partial | full | partial |
| **MCAP** | partial | partial | partial | full |

- `full`：同格式无损子集导出
- `high`：保留主体数据，可能调整布局或元数据
- `partial`：可能损失字段、标定或格式专属元数据

具体损失会显示在界面中，并写入 `conversion_report.json`。字段和 topic 映射示例见 [`config/convert.example.json`](config/convert.example.json)。

## 配置

### 环境变量

| 变量 | 说明 | 默认值 |
|---|---|---|
| `EMBODY_ROOT` | 数据浏览根目录 | 启动参数或当前目录 |
| `EMBODY_HOST` | 服务监听地址 | `127.0.0.1` |
| `EMBODY_PORT` | 服务端口 | `8765` |
| `EMBODY_PUBLIC_HOST` | 终端输出 URL 使用的主机名 | `localhost` |
| `EMBODY_TOKEN` | 访问令牌 | `config/token` 或随机生成 |
| `EMBODY_PROXY` | `uv` 下载使用的 HTTP(S) 代理 | 空 |
| `EMBODIT_SANDBOX` | 将可访问路径限制在浏览根目录内 | 关闭；设为 `1` 开启 |
| `EMBODIT_CACHE_DIR` | 统一缓存与任务状态根目录 | `.embodit_cache` |
| `EMBODIT_PREVIEW_TTL_DAYS` | 预览任务及资源保留天数 | `7` |
| `EMBODIT_MEDIA_TTL_DAYS` | HDF5/MCAP 播放视频缓存保留天数 | `7` |
| `EMBODIT_SAM_CACHE_TTL_DAYS` | SAM3 分割缓存保留天数 | `30` |
| `EMBODIT_JOB_TTL_DAYS` | 已完成/失败/取消任务记录保留天数 | `30` |
| `EMBODIT_TEMP_TTL_DAYS` | 崩溃遗留临时文件和孤儿预览的保留天数 | `1` |
| `EMBODIT_QC_REPORTS_PER_DATASET` | 每个数据集至少保留的最近 QC 报告数 | `5` |
| `EMBODIT_MAINTENANCE_INTERVAL_HOURS` | 自动维护间隔；设为 `0` 禁用周期维护 | `24` |
| `EMBODIT_HDF5_FPS` | HDF5 缺少帧率时的回退值 | `20` |
| `EMBODIT_MCAP_GAP_S` | MCAP 按时间间隙切分 episode 的阈值（秒） | `2` |
| `AUGMENT_PYTHON` | SAM3 worker 使用的 Python | 当前解释器 |
| `AUGMENT_SAM3_CHECKPOINT` | SAM3 checkpoint 路径 | `checkpoints/sam3.pt` |

兼容旧环境变量：`LEROBOT_*` 与对应的 `EMBODY_*` 等价。

### 局域网访问

服务默认仅监听本机。如需局域网访问，请设置固定强令牌，并通过防火墙或反向代理限制来源：

```bash
EMBODY_HOST=0.0.0.0 \
EMBODY_PUBLIC_HOST=<server-ip> \
EMBODY_TOKEN=<strong-random-token> \
bash embodit.sh start /path/to/your/data
```

## 数据增强

亮度、掩码换色和背景替换算法已内置。亮度增强可直接使用；颜色增强还需要 Meta SAM3、兼容的 PyTorch/CUDA 环境以及已获授权的 checkpoint。

```bash
# 在独立 Python 3.12 环境中按显卡/CUDA 版本安装 PyTorch，然后：
git clone https://github.com/facebookresearch/sam3.git ../sam3
python -m pip install -r config/augment-worker-requirements.txt
python -m pip install -e ../sam3

export AUGMENT_PYTHON=/path/to/sam3-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/your/data
```

SAM3 的环境要求和 checkpoint 获取方式以 [`facebookresearch/sam3`](https://github.com/facebookresearch/sam3) 为准；许可证说明见 [`third_party/README.md`](third_party/README.md)。未配置 SAM3 时，颜色增强会被禁用并显示缺失项，不影响其他功能。

批量增强必须关联一次成功预览。预览后修改亮度、prompt、颜色、应用方式或 GPU，必须重新生成预览。

### 增强输出保真度

增强结果输出为 LeRobot v2.1 或 v3，保留：

- 相机视频
- `observation.state`（如存在）
- `action`（如存在）
- episode 任务信息

自定义表格字段、标定、源统计和格式专属元数据不会复制；视频会重新编码为 H.264。因此增强输出的保真等级为 `partial`，详情记录在 `meta/augmentation_report.json`。

## 数据写入与安全

Embodit 不修改原始 episode payload，但会在源路径旁写入审阅和标注旁路文件：

```text
<source>/
  <name>.review.json      # 目录数据集的人工筛选结果
  labels.jsonl            # 评分与标注

  # 单文件 HDF5 / MCAP：
  <file>.review.json
  <file>.labels.jsonl
```

导出、转换和增强必须写入源数据集之外的新路径：

```text
<output>/
  selection_manifest.json
  conversion_report.json          # 转换保真与已知损失
  meta/augmentation_report.json   # 增强保真与已知损失
```

输出路径不得等于或位于源数据集内部。

## 开发与测试

```bash
uv sync --extra dev
uv run pytest -q

uv run python backend/app.py \
  --host 127.0.0.1 \
  --port 8765 \
  --browse-root /path/to/data \
  --token dev-token
```

## 项目结构

```text
.
├── backend/
│   ├── app.py          # FastAPI 入口
│   ├── datasets/       # 格式探测、适配器、帧读取与导出
│   ├── labels/         # 标注 schema 与存储
│   ├── convert/        # 格式转换和后台任务
│   ├── augment/        # 内置视觉增强与 SAM3 适配
│   └── qc/             # 自动质检检测器、评分、SQLite 报告与后台任务
├── web/                # 浏览器 UI 与中英文文案
├── tests/              # 自动化测试
├── config/             # 示例配置和 worker 依赖
├── checkpoints/        # 可选 SAM3 权重（不纳入 Git）
├── embodit.sh          # 服务管理命令
├── pyproject.toml
└── uv.lock
```

## 版本与贡献

当前版本：**v0.1.0**。

欢迎提交 Issue 和 Pull Request。请提供涉及的数据格式、复现步骤和预期行为。

## 许可证

Embodit 采用 [MIT License](LICENSE)。SAM3 等第三方组件适用各自许可证。
