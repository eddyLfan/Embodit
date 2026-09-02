"""Central, env-overridable configuration for previously hardcoded values."""

from __future__ import annotations

import os
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONFIG_DIR = PROJECT_ROOT / "config"
DATA_CONFIG_DIR = CONFIG_DIR / "data"
DEPLOYMENT_CONFIG_DIR = CONFIG_DIR / "deployment"


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


# Fallback fps for HDF5 datasets whose env_args carry no control frequency
# (override: EMBODIT_HDF5_FPS).
HDF5_DEFAULT_FPS = _env_float("EMBODIT_HDF5_FPS", 20.0)

# Gap (seconds) used to split a single MCAP log into episodes
# (override: EMBODIT_MCAP_GAP_S).
MCAP_GAP_S = _env_float("EMBODIT_MCAP_GAP_S", 2.0)

# Restrict client-supplied paths to the browse root. ``embodit.sh`` enables
# this automatically for non-loopback listeners; direct app launches can set
# EMBODIT_SANDBOX=1 explicitly.
SANDBOX_PATHS = os.environ.get("EMBODIT_SANDBOX", "").strip() in {"1", "true", "yes"}

# Human-review dropdown options. The server reads this file on each page load,
# so editing it only requires a browser refresh (override: EMBODIT_REVIEW_CONFIG).
_review_config_override = os.environ.get("EMBODIT_REVIEW_CONFIG", "").strip()
REVIEW_CONFIG_PATH = (
    Path(_review_config_override).expanduser().resolve()
    if _review_config_override
    else DATA_CONFIG_DIR / "review.json"
)

# User-facing generated artifacts belong to a stable output root independent
# of the dataset browse root (override: EMBODIT_OUTPUT_DIR).
_output_override = os.environ.get("EMBODIT_OUTPUT_DIR", "").strip()
OUTPUT_DIR = (
    Path(_output_override).expanduser().resolve()
    if _output_override
    else PROJECT_ROOT / "outputs"
)

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
QC_JOBS_DIR = JOBS_DIR / "qc"
HDF5_VIDEO_CACHE_DIR = CACHE_DIR / "media" / "hdf5"
MCAP_VIDEO_CACHE_DIR = CACHE_DIR / "media" / "mcap"
QC_REPORT_DIR = CACHE_DIR / "reports" / "qc"
QC_CACHE_DIR = CACHE_DIR
