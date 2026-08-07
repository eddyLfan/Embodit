# 数据层使用指南

[English](README.md) · **中文**

数据层用于浏览、质检、复核、标注、筛选、转换、合并、增强和导出真机数据。源数据默认只读；标注写入 sidecar，批处理写入新路径。

## 1. 启动与路径范围

```bash
bash embodit.sh start /path/to/datasets
```

`/path/to/datasets` 是页面初始目录。localhost 模式默认允许访问服务账号可访问的其他绝对路径；非本地监听会自动设置 `EMBODIT_SANDBOX=1`，将所有读写限制在该根目录。

首次启动会同步安装全部依赖，成功后再启动服务。依赖未变化时，后续启动会通过环境指纹跳过同步。网络较慢时：

```bash
EMBODIT_PYPI_MIRROR=tsinghua bash embodit.sh setup
```

常用环境变量：

| 变量 | 作用 |
|---|---|
| `EMBODY_PORT` | Web 端口，默认 `8765` |
| `EMBODY_HOST` | 监听地址，默认 `127.0.0.1` |
| `EMBODY_PUBLIC_HOST` | 输出给浏览器的工作电脑地址 |
| `EMBODIT_SANDBOX=1` | 强制所有客户端路径位于数据根目录 |
| `EMBODIT_PYPI_MIRROR` | `tsinghua`、`official` 或可信 Simple Index URL |
| `EMBODIT_CACHE_DIR` | 缓存、报告和后台任务目录 |
| `EMBODIT_REVIEW_CONFIG` | 自定义人工隔离原因配置 |
| `EMBODIT_HDF5_FPS` | HDF5 无 FPS 元数据时的回退值 |
| `EMBODIT_MCAP_GAP_S` | 单文件 MCAP 按时间间隔拆 Episode 的阈值 |

## 2. 支持的数据格式

| 格式 | 识别方式 | 浏览内容 | 同格式子集 |
|---|---|---|---|
| LeRobot v2.1 | `meta/info.json` 中 `codebase_version=v2.1` | Parquet、MP4、task、state/action | 原生结构复制或硬链接 |
| LeRobot v3 | `codebase_version=v3.0` | Parquet、分片视频、task、state/action | 原生结构复制或硬链接 |
| RoboMimic HDF5 | `.h5/.hdf5` 和 Episode group | 数组、内嵌图像、state/action | 保持 HDF5 dtype/schema |
| MCAP | `.mcap` 文件或目录 | topic、CompressedImage、数值/pose 序列 | 保留原始 schema/channel/message |

打开数据集后，页面展示 Episode、任务、相机、FPS、帧数和可用时序字段。相机共用时间轴；state/action 按维度绘制。

## 3. 浏览、人工筛选与标签

### 3.1 人工筛选

每个 Episode 有三种人工状态：

- `pass`：纳入训练集候选；
- `review`：待确认；
- `quarantine`：隔离，不进入默认导出。

进度可以保存为 `*.review.json` 并重新载入。隔离原因来自 [`../../config/data/review.json`](../../config/data/review.json)：

```json
{
  "quarantineReasons": [
    {"id": "task_failed", "label": {"zh": "任务未完成", "en": "Task not completed"}, "enabled": true}
  ]
}
```

已使用的 `id` 不要修改；停用时设置 `enabled: false`。修改标签后刷新网页即可，无需重启。

### 3.2 标签

标签支持 Episode 级和时间区间级记录，可包含任务结果、质量、标签、备注、起止时间等字段。默认写入 `labels.jsonl`、`*.labels.jsonl` 或数据集旁路文件，不改写视频和表格。

## 4. 自动 QC

点击“自动质检”，选择扫描档位并启动后台任务：

| 档位 | 行为 | 用途 |
|---|---|---|
| `fast` | 完整性检测；低成本冻结采样；关闭视觉质量与抖动 | 大规模首轮筛查 |
| `standard` | 完整性、冻结、曝光、模糊、相机抖动、运动和夹爪 | 默认日常扫描 |
| `deep` | 更高采样率和分辨率 | 最终审计或疑难数据 |

配置模板是 [`../../config/data/qc.example.json`](../../config/data/qc.example.json)。QC 报告保存到 `.embodit_cache/reports/qc/`，包含生效配置、数据 fingerprint、Detector 版本、finding、时间区间、阈值、证据和人工复核记录。

### 4.1 结果字段

| 字段 | 含义 |
|---|---|
| `integrityStatus` | 数据结构是否有效；hard-invalid 直接隔离 |
| `usableRatio` | 扣除 `error/fatal` 时间区间并集后的可用比例 |
| `qualityScore` | 按严重度、置信度和持续时间计算的质量分 |
| `coverage` | 已完成 Detector 权重 / 适用 Detector 权重 |
| `autoDecision` | 自动 `pass/review/quarantine` |
| `manualDecision` | 人工覆盖；存在时优先生效 |

默认自动判定：

| 判定 | 条件 |
|---|---|
| `quarantine` | hard-invalid 或 `fatal` finding |
| `pass` | 分数 ≥80、可用比例 ≥90%、覆盖率 ≥80%，且没有 `error` |
| `review` | 其他情况 |

单条 finding 可标记 `confirmed/rejected/modified`。这会改变问题展示与审计记录，但不会重算已保存分数；要改变最终筛选结果，应设置 Episode 级人工决定。

检测器的精确阈值、issue code 和标定方法见 [`QC_STANDARD.zh-CN.md`](QC_STANDARD.zh-CN.md)。阈值使用数据集原生单位，正式批量扫描前必须用本体的已知好/坏样本校准。

## 5. 导出与格式转换

### 5.1 子集导出

在筛选页选择目标格式、输出绝对路径、媒体方式和是否复制标签：

- 同格式导出走原生无损子集路径；
- `hardlink` 节省空间，但源和输出必须在支持硬链接的文件系统；
- `copy` 独立复制媒体；
- 跨格式导出先固定 Episode 集合，再运行转换；
- 输出路径必须不存在，完成前写入 staging，成功后原子发布。

### 5.2 转换保真

| 路径 | 等级 | 行为 |
|---|---|---|
| 同格式 | `full` | 保留原生 payload 和 schema |
| LeRobot v2.1 ↔ v3 | `high` | 视频、state、action 保留；部分 metadata 重建 |
| LeRobot ↔ HDF5 | `partial` | 视频与内嵌帧互转，可能重编码并推断 FPS |
| MCAP → LeRobot/HDF5 | `partial` | 选取相机和数值 topic；其他 topic/标定可能丢失 |
| LeRobot/HDF5 → MCAP | `partial` | 合成 state/action topic；相机写为 JPEG `foxglove.CompressedImage`；时间戳按 FPS 重建 |

每次跨格式转换都会生成 `conversion_report.json`，记录输入输出、Episode/帧数、字段映射、warning 和已知损失。

### 5.3 `mapping` 字段

页面“映射 JSON”或 [`../../config/data/convert.example.json`](../../config/data/convert.example.json) 可设置：

| 字段 | 说明 |
|---|---|
| `fps` | 源格式没有 FPS 时必须填写 |
| `state_key` / `action_key` | 明确选择时序字段；不填时只匹配标准名称 |
| `media_mode` | 同格式媒体 `hardlink` 或 `copy` |
| `on_error` | `fail` 或 `skip`；控制时序读取失败行为 |
| `allow_camera_loss` | 个别源相机无法提取时允许跳过；默认失败 |
| `state_topic` / `action_topic` | 写 MCAP 时的数值 topic |
| `camera_topics` | 写 MCAP 时 `{camera_key: "/topic/name"}` |
| `mcap_image_quality` | MCAP JPEG 质量，`1..100`，默认 `90` |

示例：

```json
{
  "fps": 30,
  "state_key": "observation.state",
  "action_key": "action",
  "state_topic": "/robot/state",
  "action_topic": "/policy/action",
  "camera_topics": {
    "head": "/camera/head/compressed",
    "left_wrist": "/camera/left_wrist/compressed"
  },
  "mcap_image_quality": 92
}
```

## 6. 严格合并

合并要求至少两个不同、非空、同格式数据集。预检会比较：

- FPS，容差 `1e-6`；
- robot type、相机 keys、规范化 features；
- LeRobot Parquet 列和类型；
- HDF5 dialect、group、dtype 和非 Episode 轴形状；
- MCAP topic、encoding 和 schema identity。

不一致时不会自动猜测或转换。先显式转换/标准化，再合并。成功输出包含 Episode 来源映射和 `merge_manifest.json` 或 sidecar。

## 7. 视觉增强

增强必须先预览，再批量写出。亮度增强直接可用：

- `auto`：按多相机亮度统计估计 gain/gamma；
- `manual`：指定 gain/gamma。

物体换色和背景替换需要独立 CUDA/SAM3 环境：

```bash
export AUGMENT_PYTHON=/path/to/augment-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/datasets
```

SAM3 安装和许可证边界见 [`../../third_party/README.md`](../../third_party/README.md)。增强保留未修改字段、Episode 边界、时间戳和原始数值 dtype，并生成处理清单。

## 8. 后台任务、缓存与清理

QC、转换、合并和增强在独立 worker 中运行，关闭网页不会中断。页面任务栏可以查看进度、取消和清除已结束任务。

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --expired
bash embodit.sh clean --cache
bash embodit.sh clean --all
```

`--cache` 只清理可重建媒体/预览缓存；`--all` 还会清理任务历史和 QC 报告。重要报告应随训练数据归档。

## 9. 常见问题

| 问题 | 检查 |
|---|---|
| 数据集未识别 | 路径、格式标识、HDF5 group 或 MCAP 文件是否完整 |
| 视频无法播放 | 源视频编码、ffmpeg、文件权限和缓存日志 |
| QC 覆盖率低 | Detector 是否 skipped、相机命名和夹爪维度是否匹配 |
| 转换 FPS 报错 | 在 mapping 中显式设置 `fps` |
| 硬链接失败 | 改用 `copy`，或确保源/输出位于同一文件系统 |
| 路径超出根目录 | 调整启动数据根目录；不要在非本地监听中关闭路径保护 |

## 10. 扩展入口

| 目标 | 目录 |
|---|---|
| 新格式 | `backend/datasets/` |
| 新 QC Detector | `backend/qc/detectors/` |
| 新转换 | `backend/convert/` |
| 新增强 | `backend/augment/` |

实现边界见 [`../architecture.md`](../architecture.md)。
