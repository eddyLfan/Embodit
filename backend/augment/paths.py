"""Paths and constants for Embodit augment."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
BACKEND_ROOT = Path(__file__).resolve().parents[1]
from settings import (  # noqa: E402,F401  (central config)
    AUGMENT_JOBS_DIR,
    AUGMENT_PREVIEW_DIR,
    CACHE_DIR,
    SAM3_CHECKPOINT,
)

DEFAULT_JOBS_DIR = AUGMENT_JOBS_DIR
DEFAULT_PREVIEW_DIR = AUGMENT_PREVIEW_DIR
DEFAULT_CACHE_DIR = CACHE_DIR / "reusable"

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
PREVIEW_MAX_SIDE = 640
BRIGHTNESS_SAMPLE_MAX_SIDE = 320
