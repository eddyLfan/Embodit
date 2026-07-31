"""Conversion report model."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any


@dataclass
class ConversionReport:
    source_format: str
    target_format: str
    source_path: str
    output_path: str
    episodes: int = 0
    frames: int = 0
    field_map: dict[str, str] = field(default_factory=dict)
    known_losses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
