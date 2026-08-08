"""Path validation shared by dataset adapters and writers."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_camera_key(value: Any) -> str:
    """Return a camera key that is safe to use as one filesystem component."""
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise ValueError(f"非法相机键：{value!r}")
    if "/" in value or "\\" in value or any(ord(char) < 32 for char in value):
        raise ValueError(f"相机键不能包含路径分隔符或控制字符：{value!r}")
    return value


def resolve_inside(root: Path, candidate: Path, *, what: str) -> Path:
    """Resolve symlinks and require ``candidate`` to remain inside ``root``."""
    resolved_root = root.expanduser().resolve()
    resolved_candidate = candidate.expanduser().resolve()
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError as error:
        raise ValueError(f"{what} 超出数据集目录：{candidate}") from error
    return resolved_candidate
