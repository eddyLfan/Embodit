"""Central, env-overridable configuration for previously hardcoded values."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _default_sam3_checkpoint() -> Path:
    """Prefer repo-local checkpoints/sam3.pt; no company path baked in."""
    override = os.environ.get("AUGMENT_SAM3_CHECKPOINT", "").strip()
    if override:
        return Path(override).expanduser().resolve()
    local = PROJECT_ROOT / "checkpoints" / "sam3.pt"
    return local


# SAM3 checkpoint for color augmentation (override: AUGMENT_SAM3_CHECKPOINT).
SAM3_CHECKPOINT = _default_sam3_checkpoint()

# Fallback fps for HDF5 datasets whose env_args carry no control frequency
# (override: EMBODIT_HDF5_FPS).
HDF5_DEFAULT_FPS = _env_float("EMBODIT_HDF5_FPS", 20.0)

# Gap (seconds) used to split a single MCAP log into episodes
# (override: EMBODIT_MCAP_GAP_S).
MCAP_GAP_S = _env_float("EMBODIT_MCAP_GAP_S", 2.0)

# Restrict client-supplied paths to the browse root. Off by default so users
# can browse anywhere on the machine; set EMBODIT_SANDBOX=1 to enforce.
SANDBOX_PATHS = os.environ.get("EMBODIT_SANDBOX", "").strip() in {"1", "true", "yes"}

# All generated cache, previews, detached job state and QC reports live under
# one root.  Keeping this outside source datasets guarantees that maintenance
# never mutates training payloads.
_cache_override = os.environ.get("EMBODIT_CACHE_DIR", "").strip()
CACHE_DIR = (
    Path(_cache_override).expanduser().resolve()
    if _cache_override
    else PROJECT_ROOT / ".embodit_cache"
)

# Stable, purpose-oriented layout.  Keep QC_CACHE_DIR as a compatibility alias
# for callers that only need to validate that a report is under the cache root.
JOBS_DIR = CACHE_DIR / "jobs"
CONVERT_JOBS_DIR = JOBS_DIR / "convert"
AUGMENT_JOBS_DIR = JOBS_DIR / "augment"
QC_JOBS_DIR = JOBS_DIR / "qc"
AUGMENT_PREVIEW_DIR = CACHE_DIR / "previews" / "augment"
SAM_TRACK_CACHE_DIR = CACHE_DIR / "reusable" / "sam_tracks"
HDF5_VIDEO_CACHE_DIR = CACHE_DIR / "media" / "hdf5"
MCAP_VIDEO_CACHE_DIR = CACHE_DIR / "media" / "mcap"
QC_REPORT_DIR = CACHE_DIR / "reports" / "qc"
QC_CACHE_DIR = CACHE_DIR
