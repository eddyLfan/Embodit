#!/usr/bin/env python3
"""No robot commands. Verify preprocessing, inverse actions, CUDA kernels and optional real weights."""
import argparse
import copy
import io
import json
import sys
import tempfile
import base64
import importlib.util
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import numpy as np
import torch
from PIL import Image
from transformers import AutoConfig
from lingbot_vla_embodit import (TASKS, OBSERVATION_MAP, LingBotVLAAdapter, decode_image,
                                task_assets, validate_checkpoint, validate_training_config)
from deploy.lingbot_vla_v2_policy import LingbotVLAv2Server
from lingbotvla.data.vla_data.utils import FeatureTransform
from lingbotvla.models import build_processor
from lingbotvla.models.vla.lingbot_vla.configuration_lingbot_vla import LingbotVLAV2Config


def observations():
    result = {"observation/state": np.linspace(-0.73, 0.91, 16, dtype=np.float32)}
    result["observation/state"][[7, 15]] = [24.3, 78.1]
    for i, key in enumerate(("observation/image", "observation/wrist_image", "observation/right_wrist_image")):
        pixels = np.empty((256, 320, 3), dtype=np.uint8)
        pixels[:] = ((220, 40, 30), (20, 210, 60), (40, 60, 230))[i]
        result[key] = {"data": pixels.tobytes(), "encoding": "rgb8", "width": 320, "height": 256}
    return result


def preprocess(task):
    cfg_path, norm, tokenizer = task_assets(task)
    cfg = validate_training_config(cfg_path, task)
    server = LingbotVLAv2Server.__new__(LingbotVLAv2Server)
    server.data_config = SimpleNamespace(**cfg["data"])
    server.config = LingbotVLAV2Config(**{**cfg["model"], **cfg["train"]})
    server.config.tokenizer_path = str(tokenizer)
    server.merge_qwen_config(AutoConfig.from_pretrained(tokenizer, local_files_only=True))
    server.processor = build_processor(str(tokenizer))
    transform = FeatureTransform(str(ROOT / "configs/robot_configs/astribot.yaml"),
                                 server.data_config, server.config, server.processor,
                                 chunk_size=50, norm_stats_path=str(norm))
    server.vla = SimpleNamespace(feature_transform=transform)
    server.use_bf16 = True
    server.action_key = transform.org_features["actions"]
    adapter = LingBotVLAAdapter()
    adapter.observation_map = OBSERVATION_MAP
    adapter.default_prompt = TASKS[task]
    raw = adapter.prepare_observation(observations())
    applied = server._prepare_model_input(raw)
    assert applied["state"].shape == (55,) and applied["state"].dtype == torch.float32
    assert applied["images"].shape[0] == 3 and applied["img_masks"].all()
    assert applied["image_grid_thw"].shape == (3, 3)
    assert int(applied["state_joint_mask"].sum()) == int(applied["action_joint_mask"].sum()) == 16
    # A non-symmetric synthetic target makes wrong arm/gripper ordering obvious.
    desired = np.tile(raw["state"], (50, 1)) + np.linspace(-0.04, 0.07, 50)[:, None]
    desired[:, 7], desired[:, 15] = np.linspace(30, 40, 50), np.linspace(68, 58, 50)
    train_item = copy.deepcopy(raw)
    server.resize_image(train_item)
    train_item = {k: torch.from_numpy(v) if isinstance(v, np.ndarray) else v for k, v in train_item.items()}
    train_item["actions"] = torch.as_tensor(desired, dtype=torch.float32)
    train_item["actions_is_pad"] = torch.zeros(50, dtype=torch.bool)
    encoded = transform.apply(train_item, policy_eval=False)
    assert encoded["actions"].shape == (50, 55)
    # Use inference's state/masks to decode training's known normalized action.
    restored = server._unapply_batched_actions([applied], encoded["actions"][None])["actions"][0]
    error = float(np.abs(restored - desired).max())
    np.testing.assert_allclose(restored, desired, atol=2e-5, rtol=0)
    # Invalid inputs cannot silently reach model inference.
    for state in ([0]*15, [float("nan")]*16):
        bad = observations(); bad["observation/state"] = state
        try: adapter.prepare_observation(bad)
        except ValueError: pass
        else: raise AssertionError("Invalid state accepted")
    bad = observations(); del bad["observation/wrist_image"]
    try: adapter.prepare_observation(bad)
    except ValueError: pass
    else: raise AssertionError("Missing camera accepted")
    rgb = np.array([[[11, 22, 33]]], dtype=np.uint8)
    bgr = {"data": rgb[..., ::-1].tobytes(), "encoding": "bgr8", "width": 1, "height": 1}
    np.testing.assert_array_equal(decode_image(bgr, "bgr"), rgb)
    stream = io.BytesIO(); Image.fromarray(rgb).save(stream, format="PNG")
    np.testing.assert_array_equal(decode_image({"data": stream.getvalue(), "encoding": "png"}, "png"), rgb)
    return {"task": task, "image_shape": list(applied["images"].shape), "grid": applied["image_grid_thw"].tolist(),
            "state_shape": [55], "action_shape": [50, 16], "roundtrip_max_error": error}


def check_schema(path):
    manifest = json.loads(Path(path).read_text())
    server = LingbotVLAv2Server.__new__(LingbotVLAv2Server)
    cfg, norm, tokenizer = task_assets("build_bridge_with_blocks")
    server.training_config_path, server.tokenizer_path = str(cfg), str(tokenizer)
    server.robot_norm_path, server.use_compile = str(norm), False
    captured = {}
    def capture(_path, strict=True):
        captured.update({k: list(v.shape) for k, v in server.vla.state_dict().items()})
    server.load_model_weights = capture
    with torch.device("meta"):
        server.load_vla("metadata-only-no-weights")
    expected = {k: v["shape"] for k, v in manifest["tensors"].items()}
    missing, extra = sorted(set(expected)-set(captured)), sorted(set(captured)-set(expected))
    mismatch = [k for k in set(expected)&set(captured) if expected[k] != captured[k]]
    if missing or extra or mismatch:
        raise AssertionError({"missing": missing[:10], "extra": extra[:10], "shape_mismatch": mismatch[:10]})
    return {"tensor_keys_and_shapes_matched": len(expected), "checkpoint_source": manifest["source"],
            "weights_loaded": False}


def check_sharded_loader():
    from safetensors.torch import save_file
    model = torch.nn.Sequential(torch.nn.Linear(3, 2), torch.nn.Linear(2, 1))
    original = {k: v.clone() for k, v in model.state_dict().items()}
    server = LingbotVLAv2Server.__new__(LingbotVLAv2Server)
    server.vla = model
    with tempfile.TemporaryDirectory(prefix="lingbot-loader-test-") as tmp:
        entries = list(original.items())
        save_file(dict(entries[:2]), str(Path(tmp)/"model-1.safetensors"))
        save_file(dict(entries[2:]), str(Path(tmp)/"model-2.safetensors"))
        with torch.no_grad():
            for p in model.parameters(): p.zero_()
        server.load_model_weights(tmp, strict=True)
        for key, value in model.state_dict().items():
            torch.testing.assert_close(value, original[key], atol=0, rtol=0)
        (Path(tmp)/"model-2.safetensors").unlink()
        try: server.load_model_weights(tmp, strict=True)
        except RuntimeError as error: assert "missing" in str(error)
        else: raise AssertionError("Incomplete shards accepted")
        save_file(dict(entries[:2]), str(Path(tmp)/"model-2.safetensors"))
        try: server.load_model_weights(tmp, strict=True)
        except RuntimeError as error: assert "duplicate" in str(error)
        else: raise AssertionError("Duplicate shard keys accepted")
    return {"exact_restore": True, "missing_rejected": True, "duplicate_rejected": True,
            "fixture": "small randomly initialized torch model, not deployed checkpoint"}


def check_embodit_transport():
    runner_path = ROOT.parents[2] / "backend/deploy/assets/model_runner.py"
    if not runner_path.is_file():
        return {"skipped": "not installed inside an Embodit checkout"}
    spec = importlib.util.spec_from_file_location("embodit_runner_test", runner_path)
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.resolve_entrypoint("lingbot_vla_embodit:LingBotVLAAdapter") is LingBotVLAAdapter
    encoded = observations()
    for key in ("observation/image", "observation/wrist_image", "observation/right_wrist_image"):
        encoded[key]["$binary"] = base64.b64encode(encoded[key].pop("data")).decode("ascii")
    adapter = LingBotVLAAdapter()
    adapter.observation_map, adapter.default_prompt = OBSERVATION_MAP, TASKS["build_bridge_with_blocks"]
    # Explicit fixture checks the real transport/adapter boundary, not model quality.
    adapter.policy = SimpleNamespace(infer=lambda obs, **kw: {"actions": np.tile(obs["state"], (50, 1))})
    provider = runner.ModelProvider({"predict_kwargs": {}})
    provider.model, provider.ready = adapter, True
    response = provider.predict(runner.decode_observation(encoded))
    values = np.asarray(response["action"]["values"])
    assert values.shape == (50, 16) and np.isfinite(values).all()
    checked = 0
    for task in TASKS:
        path = ROOT.parents[2] / "config/local" / ("lingbot-vla-v2-" + task.replace("_", "-") + ".model.json")
        config = json.loads(path.read_text())["model"]
        if not Path(config["checkpoint"]).exists():
            unloaded = runner.ModelProvider(config)
            try: unloaded.load()
            except FileNotFoundError: assert not unloaded.ready
            else: raise AssertionError("Model runner reported ready without weights")
            checked += 1
    return {"real_runner_entrypoint": True, "base64_image_decode": True, "action_values_shape": [50, 16],
            "missing_checkpoint_not_ready": checked, "model_forward": "explicit fixture, not real weights"}


def check_gpu():
    import flash_attn
    from flash_attn import flash_attn_func
    q = torch.randn(1, 64, 8, 64, device="cuda", dtype=torch.bfloat16)
    with torch.inference_mode():
        output = flash_attn_func(q, q, q, causal=True)
    assert output.shape == q.shape and torch.isfinite(output).all()
    from lingbotvla.models.vla.lingbot_vla.qwen2_action_expert import Qwen2TokenMoeBlock
    config = SimpleNamespace(hidden_size=768, num_experts=32, num_experts_per_tok=4,
                             norm_topk_prob=True, moe_intermediate_size=512,
                             shared_expert_intermediate_size=704, initializer_range=0.02,
                             hidden_act="silu", _moe_implementation="fused",
                             router_activation="sigmoid", routed_scaling_factor=4.0,
                             use_shared_expert_gate=False)
    moe = Qwen2TokenMoeBlock(config).to(device="cuda", dtype=torch.bfloat16).eval()
    states = torch.randn(1, 50, 768, device="cuda", dtype=torch.bfloat16)
    with torch.inference_mode():
        result, _router_logits = moe(states)
        assert result.shape == states.shape and torch.isfinite(result).all()
        # Independent PyTorch expert sum checks the fused CUDA kernel numerically.
        flat = states[0]
        scores = torch.nn.functional.linear(flat.float(), moe.gate.weight.float()).sigmoid()
        selected = (scores + moe.e_score_correction_bias).topk(4, dim=-1).indices
        weights = scores.gather(1, selected)
        weights = (weights / (weights.sum(-1, keepdim=True) + 1e-20) * 4).to(flat.dtype)
        reference = torch.zeros_like(flat, dtype=torch.float32)
        for expert in range(32):
            per_token = (weights.float() * (selected == expert)).sum(-1)
            gate = torch.nn.functional.linear(flat, moe.experts.gate_proj[expert])
            up = torch.nn.functional.linear(flat, moe.experts.up_proj[expert])
            value = torch.nn.functional.linear(torch.nn.functional.silu(gate)*up, moe.experts.down_proj[expert])
            reference += value.float() * per_token[:, None]
        reference = reference.to(flat.dtype) + moe.shared_expert(flat)
        error = float((result[0].float()-reference.float()).abs().max())
        torch.testing.assert_close(result[0], reference, atol=0.02, rtol=0.03)
    return {"gpu": torch.cuda.get_device_name(), "torch": torch.__version__,
            "cuda": torch.version.cuda, "flash_attn": flash_attn.__version__, "flash_forward": True,
            "moe_forward": True, "moe_vs_pytorch_max_error": error}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--gpu", action="store_true")
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--task", choices=list(TASKS), default="build_bridge_with_blocks")
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    report = {"preprocessing": [preprocess(task) for task in TASKS], "robot_actions_sent": False,
              "sharded_loader": check_sharded_loader(), "embodit_transport": check_embodit_transport()}
    if args.gpu: report["gpu"] = check_gpu()
    if args.schema: report["schema"] = check_schema(args.schema)
    if args.checkpoint:
        adapter = LingBotVLAAdapter()
        adapter.load(str(args.checkpoint), task=args.task)
        result = adapter.predict(observations())
        report["real_checkpoint"] = {"loaded": True, "finite": bool(np.isfinite(result).all()),
                                     "output_shape": list(result.shape), "input": "synthetic/no robot"}
    if args.report:
        args.report.parent.mkdir(parents=True, exist_ok=True)
        args.report.write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps(report, indent=2))


if __name__ == "__main__": main()
