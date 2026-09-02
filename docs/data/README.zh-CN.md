# 数据工作台指南

[English](README.md) · **中文**

Embodit 提供本地机器人数据浏览、复核、标注、自动质检、转换与合并工作台。源数据按只读对象处理，派生数据写入新路径。

## 1. 启动与路径范围

```bash
bash embodit.sh start /path/to/datasets
```

该路径是工作台初始目录。localhost 模式下它不是安全边界：通过认证的客户端仍可请求服务账号有权访问的其他绝对路径。非本地监听会自动启用 `EMBODIT_SANDBOX=1`，把客户端提交的数据路径限制在该根目录；内部状态与缓存目录仍单独保存。

首次启动只同步 lockfile 中的**核心环境**，不会安装 CUDA、模型 Provider 专用环境、Checkpoint 或系统工具。

| 变量 | 作用 |
|---|---|
| `EMBODY_HOST` / `EMBODY_PORT` | 监听地址与端口，默认 `127.0.0.1:8765` |
| `EMBODY_PUBLIC_HOST` | 浏览器 URL 中显示的主机，不负责配置网络 |
| `EMBODIT_SANDBOX=1` | 将客户端提交的路径限制在数据根目录 |
| `EMBODIT_PYPI_MIRROR` | `tsinghua`、`official` 或可信 Simple Index URL |
| `EMBODIT_CACHE_DIR` | 缓存、报告与后台任务状态目录 |
| `EMBODIT_REVIEW_CONFIG` | 自定义人工隔离原因 |
| `EMBODIT_HDF5_FPS` | 已识别 HDF5 数据缺少 FPS 时的回退值 |
| `EMBODIT_MCAP_GAP_S` | 单文件 MCAP 的 Episode 切分时间间隔 |

内置服务通过明文 HTTP 传输 Bearer Token。请限制在 localhost 或可信私网；局域网使用前阅读 [SECURITY.md](../../SECURITY.md)。

## 2. 当前支持范围

| 格式 | 识别与浏览 | 同格式子集 |
|---|---|---|
| LeRobot v2.1 | `meta/info.json`；兼容已识别的 `v2.0`/`v2.1` 布局 | 重建选中 Parquet 与 metadata；视频复制或硬链接 |
| LeRobot v3 | 兼容已识别的 `v3`/`v3.0` metadata 和分片数据/视频 | 重建选中分片与 metadata；视频复制或硬链接 |
| HDF5 | 已识别的 RoboMimic/Astribot 兼容 Episode 布局 | 重建一个包含选中 Episode 的 HDF5 文件 |
| MCAP | 单文件、顶层目录或一层嵌套目录 | 重写选中 Episode 时间窗内的 schema、channel 与 message |

浏览页在统一时间轴上展示 Episode、任务、相机、FPS、帧数和已识别的 state/action 时序。“统一时间轴对齐”不代表源传感器物理同步已经得到验证。

MCAP 相机、关节、数值和 pose 的发现依赖受支持 schema 与 topic 名称启发式规则。任意 HDF5 布局、ROS schema、标定记录、attachment 和自定义列不会被自动规范化。

## 3. 人工决定、QC 与标签相互独立

Embodit 明确区分三类状态：

| 状态 | 保存位置 | 作用 |
|---|---|---|
| 浏览页人工决定 | `*.review.json`（`pass/review/quarantine`） | 普通筛选导出的默认来源；也可显式传 Episode ID |
| QC 决定 | `.embodit_cache/reports/qc/` 中扫描专属 SQLite | 保存 `autoDecision`、可选 `manualDecision`、Finding 与审计历史 |
| 标签 | 数据集固定 JSONL sidecar | 添加 Episode/Frame/区间注释，不改变前两类决定 |

Review 文件必须为 v2 或 v3、文件名以 `.review.json` 结尾；覆盖已有文件时必须属于同一数据集。隔离原因来自 [`../../config/data/review.json`](../../config/data/review.json)。原因 ID 一经使用不要改名，停用时设置 `enabled: false`。

标签路径固定为：

- 目录数据集：`<dataset>/labels.jsonl`；
- 单文件数据集：`<file>.labels.jsonl`。

Web UI 可创建 Episode 与区间标签；后端 schema 还接受 Frame 标签。区间标签只是 metadata，不会让导出自动裁剪视频片段；导出单位仍是完整 Episode。

从 QC 筛选结果打开导出时，扫描查询会提供固定 Episode 集合。保存 QC 人工决定不会暗中改写浏览页 Review 文件，标签也不会暗中改变任何筛选结果。

## 4. 自动质检

| 档位 | 当前行为 | 用途 |
|---|---|---|
| `fast` | 完整性、低成本冻结采样、运动与夹爪检查；关闭曝光/模糊视觉质量和相机抖动 | 大规模首轮筛查 |
| `standard` | 完整性、冻结、曝光、模糊、相机抖动、运动与夹爪检查 | 日常扫描 |
| `deep` | 更高采样率与分辨率 | 最终审计 |

报告包含生效配置、轻量数据指纹、Detector 版本、Finding、证据区间、阈值、覆盖率和复核审计。该指纹由路径、大小、mtime 与结构信息组成，不是内容的密码学哈希。

| 字段 | 含义 |
|---|---|
| `integrityStatus` | 结构有效性；hard-invalid Episode 会隔离 |
| `usableRatio` | 扣除 `error`/`fatal` 区间并集后的可用时长比例 |
| `qualityScore` | 严重度、置信度与持续时间得分 |
| `coverage` | 已完成 Detector 权重 / 适用 Detector 权重 |
| `autoDecision` | 自动 `pass/review/quarantine` |
| `manualDecision` | 当前扫描内的人工覆盖（如有） |

默认策略保持保守：hard-invalid 或 fatal → `quarantine`；分数 ≥80、可用比例 ≥90%、覆盖率 ≥80% 且没有 error → `pass`；其他情况 → `review`。复核单条 Finding 会更新审计状态，但不会重新计算已保存分数；需要人工覆盖时应设置 Episode 级 QC 决定。

当前 Detector 主要覆盖结构完整性、视觉/信号质量以及部分运动与跨模态规则。完整传感器同步、设备物理限位、任务成功、重复数据、分布漂移和 train/eval 泄漏仍需设备 Profile 单独处理。

机器人数据不存在跨设备通用的质量阈值。内置默认值只是保守的证据生成起点，不代表质量认证。每种机器人、控制模式和相机布局都应维护独立的标注校准集；自动隔离以低误杀为优先，证据不明确时进入人工复核。报告会保存 Detector 与配置版本，规则变化后不会静默复用不兼容结果。

## 5. 子集导出与保真边界

所有子集写出都要求新输出路径。验证输出前请保留源数据。LeRobot、HDF5 和 MCAP 写出在其受支持写出路径中使用 staging 与禁止覆盖发布；失败或取消时，不会把部分暂存产物发布为目标。

“同格式子集”表示同一数据家族中可用的结构保持子集，不表示逐字节或容器级无损复制：

| 格式 | 保留内容 | 会重建或不保证保留 |
|---|---|---|
| LeRobot v2.1/v3 | 选中 Episode 样本、标准 feature、任务与媒体 | Episode/Frame 索引、分片、metadata 与统计 |
| HDF5 | 已识别 Episode 数组/图像和受支持 dtype | 根级对象/属性、多文件布局、未知 dialect 字段 |
| MCAP | 选中时间窗内受支持的 schema/channel/message | chunk/index/compression 布局、attachment、metadata record、无关 message |

`hardlink` 可节省 LeRobot 媒体空间，但要求文件系统支持，且输出与源媒体共享 inode；需要独立媒体副本时使用 `copy`。

## 6. 跨格式转换

| 路径 | 保真等级 | 主要边界 |
|---|---|---|
| LeRobot v2.1 ↔ v3 | `high` | 标准样本/媒体保留；metadata 与分片重建 |
| LeRobot ↔ HDF5 | `partial` | 媒体可能转码；时间戳/FPS 与 metadata 可能重建 |
| MCAP → LeRobot/HDF5 | `partial` | 只映射选中相机与标准或显式指定的数值时序 |
| LeRobot/HDF5 → MCAP | `partial` | 合成 state/action topic、JPEG 相机和 FPS 派生时间戳 |

转换只处理已识别任务、相机以及标准名称或显式映射的 state/action，不保留任意列、topic、标定、原始 ROS schema 或不规则时间戳结构。每个任务生成报告，记录 Episode/帧数、映射、warning 与已知损失。目录输出写入 `<output>/conversion_report.json`；单文件 HDF5 或 MCAP 输出写入 `<output-file>.conversion_report.json`。

| Mapping 字段 | 说明 |
|---|---|
| `fps` | 源数据没有可用 FPS 时必填 |
| `state_key` / `action_key` | 显式选择源时序 |
| `media_mode` | 兼容同格式媒体使用 `hardlink` 或 `copy` |
| `on_error` | `fail` 或 `skip`；`skip` 可能减少输出 Episode |
| `allow_camera_loss` | 允许跳过读取失败或无法提取的相机 |
| `state_topic` / `action_topic` | MCAP 输出数值 topic |
| `camera_topics` | MCAP 输出 `{camera_key: "/topic/name"}` |
| `mcap_image_quality` | MCAP JPEG 质量 `1..100`，默认 `90` |

模板见 [`../../config/data/convert.example.json`](../../config/data/convert.example.json)。

## 7. 严格合并

合并要求至少两个不同、非空、同格式数据集。预检比较 FPS（容差 `1e-6`）、robot type、相机/features、LeRobot schema、HDF5 dialect/group/dtype/非 Episode 轴形状，或 MCAP topic/encoding/schema identity。不兼容输入必须先显式转换或规范化。

源顺序决定输出 Episode 顺序，且输出必须不存在。选择复制标签时会重映射索引；目录数据集将 manifest/labels 写在输出内，单文件格式使用相邻 sidecar。`hardlink`/`copy` 主要影响 LeRobot 视频媒体。

## 8. 后台任务、缓存与清理

QC、转换和合并使用独立 worker；关闭浏览器不会停止任务。

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --expired
bash embodit.sh clean --cache
bash embodit.sh clean --all
```

`--cache` 删除可重建的媒体缓存；`--all` 还删除缓存根目录内的任务历史和 QC 报告。它不会删除数据集、派生输出、标签、Review 文件、部署状态、Python 环境或服务 Token/日志。清理前应归档重要报告。

保留策略会在服务启动时执行一次，并在服务运行期间定期执行。应在启动 Embodit 前配置：

| 变量 | 默认值 | 作用 |
|---|---:|---|
| `EMBODIT_MEDIA_TTL_DAYS` | `7` | 可重建的播放媒体缓存文件 |
| `EMBODIT_JOB_TTL_DAYS` | `30` | 已结束的导出、转换、合并和 QC 任务记录/日志 |
| `EMBODIT_TEMP_TTL_DAYS` | `1` | 临时/staging 产物 |
| `EMBODIT_QC_REPORTS_PER_DATASET` | `5` | 每个数据集保留的最近报告数；旧报告被任务引用时继续保留 |
| `EMBODIT_MAINTENANCE_INTERVAL_HOURS` | `24` | 定期清理间隔；正值最短按一小时执行，`0` 只关闭定期清理 |

TTL 或报告数设置为 `0` 时，匹配且未受保护的条目会立即满足清理条件；即使定期间隔为 `0`，启动时维护仍会执行。

## 9. 排查与扩展

| 问题 | 检查项 |
|---|---|
| 数据集未识别 | 版本标识、受支持 HDF5 布局或 MCAP 扫描深度 |
| 媒体不可用 | Codec/FFmpeg、文件权限与服务日志 |
| QC 覆盖率低 | 被跳过 Detector、相机映射与 state/action 维度 |
| 缺少 FPS | 设置转换 `fps` 或 `EMBODIT_HDF5_FPS` |
| 硬链接失败 | 使用 `copy`，或让源/输出位于同一文件系统 |
| 路径被拒绝 | 使用合适的数据根目录启动；局域网保持沙箱开启 |

扩展边界：数据适配器位于 `backend/datasets/`，QC Detector 位于 `backend/qc/detectors/`，转换位于 `backend/convert/`。贡献边界统一见 [CONTRIBUTING.md](../../CONTRIBUTING.md)。
