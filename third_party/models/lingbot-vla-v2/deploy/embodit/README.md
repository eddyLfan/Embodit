# LingBot VLA v2 · Astribot 部署说明

部署位置：`acting:/home/acting/Embodit/third_party/models/lingbot-vla-v2`。

源码适配、独立环境、六任务 Embodit 配置已准备好。**尚未加载完整 LingBot checkpoint，也没有执行机器人动作**；权重下载后还需完成一次真实加载和 DryRun 验证。

## 下载到哪里

从 vla-test 的训练目录中，选择明确的任务与训练步数：

```text
/media/vlm/vlm-model/asset_llm_ckpt/vla-representation/project/yifan/lingbot-vla-v2/
  output/astribot/<任务>/checkpoints/global_step_<步数>/hf_ckpt/
```

将该 **hf_ckpt 整个目录**下载到：

```text
/home/acting/Embodit/checkpoints/test/lingbot-vla-v2/<任务>/hf_ckpt/
```

保留全部 `model-*.safetensors`、`model.safetensors.index.json` 等原始内容。不要混合不同步数的分片。优化器 `.distcp`、`extra_state` 不需要。任务 YAML、归一化统计与 Qwen tokenizer 已放在部署仓库中，不需要另下 Qwen 基座权重或深度/视频教师模型。

六个支持的任务名：

- `arrange_blocks_letter_shape`
- `build_bridge_with_blocks`
- `find_fruit_under_towel`
- `pick_block_into_basket_fix`
- `pick_cup_rack_drawer3`
- `stand_stick`

这些资源对应 2026-09-05 核对的训练配置。若之后改变训练架构、状态定义或归一化统计，需要同步更新，不能仅换权重。

## 下载后使用

以 `find_fruit_under_towel` 为例，在 **acting** 上运行：

```bash
cd /home/acting/Embodit/third_party/models/lingbot-vla-v2
.venv/bin/python tools/verify_embodit.py \
  --task find_fruit_under_towel \
  --checkpoint /home/acting/Embodit/checkpoints/test/lingbot-vla-v2/find_fruit_under_towel/hf_ckpt \
  --report deploy/embodit/real-checkpoint-verification.json
```

此命令加载真实权重，用合成观测做一次推理，**不会连接机器人或发送动作**。请先在 Embodit 中正常停止占用 GPU 的其他模型服务；当前 OpenVLA 服务未被本次操作停止，24GB 的 3090 不应同时驻留两个大模型。

若权重按上述路径下载，现有配置无需修改。若放在其他目录，执行：

```bash
.venv/bin/python tools/prepare_embodit.py \
  --task find_fruit_under_towel \
  --checkpoint /实际下载路径/global_step_8000/hf_ckpt \
  --force
```

`--force` 只覆盖该任务生成的模型/配方文件；若已手工修改过配置，请先保存改动。

在 Embodit 导入/选择：

```text
config/local/lingbot-vla-v2-find-fruit-under-towel.recipe.json
```

其他五任务也有对应的 `.model.json` 和 `.recipe.json`，任务名中的下划线在文件名中换成连字符。优先使用完整 **recipe**，其中已绑定正确训练提示词；单独把 model 与原始机器人配置组合，会沿用机器人配置的旧提示词。

先运行真实相机输入的 **DryRun**，核对画面、状态、动作范围后，再由操作者开启 Live。配方默认 DryRun，模型输出 50 步，每轮执行前 10 步后重规划；沿用 30Hz 控制，双臂首步/相邻步限幅不超过 0.08rad，夹爪不超过 10 个原始单位。超限拒绝，不静默截断；这只是初测保护，不代表已完成真机安全或效果验证。

## 已完成的验证

- Python 3.12.12、Torch 2.8.0+cu128、Transformers 4.57.3、FlashAttention 2.8.3 独立环境；依赖一致性检查通过。
- 六任务真实训练预处理与动作反变换通过；最大往返误差约 `5.45e-6`。
- 三路 RGB 顺序、16 维状态、55 维内部表示、`50×16` 输出及左右臂/夹爪顺序已验证。
- 双臂 delta 反归一化后恢复绝对关节；夹爪保持绝对值；保留 float32 状态避免 bf16 舍入影响还原。
- RTX 3090 上 FlashAttention 与训练尺寸 MoE 模块前向通过；MoE 与独立 PyTorch 计算最大差约 `0.00390625`（bf16）。
- 已产出的 build_bridge/global_step_2000 checkpoint 的 **1,708 个张量键及形状**与完整空权重推理模型完全匹配。此项不等于完整权重推理。
- 分片加载器的小型测试模型精确还原、缺失/重复权重拒绝测试通过。
- 实际 Embodit runner 的入口解析、Base64 图像解码、动作返回协议已测试；该接口测试使用明确的策略替身，不冒充真实模型推理。
- 六套配方通过 Embodit schema 校验；缺权重时不会报告模型 ready。

证据：仓库内 `deploy/embodit/verification.json`；复测工具：`tools/verify_embodit.py`。

## 环境与变更记录

独立解释器：`.venv/bin/python`。复用现有 Python 3.12，依赖通过清华镜像安装，没有修改 OpenVLA/OpenPI 环境。推理采用官方 Transformers 加仓库内 Qwen patch，不需要训练数据加载器和教师模型依赖。源码保留当前 Embodit 直接管理的目录布局，没有擅自改成新的 Git 子模块。

核心锁定依赖：`requirements-inference.txt`；完整安装快照：`deploy/embodit/environment.lock.txt`；重建脚本：`tools/setup_embodit_env.sh`。FlashAttention 复用训练端 cp312/Torch2.8/CUDA12/cxx11abiTRUE 二进制，归档 SHA256：

```text
c54c393fb1a6b0d745c814af01feec4f30bae9cac0ed7ad25c8075a701c5a9ba
```

初始四个被修改的源码文件备份在 acting 的 `/tmp/embodit_lingbot_pre_adaptation.tgz`。所有改动保持未提交，未回滚原有工作。
