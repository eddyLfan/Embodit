"""Color palettes and deterministic random picks."""

from __future__ import annotations

import hashlib
import random
from typing import Any

from augment.paths import BG_COLORS, CLOTH_COLORS


def palette_for_mode(apply_mode: str) -> dict[str, tuple[int, int, int]]:
    if apply_mode == "background_replace":
        return dict(BG_COLORS)
    return dict(CLOTH_COLORS)


def resolve_color(
    *,
    apply_mode: str,
    color_mode: str,
    color_name: str | None = None,
    color_rgb: list[int] | tuple[int, ...] | None = None,
    seed_key: str = "",
) -> dict[str, Any]:
    """Return {colorMode, colorName, colorRgb}."""
    palette = palette_for_mode(apply_mode)
    mode = (color_mode or "random").lower()

    if mode == "fixed" and color_rgb is not None and len(color_rgb) >= 3:
        rgb = [int(color_rgb[0]), int(color_rgb[1]), int(color_rgb[2])]
        name = color_name or "custom"
        return {"colorMode": "fixed", "colorName": name, "colorRgb": rgb}

    if mode == "fixed" and color_name and color_name in palette:
        rgb = list(palette[color_name])
        return {"colorMode": "fixed", "colorName": color_name, "colorRgb": rgb}

    # random (default)
    digest = hashlib.blake2b(seed_key.encode("utf-8"), digest_size=8).digest()
    rng = random.Random(int.from_bytes(digest, "big"))
    name = rng.choice(list(palette.keys()))
    rgb = list(palette[name])
    return {"colorMode": "random", "colorName": name, "colorRgb": rgb}


def options_payload() -> dict[str, Any]:
    return {
        "augTypes": [
            {"id": "brightness", "label": "亮度调节"},
            {"id": "color", "label": "颜色替换"},
        ],
        "applyModes": [
            {"id": "object_recolor", "label": "物体换色"},
            {"id": "background_replace", "label": "背景替换"},
        ],
        "clothColors": {k: list(v) for k, v in CLOTH_COLORS.items()},
        "bgColors": {k: list(v) for k, v in BG_COLORS.items()},
        "examplePrompts": ["clothes", "cup", "table", "robot arm", "box"],
        "defaultColorMode": "random",
    }
