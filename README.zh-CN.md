<div align="center">
  <img src="images/Embodit_logo.png" alt="Embodit" width="180">
  <h1>Embodit</h1>
  <p>面向具身智能数据闭环与真机部署的一体化工具</p>
  <p><a href="README.md">English</a> · <strong>中文</strong></p>
</div>

## 我们为什么制作这个工具

具身智能项目常常被两类重复工作拖慢：一类是数据集浏览、质检、标注、转换、合并和增强；另一类是真机调试时反复登录模型服务器与机器人、启动服务、建立 SSH 隧道、检查 ROS、上电并运行推理客户端。

这些工作通常散落在临时脚本、终端历史和个人经验中，流程不稳定、难以复现，也很难迁移到下一台机器人。Embodit 希望把它们沉淀成一套可检查、可复用、默认安全的工作流：数据侧形成从发现问题到导出数据的闭环，部署侧形成从配置到 Dry Run、Live 和故障回滚的闭环。

## 工具简要介绍

Embodit 由两个边界清晰的功能层组成：

| 功能层 | 解决的问题 | 详细文档 |
|---|---|---|
| 数据层 | 多格式浏览、同步回放、自动质检、人工复核、标签、筛选、转换、合并、视觉增强与保真导出 | [数据层详细指南](docs/data/README.zh-CN.md) |
| 真机部署层 | 双机 SSH、模型加载、隧道、ROS Bringup、上电、初始位姿、推理客户端、Dry Run、Live、日志与回滚 | [真机部署层详细指南](docs/deployment/README.zh-CN.md) |

当前真机部署只保留最终版 Recipe v2。模型默认只需提供远端 Python `entrypoint` 和 Checkpoint，用户类实现 `load()`、`predict()`；内部 HTTP 服务、健康检查和进程托管由 Embodit 自动完成。

自动质检是确定性、配置带版本的规则系统：详细指南明确列出 hard-invalid 规则、视觉/运动默认阈值、评分公式、pass/review/quarantine 判定和人工覆盖关系。部署指南同样给出精确的 ROS readiness 与本体端动作安全校验，不用固定等待时间代替就绪判断。

## 工具简要页面展示

### 数据集概览与同步回放

![Embodit 数据集概览](images/overview.png)

### 标注与区间复核

![Embodit 标注工作台](images/annotation.png)

### 数据增强

![Embodit 数据增强](images/augmentation.png)

真机部署工作台位于页面右上角“真机部署”，用于编辑 Recipe、启动 Dry Run、观察组件状态、查看日志、切换 Live 和触发急停。

## Quick start

要求：Linux、Python 3.10+、[uv](https://docs.astral.sh/uv/)。

```bash
git clone https://github.com/eddyLfan/Embodit.git
cd Embodit

# 同步环境并启动；不传 DATA_ROOT 时浏览当前目录
bash embodit.sh start /path/to/datasets
```

终端会输出带临时 Token 的访问地址。常用命令：

```bash
bash embodit.sh status
bash embodit.sh logs -f
bash embodit.sh stop

# 真机部署配置校验与启动
bash embodit.sh recipe-validate config/deployment.recipe-v2.example.json
bash embodit.sh recipe-run config/deployment.recipe-v2.example.json --mode dry_run
```

局域网使用：

```bash
EMBODY_HOST=0.0.0.0 EMBODY_PUBLIC_HOST=<工作电脑IP> \
  bash embodit.sh start /path/to/datasets
```

开始处理数据前阅读[数据层详细指南](docs/data/README.zh-CN.md)；连接真实机器人前完整阅读[真机部署层详细指南](docs/deployment/README.zh-CN.md)。

## 更新日志

### 2026-08-04

- 真机部署层收敛到 Recipe v2，删除 Profile v1、旧 Gateway、Mock/SDK Policy 和旧 Session API。
- 新增 Embodit 托管的 Python Model Provider，普通用户不再实现 `/health`、`/infer`。
- 完成模型、SSH 隧道、ROS、初始位姿、Robot Client 的自动编排、监控与逆序回滚。
- 合并部署配置存储与 CLI，将远端运行时资产归入 `backend/deploy/assets/`。
- 重组项目文档为主 README、数据层指南和真机部署层指南。
- 以中英文补充质检/筛选、转换保真、合并兼容、部署 readiness 和动作安全的精确标准。

### 2026-08-03

- 增加数据集合并、自动质检、区间证据、复核筛选和增强预览闭环。
- 增加 LeRobot、RoboMimic HDF5、MCAP 的统一浏览与转换能力。

## 二次开发、共建说明

项目架构与模块职责见[开发架构说明](docs/architecture.md)。推荐扩展方式：

- 新数据格式：实现 `backend/datasets/` 中的统一数据集接口并注册。
- 新质检规则：在 `backend/qc/detectors/` 增加 Detector，保持输出 schema 稳定。
- 新增强算法：在 `backend/augment/` 增加纯算法与 worker 接入，保留预览和输出保真检查。
- 新模型：提供 `load(checkpoint)`、`predict(observations)`，优先使用 Python Provider。
- 新机器人 SDK：优先实现薄 ROS Bridge，向上暴露标准 topic/service/action，避免修改部署控制面。

提交修改前执行：

```bash
UV_CACHE_DIR=/tmp/embodit-uv-cache uv run pytest -q
python3 -m compileall -q backend
bash -n embodit.sh
git diff --check
```

请将功能修改、测试和文档放在同一变更中；涉及真实动作的功能必须保留 Dry Run、显式 Live 确认、限位、watchdog、hold/stop 和失败回滚。

## License

[MIT](LICENSE)
