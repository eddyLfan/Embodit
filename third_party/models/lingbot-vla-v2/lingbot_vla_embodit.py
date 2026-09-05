"""Astribot Embodit bridge; original training preprocessing and inverse mapping."""
from __future__ import annotations

import io
import json
from pathlib import Path
import numpy as np
from PIL import Image
import yaml

ROOT = Path(__file__).resolve().parent
TASKS = {
    "arrange_blocks_letter_shape": "arrange the red, green, and blue long flat blocks into an L shape in that order",
    "build_bridge_with_blocks": "stand two blue long flat blocks upright with a gap and place a green long flat block on top to form a bridge",
    "find_fruit_under_towel": "move the gray towel aside, find the apple underneath, and put the apple into the plate on the right",
    "pick_block_into_basket_fix": "pick up the block in the middle of the table and put it into the plate on the right",
    "pick_cup_rack_drawer3": "open the middle green drawer, take the purple cup from the higher left fork of the rack, put it into the drawer, and close the drawer",
    "stand_stick": "insert the blue stick into the center hole of the 7x7 pegboard on the right",
}
OBSERVATION_MAP = {
    "image": "observation/image",
    "wrist_image": "observation/wrist_image",
    "right_wrist_image": "observation/right_wrist_image",
    "state": "observation/state",
}


def decode_image(value, name):
    if isinstance(value, Image.Image):
        return np.asarray(value.convert("RGB"), dtype=np.uint8)
    if isinstance(value, dict):
        payload = value.get("data")
        if not isinstance(payload, (bytes, bytearray, memoryview)):
            raise ValueError(f"{name}: data must be decoded binary bytes")
        enc = str(value.get("encoding", "")).lower()
        channels = {"rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4, "mono8": 1, "8uc1": 1}.get(enc)
        if channels:
            w, h = value.get("width"), value.get("height")
            if not isinstance(w, int) or not isinstance(h, int) or min(w, h) <= 0:
                raise ValueError(f"{name}: invalid raw image dimensions")
            if len(payload) != w*h*channels:
                raise ValueError(f"{name}: raw image byte count mismatch")
            value = np.frombuffer(payload, np.uint8).reshape(h, w, channels)
            if enc in ("bgr8", "bgra8"):
                value = value[..., [2, 1, 0]]
        else:
            value = np.asarray(Image.open(io.BytesIO(payload)).convert("RGB"))
    value = np.asarray(value)
    if value.ndim != 3 or value.shape[-1] not in (1, 3, 4) or min(value.shape[:2]) <= 0:
        raise ValueError(f"{name}: expected HWC image, got {value.shape}")
    if not np.isfinite(value).all():
        raise ValueError(f"{name}: non-finite pixels")
    if value.dtype != np.uint8:
        if value.min() >= 0 and value.max() <= 1:
            value = value * 255
        if value.min() < 0 or value.max() > 255:
            raise ValueError(f"{name}: pixels outside [0,255]")
        value = value.astype(np.uint8)
    value = np.repeat(value, 3, axis=-1) if value.shape[-1] == 1 else value[..., :3]
    # frombuffer(bytes) and PIL arrays can be read-only; Torch expects writable memory.
    return np.array(value, dtype=np.uint8, order="C", copy=True)


def task_assets(task):
    if task not in TASKS:
        raise ValueError(f"Unknown Astribot task: {task}; choose from {list(TASKS)}")
    return (ROOT / "deploy/embodit/tasks" / task / "lingbotvla_cli.yaml",
            ROOT / "assets/norm_stats" / f"astribot_{task}.json",
            ROOT / "weights/Qwen3-VL-4B-Instruct")


def validate_training_config(path, task):
    cfg = yaml.safe_load(Path(path).read_text())
    data, train = cfg["data"], cfg["train"]
    if Path(data["train_path"]).name != task or data["data_name"] != "astribot":
        raise ValueError("Training config task/robot mismatch")
    if data["cameras"] != ["camera_top", "camera_wrist_left", "camera_wrist_right"]:
        raise ValueError("Training camera order mismatch")
    if (train["chunk_size"], train["max_action_dim"], train["max_state_dim"]) != (50, 55, 55):
        raise ValueError("Expected training dimensions: chunk=50, action/state=55")
    return cfg


def validate_checkpoint(path):
    path = Path(path).expanduser().resolve()
    if (path / "hf_ckpt").is_dir():
        path = path / "hf_ckpt"
    if not path.is_dir():
        raise FileNotFoundError(f"Download the selected checkpoint's hf_ckpt directory first: {path}")
    index = path / "model.safetensors.index.json"
    if index.is_file():
        names = set(json.loads(index.read_text())["weight_map"].values())
        if not names or any(Path(n).name != n for n in names):
            raise ValueError("Invalid checkpoint shard index")
        missing = [n for n in names if not (path / n).is_file()]
        if missing:
            raise FileNotFoundError(f"Incomplete checkpoint shards: {missing}")
    elif not (path / "model.safetensors").is_file():
        raise FileNotFoundError("Expected model.safetensors or model.safetensors.index.json; do not use optimizer .distcp files")
    return path


class LingBotVLAAdapter:
    def load(self, checkpoint, *, task, training_config_path=None, norm_stats_path=None,
             tokenizer_path=None, default_prompt=None, observation_map=None, use_compile=False,
             action_horizon=50, **kwargs):
        if kwargs:
            raise ValueError(f"Unknown LingBot load settings: {sorted(kwargs)}")
        if action_horizon != 50:
            raise ValueError("This Astribot training run uses 50-action chunks")
        config, norm, tokenizer = task_assets(task)
        config = Path(training_config_path or config).resolve()
        norm = Path(norm_stats_path or norm).resolve()
        tokenizer = Path(tokenizer_path or tokenizer).resolve()
        validate_training_config(config, task)
        if not norm.is_file() or not (tokenizer / "tokenizer.json").is_file():
            raise FileNotFoundError("Missing task normalization or local Qwen3-VL tokenizer assets")
        path = validate_checkpoint(checkpoint)
        self.task = task
        self.default_prompt = default_prompt or TASKS[task]
        self.observation_map = dict(observation_map or OBSERVATION_MAP)
        if set(self.observation_map) != set(OBSERVATION_MAP):
            raise ValueError("observation_map must specify image, wrist_image, right_wrist_image, state")
        import torch
        if not torch.cuda.is_available():
            raise RuntimeError("LingBot 6B inference requires CUDA; no CUDA GPU is available")
        from deploy.lingbot_vla_v2_policy import LingbotVLAv2Server
        self.policy = LingbotVLAv2Server(
            path_to_pi_model=str(path), robot_norm_path=str(norm),
            training_config_path=str(config), tokenizer_path=str(tokenizer),
            robot_config_path=str(ROOT / "configs/robot_configs/astribot.yaml"),
            use_bf16=True, use_fp32=False, use_compile=use_compile,
            chunk_ret=True, use_length=50,
        )
        self.policy.reset("astribot")
        self.specification = {"action_horizon": 50, "action_dim": 16, "state_dim": 16,
                              "task": task, "action_semantics": "absolute_joint_positions_and_grippers"}

    def prepare_observation(self, observations):
        missing = [v for v in self.observation_map.values() if v not in observations]
        if missing:
            raise ValueError(f"Missing observations: {missing}")
        state = np.asarray(observations[self.observation_map["state"]], dtype=np.float32)
        if state.shape != (16,) or not np.isfinite(state).all():
            raise ValueError("Astribot state must contain 16 finite values: left7, left_gripper, right7, right_gripper")
        prompt = observations.get("prompt", self.default_prompt)
        if not isinstance(prompt, str) or not prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        return {"state": state, "task": prompt.strip(), **{
            key: decode_image(observations[self.observation_map[key]], key)
            for key in ("image", "wrist_image", "right_wrist_image")}}

    def predict(self, observations, **kwargs):
        if kwargs:
            raise ValueError(f"Unknown predict settings: {sorted(kwargs)}")
        import torch
        obs = self.prepare_observation(observations)
        with torch.inference_mode():
            result = self.policy.infer(obs, center_crop=False)
        # FeatureTransform already restores arm deltas AND interleaves grippers.
        # Do not add state a second time or rearrange this vector again.
        actions = np.asarray(result["actions"], dtype=np.float32)
        if actions.shape != (50, 16) or not np.isfinite(actions).all():
            raise ValueError(f"Invalid model actions: {actions.shape}; expected finite 50x16")
        return actions
