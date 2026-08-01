"""Minimal SAM3 text-prompt video segmentation adapter.

SAM3 itself is not vendored.  Install it from Meta's repository and accept its
license/checkpoint terms as documented in ``third_party/README.md``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from settings import SAM_TRACK_CACHE_DIR

CACHE_VERSION = "embodit-sam3-v1"


def write_frames_to_jpg_dir(frames: np.ndarray, frame_dir: Path, quality: int = 90) -> Path:
    frame_dir.mkdir(parents=True, exist_ok=True)
    for index, image in enumerate(frames):
        Image.fromarray(image).save(frame_dir / f"{index:05d}.jpg", quality=quality)
    return frame_dir


class Sam3Segmenter:
    def __init__(self, checkpoint: Path, device_id: int = 0, cache_enabled: bool = True) -> None:
        try:
            import torch
            import sam3
            from sam3.model_builder import build_sam3_predictor
        except ImportError as error:
            raise RuntimeError(
                "SAM3 环境不可用；请按 third_party/README.md 安装 torch 与 facebookresearch/sam3"
            ) from error
        if not torch.cuda.is_available():
            raise RuntimeError("SAM3 颜色增强需要可用的 CUDA GPU")
        self.cache_enabled = cache_enabled
        self.cache_dir = SAM_TRACK_CACHE_DIR
        if cache_enabled:
            self.cache_dir.mkdir(parents=True, exist_ok=True)
        torch.cuda.set_device(device_id)
        self._autocast = torch.autocast(device_type="cuda", dtype=torch.bfloat16)
        self._autocast.__enter__()
        package_dir = Path(sam3.__file__).resolve().parent
        bpe_path = package_dir / "assets" / "bpe_simple_vocab_16e6.txt.gz"
        if not bpe_path.is_file():
            raise FileNotFoundError(f"SAM3 BPE 词表不存在：{bpe_path}")
        self.predictor = build_sam3_predictor(
            checkpoint_path=str(checkpoint),
            bpe_path=str(bpe_path),
            version="sam3",
            async_loading_frames=False,
        )

    def _cache_path(
        self,
        frames: np.ndarray,
        prompt: str,
        prompt_frame_index: int | None,
    ) -> Path:
        digest = hashlib.blake2b(digest_size=20)
        digest.update(CACHE_VERSION.encode())
        digest.update(prompt.encode())
        digest.update(str(prompt_frame_index).encode())
        digest.update(np.asarray(frames.shape, dtype=np.int64).tobytes())
        ids = np.linspace(0, len(frames) - 1, min(5, len(frames)), dtype=np.int64)
        for frame_id in ids:
            digest.update(np.ascontiguousarray(frames[frame_id]).tobytes())
        return self.cache_dir / f"{digest.hexdigest()}.npz"

    @staticmethod
    def _load_cache(path: Path) -> np.ndarray | None:
        try:
            with np.load(path, allow_pickle=False) as payload:
                shape = tuple(int(value) for value in payload["shape"])
                packed = payload["packed"]
            return np.unpackbits(packed, count=int(np.prod(shape))).reshape(shape).astype(bool)
        except Exception:
            return None

    @staticmethod
    def _save_cache(path: Path, masks: np.ndarray) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f".{path.name}.{os.getpid()}.tmp.npz")
        np.savez_compressed(
            temp,
            shape=np.asarray(masks.shape, dtype=np.int64),
            packed=np.packbits(masks.reshape(-1)),
        )
        temp.replace(path)

    @staticmethod
    def _masks_from_outputs(outputs: dict[str, Any]) -> list[np.ndarray]:
        masks = outputs.get("out_binary_masks")
        if masks is None:
            return []
        if hasattr(masks, "cpu"):
            masks = masks.cpu().numpy()
        masks = np.asarray(masks)
        if masks.size == 0:
            return []
        if masks.ndim == 4:
            masks = masks[:, 0]
        if masks.ndim == 2:
            return [masks.astype(bool)]
        return [masks[index].astype(bool) for index in range(masks.shape[0])]

    def _segment_in_dir(
        self,
        frame_dir: Path,
        frame_count: int,
        height_width: tuple[int, int],
        prompt: str,
        prompt_frame_index: int | None,
        score_threshold: float = 0.35,
    ) -> np.ndarray:
        height, width = height_width
        anchor = frame_count // 2 if prompt_frame_index is None else int(prompt_frame_index)
        response = self.predictor.handle_request(
            {"type": "start_session", "resource_path": str(frame_dir)}
        )
        session_id = response["session_id"]
        try:
            response = self.predictor.handle_request(
                {
                    "type": "add_prompt",
                    "session_id": session_id,
                    "frame_index": anchor,
                    "text": prompt,
                }
            )
            outputs = response.get("outputs", {})
            first_masks = self._masks_from_outputs(outputs)
            if not first_masks:
                return np.zeros((frame_count, height, width), dtype=bool)
            probabilities = outputs.get("out_probs")
            if probabilities is not None and hasattr(probabilities, "cpu"):
                probabilities = probabilities.cpu().numpy()
            keep = list(range(len(first_masks)))
            if probabilities is not None and len(probabilities) == len(first_masks):
                keep = [
                    index
                    for index, probability in enumerate(probabilities)
                    if float(probability) >= score_threshold
                ]
            if not keep:
                return np.zeros((frame_count, height, width), dtype=bool)
            union = np.zeros((frame_count, height, width), dtype=bool)
            for result in self.predictor.handle_stream_request(
                {"type": "propagate_in_video", "session_id": session_id}
            ):
                frame_index = result.get("frame_index")
                if frame_index is None or not 0 <= frame_index < frame_count:
                    continue
                masks = self._masks_from_outputs(result.get("outputs", {}))
                if len(masks) == len(first_masks):
                    for index in keep:
                        union[frame_index] |= masks[index]
                else:
                    for mask in masks:
                        union[frame_index] |= mask
            return union
        finally:
            self.predictor.handle_request({"type": "close_session", "session_id": session_id})

    def segment_single_prompt_video(
        self,
        frames: np.ndarray,
        prompt: str,
        prompt_frame_index: int | None = None,
        frame_dir: Path | None = None,
    ) -> np.ndarray:
        if not prompt.strip():
            raise ValueError("SAM3 prompt must not be empty")
        cache_path = self._cache_path(frames, prompt, prompt_frame_index)
        if self.cache_enabled and cache_path.exists():
            cached = self._load_cache(cache_path)
            if cached is not None and cached.shape == frames.shape[:3]:
                return cached
        own_dir = frame_dir is None
        working_dir = Path(tempfile.mkdtemp(prefix="sam3_frames_")) if own_dir else Path(frame_dir)
        try:
            if own_dir:
                write_frames_to_jpg_dir(frames, working_dir)
            masks = self._segment_in_dir(
                working_dir,
                len(frames),
                frames.shape[1:3],
                prompt,
                prompt_frame_index,
            )
            if self.cache_enabled:
                self._save_cache(cache_path, masks)
            return masks
        finally:
            if own_dir:
                shutil.rmtree(working_dir, ignore_errors=True)
