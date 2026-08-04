# Embodit 具身数据质检标准与落地基线

## 1. 结论

目前没有一个可直接套用、覆盖视频、机器人状态、动作、时序同步和任务成功的统一具身数据质检标准。生产系统应采用“通用数据质量标准 + 成熟媒体/容器工具 + 机器人领域规则 + 标注集校准”的组合方案。

Embodit 的自动质检只负责产出可复现证据和候选决策：结构性确定错误可以自动隔离；感知、轨迹和任务语义问题默认进入人工复核。任何不能从现有观测中区分的情况都不得伪装成高置信结论。例如，机器人和画面同时静止时，仅凭像素无法区分自然静止与相机冻结。

## 2. 可采用的标准和成熟手段

| 来源 | 可直接采用的部分 | 不应误用的部分 |
|---|---|---|
| [ISO/IEC 5259 系列](https://www.iso.org/publication/PUB200525.html) | 质量特性、可度量指标、质量管理、生命周期过程和治理职责 | 不提供机器人 jitter/frozen 的具体阈值 |
| [FFmpeg 视频滤镜](https://ffmpeg.org/ffmpeg-filters.html) | `freezedetect`、`blackdetect`、`blurdetect`、`signalstats` 可作为媒体基线或离线交叉验证 | `freezedetect` 仍是帧差阈值，无法独自区分自然静止 |
| [ITU-T P.1204](https://www.itu.int/rec/T-REC-P.1204) | 像素、码流和混合式视频质量评估的分层思想 | 面向流媒体主观质量，不是机器人数据准入标准 |
| [Netflix VMAF](https://github.com/Netflix/vmaf) | 有原始参考视频时评估转码、增强或导出保真度 | 无参考的真机采集不能直接用 VMAF 判“好坏” |
| [MCAP CLI](https://mcap.dev/guides/cli) | `mcap doctor/info` 检查容器结构、索引和统计；损坏文件可单独 `recover` | 容器可读不代表传感器内容或任务语义正确 |
| [LeRobot Dataset v3](https://github.com/huggingface/lerobot/blob/main/docs/source/lerobot-dataset-v3.mdx) | schema、FPS、episode 长度、全局统计、Parquet/视频元数据一致性 | 格式兼容不等于 demonstration 质量合格 |
| [RLDS](https://github.com/google-research/rlds) | episode/step 语义、`is_first/is_last/is_terminal` 和 `invalid` 生命周期语义 | 已归档的实现不宜成为新增强依赖，可借鉴数据模型 |
| [TensorFlow Data Validation](https://www.tensorflow.org/tfx/data_validation/get_started) | schema、统计异常、数据漂移和训练/部署偏移的方法 | 不必引入完整 TFX；可复用其“schema + baseline + drift”模式 |

推荐把 ISO/IEC 5259 映射到工程产物：每个 finding 都有质量特性、测量值、阈值、版本、置信度和证据区间；每次扫描保存完整配置、数据指纹、覆盖率和人工复核结果；规则变更必须用冻结的金标集回归。

## 3. 六层质检模型

| 层级 | 检查内容 | 默认处置 |
|---|---|---|
| L0 容器与结构 | 文件可读、索引、schema、dtype/shape、必需字段、episode 边界、解码帧数 | 确定错误自动 `quarantine` |
| L1 时序与同步 | 时间戳单调性、重复/回退、实际频率、丢帧、跨传感器偏移、音视频/动作同步 | 超出设备规范时隔离，否则复核 |
| L2 单模态信号 | NaN/Inf、饱和、卡死、黑屏、过曝、模糊、近重复帧、编码异常 | 确定错误隔离，感知问题复核 |
| L3 跨模态与物理 | action-state 跟随、命令运动但视频不变、运动学/关节/速度限制、相机全局运动 | 规则产出证据，默认复核 |
| L4 任务语义 | 任务是否成功、步骤顺序、抓取/放置结果、人工干预、恢复动作 | 人工或经校准模型确认 |
| L5 数据集统计 | 重复 episode、任务/场景/操作者覆盖、长尾、漂移、train/eval 泄漏 | 数据集级告警和采样治理 |

只有 L0 和一部分有明确设备规范的 L1/L2 适合做无条件硬门禁。L3/L4 的阈值依赖机器人本体、控制模式、任务和采样率，必须使用 profile 分开标定。

## 4. jitter 与 frozen 的标准化定义

### 4.1 Motion jitter

`motion/jitter` 指短时间内非预期的高频往返，不等同于“大加速度”或“一次快速动作”。默认判定同时要求：

1. 同一非夹爪维度的加速度和 jerk 均为 robust-Z 异常；
2. 变化相对该维度 episode 量程足够大，排除浮点噪声和量化噪声；
3. 0.4 秒窗口内存在方向反转；
4. 实际路径长度显著大于窗口首尾净位移，排除单向 setpoint 阶跃；
5. 异常持续时间达到配置下限。

对有物理单位和设备限制的数据，应在此基础上增加关节速度、加速度、jerk 和笛卡尔速度的本体上限。归一化 action 不能直接共用弧度或米制阈值。

### 4.2 Camera shake

`visual/camera_shake` 指静态安装相机的高频全局运动，不等同于前景物体运动或平滑相机移动。默认使用：

1. KLT 稀疏特征跟踪；
2. RANSAC 估计相邻帧全局相似变换；
3. 最少跟踪内点、内点率和画面网格覆盖率共同过滤大前景运动；
4. 对连续全局变换的高频变化做阈值和持续时间判定。

静态相机列表应按采集配置显式填写。仅根据名称识别 `front/head/wrist` 是兼容性回退，不是生产标准。

### 4.3 Frozen video

`visual/frozen` 指连续近重复画面；`integrity/video_frozen` 还要求足以证明采集不可用的跨模态证据。默认使用：

1. 相邻采样帧平均绝对差；
2. 发生变化的像素占比，排除低幅传感器噪声造成的误判；
3. 连续时长；
4. action/state 在同一区间内是否仍有运动；
5. 冻结后的帧差、正常背景帧差和冻结占比作为审计指标。

当 action/state 存在但同一区间也静止时，候选不会被命名为 frozen；当运动信号缺失时，可以给出低置信复核项，但不能升级为 hard-invalid。这是可观测性限制，不应通过调低阈值掩盖。

## 5. 金标集和准入指标

每个机器人 × 控制模式 × 相机安装方案应维护独立的 QC profile 和冻结金标集。初始建议抽取 200–500 个 episode，由至少两名复核者标注问题类型和时间区间；每类关键缺陷至少覆盖 30 个正例，并保留足量容易混淆的反例，例如自然静止、单向阶跃、平滑反向、前景快速运动、弱纹理和曝光变化。

上线门槛应按用途分开：

- 自动 `quarantine` 以精确率优先，目标应接近零误杀；建议在金标集上 precision ≥99%，且所有误报逐例签字确认；
- `review` 候选以召回率优先，建议关键问题 recall ≥95%；
- 时间区间同时报告 event precision/recall 和 interval IoU，不能只看 episode 是否命中；
- 按任务、操作者、相机、日期和机器人切片报告，禁止只看总体平均；
- 阈值或算法升级先 shadow scan，再比较新旧 finding 的迁移矩阵和人工复核差异。

这些数值是 Embodit 的建议工程目标，不是 ISO 或 ITU 规定值。数据不足时应降低自动处置权限，而不是用小样本给出高置信门槛。

## 6. 推荐落地顺序

### P0：当前门禁

- 保留完整解码、必需字段、NaN/Inf、长度和相机缺失等硬规则；
- jitter 增加反转和超额行程证据；
- frozen 增加变化像素占比与 action/state 交叉验证；
- camera shake 改用 KLT + RANSAC，并提高采样率；
- 报告完整指标，所有非确定问题支持区间复核。

### P1：设备和跨模态规则

- 从机器人 URDF/设备配置导入关节、速度、加速度和控制频率限制；
- 增加时间戳单调性、实际采样率、丢帧、相机—state/action 延迟估计；
- 对 MCAP 前置运行结构检查，对 MP4 用 FFmpeg 结果做离线交叉验证；
- 建立 robot/camera/task profile，不再共享一套全局阈值。

### P2：数据集和语义质量

- 增加近重复 episode、分布漂移、覆盖度和 train/eval 泄漏检测；
- 使用任务成功传感器、规则状态机或视觉模型生成 L4 候选；
- 模型/VLM 只生成证据和候选，经过独立验证和置信校准后才能参与自动处置；
- 用人工复核结果持续计算各 detector 的 precision、recall、校准误差和切片表现。

## 7. 当前实现的版本策略

QC 配置版本为 4，motion/video detector 版本为 3。版本变化会进入报告和配置哈希，避免复用旧算法缓存。默认值是没有金标集时的保守起点，不是跨机器人的固定物理标准。
