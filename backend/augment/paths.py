"""Paths and constants for Embodit augment."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]  # repo root
BACKEND_ROOT = Path(__file__).resolve().parents[1]


def _resolve_augment_algo_root() -> Path:
    """Locate the external augment algorithm package (brightness / SAM3 wrappers).

    Resolution order:
    1. ``EMBODIT_AUGMENT_ROOT`` / ``AUGMENT_ALGO_ROOT``
    2. ``<repo>/third_party/data_strengthen`` (vendored layout for OSS)
    3. Legacy sibling ``../../data_strengthen`` (internal monorepo layout)
    """
    for key in ("EMBODIT_AUGMENT_ROOT", "AUGMENT_ALGO_ROOT"):
        raw = os.environ.get(key, "").strip()
        if raw:
            return Path(raw).expanduser().resolve()

    vendored = PROJECT_ROOT / "third_party" / "data_strengthen"
    if (vendored / "augment").is_dir():
        return vendored.resolve()

    legacy = PROJECT_ROOT.parent.parent / "data_strengthen"
    if (legacy / "augment").is_dir():
        return legacy.resolve()

    return vendored.resolve()


DATA_STRENGTHEN_ROOT = _resolve_augment_algo_root()
from settings import SAM3_CHECKPOINT  # noqa: E402,F401  (central config)

DEFAULT_JOBS_DIR = PROJECT_ROOT / ".augment_jobs"
DEFAULT_PREVIEW_DIR = PROJECT_ROOT / ".augment_previews"

CLOTH_COLORS = {
    "red": (220, 40, 40),
    "blue": (40, 90, 220),
    "green": (40, 170, 70),
    "yellow": (230, 200, 40),
    "purple": (150, 60, 200),
    "pink": (230, 90, 160),
    "orange": (230, 130, 40),
}

BG_COLORS = {
    "light_gray": (200, 200, 200),
    "white": (245, 245, 245),
    "beige": (230, 220, 200),
    "light_blue": (190, 215, 235),
    "light_green": (200, 225, 200),
}

EXAMPLE_PROMPTS = ["clothes", "cup", "table", "robot arm", "box"]

PREVIEW_FRAME_LIMIT = 90
