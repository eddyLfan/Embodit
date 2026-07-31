"""Tests for the built-in augmentation algorithms and preview contract."""

from __future__ import annotations

import numpy as np
import pytest

from augment.algorithms import apply_brightness_videos, apply_color_videos
from augment.capabilities import config_fingerprint
from augment.effects import recolor_frames, replace_background_frames


def _video(value: int = 80) -> np.ndarray:
    return np.full((4, 32, 32, 3), value, dtype=np.uint8)


def _center_mask() -> np.ndarray:
    mask = np.zeros((4, 32, 32), dtype=bool)
    mask[:, 8:24, 8:24] = True
    return mask


def test_brightness_is_built_in_and_preserves_shape():
    source = _video()
    result, meta = apply_brightness_videos(
        {"cam": source}, mode="manual", gain=1.2, gamma=0.9
    )
    assert result["cam"].shape == source.shape
    assert result["cam"].dtype == np.uint8
    assert meta["mode"] == "manual"
    assert not np.array_equal(result["cam"], source)


def test_mask_effects_change_only_the_intended_region():
    source = _video()
    mask = _center_mask()
    recolored, recolor_meta = recolor_frames(source, mask, [220, 40, 40])
    assert recolor_meta["inside_mean_abs_change"] >= 8
    assert recolor_meta["outside_mean_abs_change"] <= 1
    assert np.array_equal(recolored[:, :4, :4], source[:, :4, :4])

    replaced, replace_meta = replace_background_frames(source, mask, [245, 245, 245])
    assert replace_meta["keep_mean_abs_change"] <= 1.5
    assert np.all(replaced[:, :4, :4] == 245)


class _FakeSegmenter:
    def segment_single_prompt_video(self, frames, _prompt, frame_dir=None):
        del frame_dir
        return _center_mask() if int(frames[0, 0, 0, 0]) else np.zeros(frames.shape[:3], dtype=bool)


def test_color_preview_respects_strict_camera_policy():
    videos = {"ok": _video(), "failed": _video(0)}
    with pytest.raises(RuntimeError, match="strict"):
        apply_color_videos(
            videos,
            prompts=["object"],
            apply_mode="object_recolor",
            color_rgb=[220, 40, 40],
            segmenter=_FakeSegmenter(),
            camera_policy="strict",
        )

    result, _, meta = apply_color_videos(
        videos,
        prompts=["object"],
        apply_mode="object_recolor",
        color_rgb=[220, 40, 40],
        segmenter=_FakeSegmenter(),
        camera_policy="partial",
    )
    assert set(result) == {"ok"}
    assert len(meta["failures"]) == 1


def test_config_fingerprint_binds_transform_but_not_batch_scope():
    base = {
        "dataset": "/data/source",
        "augType": "brightness",
        "brightnessMode": "manual",
        "brightnessGain": 1.2,
        "brightnessGamma": 0.9,
        "targetFormat": "lerobot_v3",
        "cameraPolicy": "strict",
    }
    preview = {**base, "mode": "preview", "previewEpisode": 3}
    batch = {**base, "mode": "batch", "output": "/data/out", "episodes": [1, 2]}
    assert config_fingerprint(preview) == config_fingerprint(batch)
    assert config_fingerprint(base) != config_fingerprint({**base, "brightnessGain": 1.3})
