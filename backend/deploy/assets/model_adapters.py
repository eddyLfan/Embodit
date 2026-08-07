"""Checkpoint-only adapters for pinned third-party VLA repositories.

This module contains only Embodit integration code. No third-party source is
copied here. The referenced upstream revisions and licenses are documented in
``third_party/README.md`` and locked by the parent repository's gitlinks.
"""

from __future__ import annotations

import dataclasses
import io
import sys
from pathlib import Path
from typing import Any


def _add_source_path(source_path: str | None) -> None:
    if source_path:
        path = str(Path(source_path).expanduser())
        if path not in sys.path:
            sys.path.insert(0, path)


def _is_number_list(value: list[Any]) -> bool:
    return bool(value) and all(isinstance(item, (int, float, bool)) for item in value)


def _decode_image(value: dict[str, Any]) -> Any:
    data = value.get("data")
    if not isinstance(data, (bytes, bytearray)):
        return None
    payload = bytes(data)
    width = value.get("width")
    height = value.get("height")
    encoding = str(value.get("encoding") or "").lower()
    if width and height and encoding:
        import numpy as np

        channels = {"mono8": 1, "8uc1": 1, "rgb8": 3, "bgr8": 3, "rgba8": 4, "bgra8": 4}.get(encoding)
        if channels:
            image = np.frombuffer(payload, dtype=np.uint8).reshape(int(height), int(width), channels)
            if channels == 1:
                return image[..., 0]
            if encoding in {"bgr8", "bgra8"}:
                image = image[..., [2, 1, 0, *([3] if channels == 4 else [])]]
            return image
    try:
        from PIL import Image

        return Image.open(io.BytesIO(payload)).convert("RGB")
    except Exception as error:  # noqa: BLE001
        raise ValueError("无法解码模型图像观测；请提供 ROS Image/CompressedImage 的有效 data") from error


def native_observation(value: Any) -> Any:
    """Convert Embodit's transport-safe values to common policy-native values."""

    if isinstance(value, dict):
        decoded = _decode_image(value)
        if decoded is not None:
            return decoded
        return {key: native_observation(item) for key, item in value.items()}
    if isinstance(value, list):
        if _is_number_list(value):
            import numpy as np

            return np.asarray(value, dtype=np.float32)
        return [native_observation(item) for item in value]
    return value


def _mapped_observations(observations: dict[str, Any], mapping: dict[str, str] | None) -> dict[str, Any]:
    converted = {key: native_observation(value) for key, value in observations.items()}
    if not mapping:
        return converted
    missing = [source for source in mapping.values() if source not in converted]
    if missing:
        raise ValueError("observation_map 引用了缺失观测：" + ", ".join(sorted(missing)))
    return {target: converted[source] for target, source in mapping.items()}


def _auto_map_lerobot_features(
    values: dict[str, Any],
    expected: set[str],
    default_prompt: str | None,
) -> dict[str, Any]:
    """Fill standard LeRobot feature names from ordinary robot observation names."""

    result = dict(values)
    image_keys = [key for key in values if "image" in key.lower() or "camera" in key.lower()]
    state_keys = [
        key for key in values
        if any(token in key.lower() for token in ("state", "joint", "proprio"))
        and key not in image_keys
    ]
    text_keys = [key for key, value in values.items() if isinstance(value, str)]
    used_images: set[str] = set()
    for target in sorted(expected - set(result)):
        lowered = target.lower()
        if "image" in lowered or "camera" in lowered:
            tokens = {part for part in lowered.replace(".", "_").split("_") if len(part) > 2}
            ranked = sorted(
                (key for key in image_keys if key not in used_images),
                key=lambda key: (-sum(token in key.lower() for token in tokens), image_keys.index(key)),
            )
            if ranked:
                result[target] = values[ranked[0]]
                used_images.add(ranked[0])
        elif "state" in lowered or "proprio" in lowered:
            if state_keys:
                result[target] = values[state_keys[0]]
        elif any(token in lowered for token in ("prompt", "task", "language", "lang")):
            if text_keys:
                result[target] = values[text_keys[0]]
            elif default_prompt is not None:
                result[target] = default_prompt
    return result


class OpenPIAdapter:
    """Physical Intelligence OpenPI adapter at the pinned submodule API."""

    specification: dict[str, Any]

    def load(
        self,
        checkpoint: str,
        *,
        source_path: str | None = None,
        config_name: str | None = None,
        action_horizon: int | None = None,
        default_prompt: str | None = None,
        device: str | None = None,
        observation_map: dict[str, str] | None = None,
        **policy_kwargs: Any,
    ) -> None:
        _add_source_path(source_path)
        from openpi.policies import policy_config
        from openpi.training import config as training_config

        resolved_name = config_name or self._infer_config_name(checkpoint, training_config)
        config = training_config.get_config(resolved_name)
        if action_horizon is not None:
            if isinstance(action_horizon, bool) or not isinstance(action_horizon, int) or action_horizon <= 0:
                raise ValueError("action_horizon 必须是正整数")
            config = dataclasses.replace(
                config,
                model=dataclasses.replace(config.model, action_horizon=action_horizon),
            )
        self.policy = policy_config.create_trained_policy(
            config,
            checkpoint,
            default_prompt=default_prompt,
            pytorch_device=device,
            sample_kwargs=policy_kwargs or None,
        )
        self.observation_map = observation_map
        self.specification = {
            "family": "openpi",
            "config_name": resolved_name,
            "action_horizon": action_horizon or getattr(config.model, "action_horizon", None),
            "checkpoint": checkpoint,
            **(getattr(self.policy, "metadata", None) or {}),
        }

    @staticmethod
    def _infer_config_name(checkpoint: str, training_config: Any) -> str:
        normalized = checkpoint.rstrip("/")
        candidates: list[str] = []
        aliases = {
            "pi0_fast_droid": "pi0_fast_droid",
            "pi0_droid": "pi0_droid",
            "pi0_aloha_towel": "pi0_aloha_towel",
            "pi0_aloha_tupperware": "pi0_aloha_tupperware",
            "pi0_aloha_pen_uncap": "pi0_aloha_pen_uncap",
            "pi05_libero": "pi05_libero",
            "pi05_droid": "pi05_droid",
        }
        for part in reversed(Path(normalized).parts[-4:]):
            candidate = aliases.get(part, part)
            if candidate and candidate not in candidates:
                candidates.append(candidate)
        for candidate in candidates:
            try:
                training_config.get_config(candidate)
                return candidate
            except Exception:  # noqa: BLE001
                continue
        raise ValueError(
            "OpenPI checkpoint 无法自动推断训练 config；官方 checkpoint 通常可由目录名推断。"
            "如为自训练 checkpoint，请在 load_kwargs.config_name 中填写 OpenPI config 名称。"
        )

    def predict(self, observations: dict[str, Any], **_: Any) -> Any:
        values = _mapped_observations(observations, self.observation_map)
        if self.observation_map is None:
            images = [value for key, value in values.items() if "image" in key.lower() or "camera" in key.lower()]
            states = [value for key, value in values.items() if "state" in key.lower() or "joint" in key.lower()]
            if images:
                values.setdefault("observation/image", images[0])
                values.setdefault("image", images[0])
            if states:
                values.setdefault("observation/state", states[0])
                values.setdefault("state", states[0])
        result = self.policy.infer(values)
        if not isinstance(result, dict) or "actions" not in result:
            raise ValueError("OpenPI policy.infer 必须返回包含 actions 的对象")
        return result["actions"]


class LeRobotAdapter:
    """Hugging Face LeRobot adapter using checkpoint-embedded config/processors."""

    specification: dict[str, Any]

    def load(
        self,
        checkpoint: str,
        *,
        source_path: str | None = None,
        device: str | None = None,
        revision: str | None = None,
        default_prompt: str | None = None,
        observation_map: dict[str, str] | None = None,
        **_: Any,
    ) -> None:
        _add_source_path(source_path)
        from lerobot.configs import PreTrainedConfig
        from lerobot.policies.factory import get_policy_class, make_pre_post_processors

        config = PreTrainedConfig.from_pretrained(checkpoint, revision=revision)
        if device:
            config.device = device
        config.pretrained_path = Path(checkpoint)
        config.pretrained_revision = revision
        policy_class = get_policy_class(config.type)
        self.policy = policy_class.from_pretrained(checkpoint, config=config, revision=revision)
        self.preprocessor, self.postprocessor = make_pre_post_processors(
            policy_cfg=config,
            pretrained_path=checkpoint,
            pretrained_revision=revision,
            preprocessor_overrides={"device_processor": {"device": str(config.device)}},
        )
        self.policy.eval()
        self.policy.reset()
        self.observation_map = observation_map
        self.default_prompt = default_prompt
        self.specification = {
            "family": "lerobot",
            "policy_type": config.type,
            "checkpoint": checkpoint,
            "input_features": sorted((config.input_features or {}).keys()),
            "output_features": sorted((config.output_features or {}).keys()),
        }

    @staticmethod
    def _batch(observations: dict[str, Any]) -> dict[str, Any]:
        import numpy as np
        import torch

        batch: dict[str, Any] = {}
        for key, value in observations.items():
            if isinstance(value, str):
                batch[key] = [value]
                continue
            if hasattr(value, "convert") and callable(value.convert):
                value = np.asarray(value.convert("RGB"))
            if isinstance(value, np.ndarray):
                tensor = torch.from_numpy(np.array(value, copy=True))
                if tensor.ndim == 3 and tensor.shape[-1] in {1, 3, 4}:
                    tensor = tensor[..., :3].permute(2, 0, 1).to(torch.float32) / 255.0
                elif not tensor.dtype.is_floating_point:
                    tensor = tensor.to(torch.float32)
                batch[key] = tensor.unsqueeze(0)
            elif isinstance(value, (int, float, bool)):
                batch[key] = torch.tensor([[value]], dtype=torch.float32)
            else:
                batch[key] = value
        return batch

    def predict(self, observations: dict[str, Any], **_: Any) -> Any:
        import torch

        values = _mapped_observations(observations, self.observation_map)
        expected = set(self.specification["input_features"])
        if self.observation_map is None:
            values = _auto_map_lerobot_features(values, expected, self.default_prompt)
        missing = sorted(expected - set(values))
        if missing:
            raise ValueError("LeRobot checkpoint 缺少所需观测：" + ", ".join(missing))
        batch = self.preprocessor(self._batch(values))
        with torch.inference_mode():
            action = self.policy.predict_action_chunk(batch)
            action = self.postprocessor(action)
        if isinstance(action, dict):
            action = action.get("action", action)
        return action[0] if getattr(action, "ndim", 0) >= 3 else action


class StarVLAAdapter:
    """StarVLA adapter built on its official checkpoint-only PolicyServerWrapper."""

    specification: dict[str, Any]

    def load(
        self,
        checkpoint: str,
        *,
        source_path: str | None = None,
        device: str = "cuda",
        use_bf16: bool = False,
        unnorm_key: str | None = None,
        config_overrides: list[str] | None = None,
        default_prompt: str | None = None,
        observation_map: dict[str, str] | None = None,
        **predict_kwargs: Any,
    ) -> None:
        _add_source_path(source_path)
        from deployment.model_server.policy_wrapper import PolicyServerWrapper

        self.policy = PolicyServerWrapper(
            checkpoint,
            device=device,
            use_bf16=use_bf16,
            unnorm_key=unnorm_key,
            config_overrides=config_overrides,
        )
        self.unnorm_key = unnorm_key
        self.default_prompt = default_prompt
        self.observation_map = observation_map
        self.predict_kwargs = predict_kwargs
        self.specification = {
            "family": "starvla",
            "checkpoint": checkpoint,
            **(getattr(self.policy, "metadata", None) or {}),
        }

    def predict(self, observations: dict[str, Any], **kwargs: Any) -> Any:
        values = _mapped_observations(observations, self.observation_map)
        if "image" not in values:
            image_values = [value for key, value in values.items() if "image" in key or "camera" in key]
            if image_values:
                values["image"] = image_values
        if "lang" not in values:
            values["lang"] = values.get("prompt") or values.get("task") or self.default_prompt or ""
        if "state" not in values:
            state = next((value for key, value in values.items() if "state" in key or "joint" in key), None)
            if state is not None:
                values["state"] = state
        result = self.policy.predict_action(
            [values],
            unnorm_key=self.unnorm_key,
            **self.predict_kwargs,
            **kwargs,
        )
        actions = result.get("actions") if isinstance(result, dict) else None
        if actions is None:
            raise ValueError("StarVLA PolicyServerWrapper 必须返回 actions")
        return actions[0]
