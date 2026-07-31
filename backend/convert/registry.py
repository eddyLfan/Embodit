"""Converter registry: explicit capability matrix per (source, target) pair.

Instead of advertising "anything × anything", each pair declares a fidelity
level and the caveats a user should know before converting, so the UI can
show what will actually be preserved.
"""

from __future__ import annotations

from typing import Any

from datasets.view import (
    FORMAT_HDF5,
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_MCAP,
    SUPPORTED_FORMATS,
)

# Fidelity levels:
#   full    — same format, lossless subset export
#   high    — video/state/action preserved; some metadata may be regenerated
#   partial — data preserved but with known losses (see notes)
_FULL = "full"
_HIGH = "high"
_PARTIAL = "partial"

_MATRIX: dict[tuple[str, str], dict[str, Any]] = {}


def _pair(src: str, dst: str, fidelity: str, notes: list[str] | None = None) -> None:
    _MATRIX[(src, dst)] = {"fidelity": fidelity, "notes": notes or []}


# Notes are stable keys; the frontend translates them via i18n
# (convertNote_<key>) so the hint follows the UI language.
for _fmt in SUPPORTED_FORMATS:
    _pair(_fmt, _fmt, _FULL, ["sameFormatLossless"])

_pair(FORMAT_LEROBOT_V21, FORMAT_LEROBOT_V3, _HIGH, ["videoHardlinkMetaRebuild"])
_pair(FORMAT_LEROBOT_V3, FORMAT_LEROBOT_V21, _HIGH, ["videoHardlinkMetaRebuild"])
_pair(FORMAT_LEROBOT_V21, FORMAT_HDF5, _PARTIAL, ["videoReencodeToFrames"])
_pair(FORMAT_LEROBOT_V3, FORMAT_HDF5, _PARTIAL, ["videoReencodeToFrames"])
_pair(FORMAT_HDF5, FORMAT_LEROBOT_V21, _PARTIAL, ["framesReencodeFpsInferred"])
_pair(FORMAT_HDF5, FORMAT_LEROBOT_V3, _PARTIAL, ["framesReencodeFpsInferred"])
_pair(FORMAT_MCAP, FORMAT_LEROBOT_V21, _PARTIAL, ["mcapReencodeDropTopicsCalib"])
_pair(FORMAT_MCAP, FORMAT_LEROBOT_V3, _PARTIAL, ["mcapReencodeDropTopicsCalib"])
_pair(FORMAT_MCAP, FORMAT_HDF5, _PARTIAL, ["mcapDecodeDropTopics"])
_pair(FORMAT_LEROBOT_V21, FORMAT_MCAP, _PARTIAL, ["synthTopicsTimestamps"])
_pair(FORMAT_LEROBOT_V3, FORMAT_MCAP, _PARTIAL, ["synthTopicsTimestamps"])
_pair(FORMAT_HDF5, FORMAT_MCAP, _PARTIAL, ["synthTopicsTimestamps"])


def pair_capability(source_format: str, target_format: str) -> dict[str, Any] | None:
    """Fidelity descriptor for a conversion pair (None = unsupported)."""
    return _MATRIX.get((source_format, target_format))


def list_conversion_targets(source_format: str) -> list[str]:
    return [dst for (src, dst) in _MATRIX if src == source_format]


def supported_pairs() -> list[tuple[str, str]]:
    return list(_MATRIX.keys())


def capability_matrix() -> list[dict[str, Any]]:
    """Serializable matrix for the UI."""
    return [
        {"source": src, "target": dst, **info}
        for (src, dst), info in sorted(_MATRIX.items())
    ]
