# 数据层详细指南

[English](README.md) · **中文**

数据层负责数据集浏览、同步回放、自动质检、人工复核、标签、筛选、转换、合并、增强和保真导出。默认不覆盖源数据：写操作生成独立输出或显式 sidecar。

## 1. 启动与选择数据

```bash
bash embodit.sh start /path/to/datasets
```

不传路径时以当前目录作为浏览根目录。需要限制 API 客户端只能访问指定根目录时，启用路径沙箱：

```bash
EMBODIT_SANDBOX=1 bash embodit.sh start /allowed/root
```

当前识别 LeRobot v2.1、LeRobot v3、RoboMimic HDF5 和 MCAP。`backend/datasets/` 中的适配器提供统一的数据集、Episode、时序、图像和视频接口。统一访问接口不代表所有字段都能无损互换；导出前应查看转换能力和保真报告。

## 2. 浏览、回放与标注

选择 Episode 后，工作台同步相机流、state/action 信号、任务和主时间轴。标注写入 `labels.jsonl`、`*.labels.jsonl` 或 `*.review.json` 等 sidecar，不修改原始媒体和表格。生成的媒体预览位于 `.embodit_cache/media/`，可重新生成。

“不合格原因”的唯一配置入口是 [`../../config/data/review.json`](../../config/data/review.json)。直接编辑 `quarantineReasons` 数组即可：调整数组顺序会改变下拉顺序，修改 `label.zh/en` 会改显示名称，追加对象会新增原因。`id` 会写入审核进度，使用后应保持不变；不再使用的项请设为 `"enabled": false`，这样历史记录仍能正确显示。保存配置后刷新网页即可生效，无需重启服务。也可通过 `EMBODIT_REVIEW_CONFIG=/path/to/review.json` 使用外部配置文件。

## 3. 自动质检具体做什么

质检是一套确定性、带版本的规则流水线，不是学习得到的“好/坏”黑盒分类器。每条 finding 都记录稳定 issue code、检测器版本、严重度、置信度、阈值、实测证据，以及适用时的时间区间。最终生效的完整配置会写入 SQLite 报告。

三种档位改变扫描成本，不改变输出含义：

| 档位 | 视觉/冻结采样 | 默认并行度 | 适用场景 |
|---|---|---|---|
| `fast` | 关闭视觉质量与相机抖动；冻结检测 3 fps、宽 128 | 4 Episode / 2 相机 | 首轮结构和完整性筛查 |
| `standard` | 冻结 5 fps/160 px，视觉 4 fps/320 px，抖动 5 fps/320 px | 2 / 2 | 日常默认扫描 |
| `deep` | 冻结 10 fps/240 px，视觉 8 fps/480 px，抖动 8 fps/480 px | 2 / 2 | 最终审计或疑难视觉问题 |

所有档位都保留完整性解码。实际 Episode × 相机嵌套并行度还会受可用 CPU 数量限制。可编辑的完整默认值见 [`../../config/data/qc.example.json`](../../config/data/qc.example.json)。

## 4. 默认检测标准

以下是当前 `standard` 默认值。它们是初始基线，不是对所有机器人都成立的物理常量。

### 4.1 硬完整性规则

出现以下任一 hard-invalid 条件时，Episode 标记为 `invalid`，自动判定为 `quarantine`：

- 缺少必需的 `action`（`integrity/missing_action`，默认必需），或缺少被配置为必需的 `state`（`integrity/missing_state`，默认非必需）；
- action/state 为空、非数值、形状无法解析或包含 NaN/Inf（`integrity/empty_signal`、`integrity/non_numeric_signal`、`integrity/invalid_signal_shape`、`integrity/non_finite_signal`）；适配器读取异常为 `integrity/signal_read_error`；
- 信号长度与 Episode 长度偏差超过 `max(2 帧, 期望长度的 10%)`，包括 action/state 相互长度不兼容（`integrity/signal_length_mismatch`、`integrity/action_state_length_mismatch`）；
- 相机少于 1 路，或缺少配置中指定的相机 key/pattern（`integrity/missing_required_camera`、`integrity/missing_required_camera_pattern`）；
- 相机无法打开/解码、内容为空，或成功解码比例低于 90%（`integrity/video_source_unavailable`、`integrity/video_decode_error`、`integrity/empty_video`、`integrity/video_frame_count_mismatch`）；
- 有 action/state 运动交叉证据的冻结画面累计达到 Episode 时长的 50%（`integrity/video_frozen`）。无运动信号时只送人工复核，不自动 hard-invalid。

Hard-invalid Episode 会单独进入 `invalidEpisodes`，不会进入正常 selected 导出集合，避免高分或宽泛筛选意外放入结构上不可用的数据。

### 4.2 视频与视觉规则

| Issue code | 默认标准 | 严重度 |
|---|---|---|
| `visual/frozen` | 平均像素差 ≤0.75、变化像素占比 ≤1%，连续至少 2 秒；存在 action/state 时还要求区间内归一化运动量程 ≥2% | `error`；有跨模态运动证据且累计 ≥Episode 50% 时变为 `integrity/video_frozen`、hard `fatal` |
| `visual/dark` | 灰度均值 <40，连续至少 0.5 秒 | `warning` |
| `visual/overexposed` | 灰度均值 >245，连续至少 0.5 秒 | `warning` |
| `visual/blur` | Laplacian 方差 <35，连续至少 0.5 秒 | `warning` |
| `visual/camera_shake` | KLT 特征跟踪 + RANSAC 相似变换的全局运动变化 >4；至少 12 个内点、内点率 ≥0.5、4×4 网格覆盖 ≥0.15，连续至少 2 次变化 | `error` |

相距不超过 0.25 秒的视觉区间会合并。静态相机可以显式指定；列表为空时，名称含 `base`、`head`、`main`、`front`、`overhead` 的相机会成为候选，含 `wrist`、`hand`、`eef` 的相机会排除。命名不符合本体实际时必须覆盖该启发式规则。

### 4.3 运动与夹爪规则

| Issue code | 默认标准 | 严重度 |
|---|---|---|
| `motion/jitter` | 同一非夹爪维度在 1 帧内同时满足加速度 robust-Z >8、jerk robust-Z >8、P99−P1 信号量程 ≥0.001、加速度/量程比 >0.15、jerk/量程比 >0.3；0.4 秒短窗还必须有至少 1 次反向和 ≥0.5 的超额行程比，持续 ≥0.08 秒 | `error` |
| `motion/stationary` | 最大逐帧变化 ≤0.0005，连续至少 3 秒 | `warning` |
| `motion/near_zero_episode` | Episode 内最大信号量程 <0.01 | `error` |
| `manipulation/gripper_chatter` | 单次变化 ≥0.2 视为 transition，平均 >4 次/秒 | `error` |
| `manipulation/regrasp_candidate` | 不允许多次抓取时，5 秒内出现 3 次方向交替的夹爪变化 | `warning` |

抖动默认分析 `action`；缺少 action 时回退到第一个可用连续信号。包含 `gripper` 的维度不参与运动抖动。夹爪规则只分析名称含 `gripper`、`finger` 或 `jaw` 的维度；识别不到时检测器记为 skipped，覆盖率会下降。

运动和夹爪阈值使用数据集原生数值单位。关节弧度、归一化 action、笛卡尔米制和平二值夹爪显然需要不同阈值；批量导出前必须用有代表性的好/坏 Episode 校准。

标准依据、成熟工具对比、分层准入规则和标定流程见 [`QC_STANDARD.zh-CN.md`](QC_STANDARD.zh-CN.md)。

## 5. 评分与自动判定

Embodit 暴露三个独立指标，避免单一总分掩盖未完成的检测：

- `usableRatio`：从 100% 中扣除所有 `error`/`fatal` 时间区间的并集；warning 不扣可用时长。任一 hard-invalid 直接归零。
- `qualityScore`：从 100 分开始。每个时间片只积分重叠 findings 中最大的 `严重度权重 × 置信度`，避免重复扣分。默认权重为 info 0、warning 0.25、error/fatal 1。无时间区间的 Episode 级 finding 取最大惩罚而非累加：warning 5、error 25、fatal 100。Hard-invalid 直接归零。
- `coverage`：已完成检测器权重 / 适用检测器权重。默认权重为信号完整性 3、视频 3、运动 1.5、夹爪 1。失败或 skipped 会降低覆盖率，不会被误当作“没有问题”。

自动判定按以下顺序执行：

| 判定 | 精确规则 |
|---|---|
| `quarantine` | 任一 hard-invalid 或任一 `fatal` finding |
| `pass` | 质量分 ≥80、可用比例 ≥90、覆盖率 ≥80，且没有 `error` finding |
| `review` | 其余所有未隔离 Episode |

Warning 在三个阈值仍满足时可以通过；只要存在 error，即使区间很短、数值分数仍高，也必须进入 review。

## 6. 筛选与人工复核语义

筛选支持完整性状态、生效判定、最低质量/可用/覆盖率、issue code、任务文本或 Episode 索引搜索，以及按索引、分数、覆盖率和 finding 数排序。

生效判定优先使用 `manualDecision`，没有人工决定时使用 `autoDecision`。对单条 finding 执行 `confirmed`、`rejected`、`modified` 会记录审计信息和调整后的证据，但当前**不会重新计算**已保存的自动分数。标记为 `rejected` 的误报默认从时间轴、问题卡片、有效问题数和 issue 筛选中隐藏；原始 finding 与复核记录仍保留，可通过“查看已隐藏误报”恢复查看或撤销误报标记。要改变 Episode 是否按判定被选中，应设置 Episode 级人工决定；清除人工决定后恢复自动结果。

推荐工作流程：

1. 使用 `standard` 扫描有代表性的子集。
2. 检查全部 `quarantine`，并抽查部分 `pass`。
3. 用已知好/坏 Episode 校准相机命名规则和阈值。
4. 运行全量扫描，按生效判定和最低覆盖率组合筛选。
5. 导出前预览固定的 Episode 集合，并随输出保留 QC 报告。

QC 只负责暴露可测异常，不能判断任务语义是否成功、行为是否安全或异常运动是否是有意操作；这些仍需人工或任务专用标签。

## 7. 报告缓存与可复现性

报告以 SQLite 保存到 `.embodit_cache/reports/qc/`。只有配置 hash 和数据集 fingerprint 同时一致，且旧报告状态为 completed 时才会复用。Fingerprint 包括解析后的路径、格式、FPS、features、Episodes、相机、选定元数据，以及媒体文件大小/mtime；数据或配置变化会得到新结果。

报告保留检测器状态、findings、阈值、指标、判定、复核和审计日志。长期使用的数据集建议将报告和 QC 配置与导出子集一起归档。

## 8. 筛选、转换与保真标准

导出会先固定 Episode 集合，校验源/目标，重编号必要标识，写入保真报告，最后原子暴露完整输出。目标必须与源数据分离。

转换会显式报告保真等级：

| 路径 | 保真等级 | 预期行为 |
|---|---|---|
| 同格式子集 | `full` | 在支持的格式上保持原生无损子集 |
| LeRobot v2.1 ↔ v3 | `high` | 视频/action/state 保留，部分元数据重建 |
| LeRobot ↔ HDF5 | `partial` | 可能重编码视频/帧并推断 FPS |
| MCAP → LeRobot/HDF5 | `partial` | 解码选定流，可能丢弃无关 topic/标定信息 |
| LeRobot/HDF5 → MCAP | `partial` | 依据数据字段合成 topic 和时间戳 |

报告记录源/目标格式、Episode/Frame 数、字段映射、已知损失和警告。配置示例见 [`../../config/data/convert.example.json`](../../config/data/convert.example.json)。

## 9. 严格合并标准

合并至少需要两个不重复、非空、同格式数据集。预检会拒绝格式、FPS（容差 `1e-6`）、robot type、相机 keys、规范化 features、HDF5 dialect 或原生 schema 不一致。原生检查包括 LeRobot Parquet 列/类型，HDF5 group、dataset dtype 和非 Episode 轴形状，以及 MCAP topics、encoding 与 schema identity。合并保留原生 payload，只改写形成一致输出所必需的标识、任务引用和时间戳。

这项严格性是有意设计：需要 schema 或单位变换的数据，应先经过显式转换和标准化，再执行合并。

## 10. 视觉增强

亮度调整不需要额外模型；物体换色和背景替换需要独立 Python/CUDA 环境与 SAM3 Checkpoint：

```bash
export AUGMENT_PYTHON=/path/to/augment-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/datasets
```

Worker 核心依赖统一声明在 [`../../pyproject.toml`](../../pyproject.toml)；PyTorch 需按宿主机 CUDA 版本单独安装，SAM3 的安装方式见 [`../../third_party/README.md`](../../third_party/README.md)。增强保留未修改字段、时间戳、Episode 边界和原始数据类型，并生成处理清单。批量写出前应先验证单帧和视频预览。

## 11. 缓存清理与数据安全

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --cache
```

不要直接删除整个 `.embodit_cache/`：其中既有可再生成的预览，也可能有值得保留的报告。Embodit 使用独立输出、sidecar、写入前校验和原子完成；失败不会让不完整临时输出伪装成有效数据集。

## 12. 数据层二次开发

| 目标 | 目录 | 必需契约 |
|---|---|---|
| 新数据格式 | `backend/datasets/` | 检测、元信息、Episode/Frame、信号和媒体访问 |
| 新转换路径 | `backend/convert/` | 注册能力、字段映射和保真报告 |
| 新质检规则 | `backend/qc/detectors/` | 稳定 issue code、严重度、置信度、证据/区间和版本 |
| 新增强算法 | `backend/augment/` | 纯算法、预览、批处理写出和保真检查 |
| 新页面交互 | `web/` | 任务可取消、错误可见、写操作显式 |

相关测试包括 `tests/test_detect.py`、`test_convert_matrix.py`、`test_qc_core.py`、`test_augment_*` 和 `test_merge.py`。
