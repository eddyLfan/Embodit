"""Validated, versioned configuration for automatic data quality scans."""

from __future__ import annotations

import copy
import hashlib
import json
from typing import Any


DEFAULT_CONFIG: dict[str, Any] = {
    "version": 3,
    "profile": "standard",
    "requirements": {
        "action": True,
        "state": False,
        "minimumCameras": 1,
        "requiredCameraKeys": [],
        "requiredCameraPatterns": [],
        "lengthToleranceFrames": 2,
        "lengthToleranceRatio": 0.1,
    },
    "detectors": {
        "signalIntegrity": {"enabled": True},
        "videoIntegrity": {
            "enabled": True,
            "minimumDecodedRatio": 0.9,
            "freezePixelDelta": 0.75,
            "freezeMinimumSeconds": 2.0,
            "freezeSampleFps": 5.0,
            "resizeWidth": 160,
        },
        "motion": {
            "enabled": True,
            "excludeNamePatterns": ["gripper"],
            "jitterSignalKeys": ["action"],
            "accelerationRobustZ": 8.0,
            "jerkRobustZ": 8.0,
            "minimumDimensionRange": 0.001,
            "minimumAccelerationRangeRatio": 0.15,
            "minimumJerkRangeRatio": 0.3,
            "derivativeAlignmentFrames": 1,
            "minimumAnomalySeconds": 0.08,
            "mergeGapSeconds": 0.15,
            "stationaryDelta": 0.0005,
            "stationaryMinimumSeconds": 3.0,
            "nearZeroMotionRange": 0.01,
        },
        "visualQuality": {
            "enabled": True,
            "darkMeanThreshold": 40.0,
            "brightMeanThreshold": 245.0,
            "blurVarianceThreshold": 35.0,
            "minimumIssueSeconds": 0.5,
            "mergeGapSeconds": 0.25,
            "resizeWidth": 320,
            "sampleFps": 4.0,
        },
        "cameraShake": {
            "enabled": True,
            "staticCameraKeys": [],
            "sampleFps": 2.0,
            "resizeWidth": 320,
            "jitterThreshold": 4.0,
            "uniformMotionRatio": 0.45,
        },
        "gripper": {
            "enabled": True,
            "namePatterns": ["gripper", "finger", "jaw"],
            "transitionThreshold": 0.2,
            "maxTransitionsPerSecond": 4.0,
            "regraspWindowSeconds": 5.0,
            "allowMultipleGrasps": False,
        },
    },
    "intervals": {"contextSeconds": 0.1},
    "scoring": {
        "passQualityScore": 80.0,
        "passUsableRatio": 90.0,
        "minimumCoverage": 80.0,
        "severityWeights": {"info": 0.0, "warning": 0.25, "error": 1.0, "fatal": 1.0},
    },
    "runtime": {
        "sleepBetweenEpisodes": 0.0,
        "episodeWorkers": 2,
        "cameraWorkers": 2,
    },
}

PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    # Integrity is always fully decoded. Fast only skips the expensive visual
    # quality and optical-flow checks.
    "fast": {
        "detectors": {
            "visualQuality": {"enabled": False},
            "cameraShake": {"enabled": False},
            "videoIntegrity": {"freezeSampleFps": 3.0, "resizeWidth": 128},
        },
        "runtime": {"episodeWorkers": 4, "cameraWorkers": 2},
    },
    "standard": {},
    "deep": {
        "detectors": {
            "videoIntegrity": {"freezeSampleFps": 10.0, "resizeWidth": 240},
            "visualQuality": {"sampleFps": 8.0, "resizeWidth": 480},
            "cameraShake": {"sampleFps": 4.0, "resizeWidth": 480},
        },
        "runtime": {"episodeWorkers": 2, "cameraWorkers": 2},
    },
}

def merge_config(override: dict[str, Any] | None = None) -> dict[str, Any]:
    """Deep-merge a user override into safe defaults."""

    def merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
        result = copy.deepcopy(base)
        for key, value in patch.items():
            if isinstance(value, dict) and isinstance(result.get(key), dict):
                result[key] = merge(result[key], value)
            else:
                result[key] = copy.deepcopy(value)
        return result

    patch = override or {}
    requested_profile = str(patch.get("profile", DEFAULT_CONFIG["profile"]))
    config = merge(DEFAULT_CONFIG, PROFILE_OVERRIDES.get(requested_profile, {}))
    # Explicit caller settings take precedence over the selected profile.
    config = merge(config, patch)
    if str(config.get("profile")) not in {"fast", "standard", "deep"}:
        raise ValueError("QC profile 必须是 fast、standard 或 deep")
    scoring = config["scoring"]
    for key in ("passQualityScore", "passUsableRatio", "minimumCoverage"):
        value = float(scoring[key])
        if value < 0 or value > 100:
            raise ValueError(f"{key} 必须在 0–100")
        scoring[key] = value
    requirements = config["requirements"]
    requirements["minimumCameras"] = max(0, int(requirements.get("minimumCameras", 1)))
    runtime = config["runtime"]
    for key in ("episodeWorkers", "cameraWorkers"):
        runtime[key] = min(16, max(1, int(runtime.get(key, 1))))
    motion = config["detectors"]["motion"]
    jitter_keys = motion.get("jitterSignalKeys") or ["action"]
    if isinstance(jitter_keys, str):
        jitter_keys = [jitter_keys]
    motion["jitterSignalKeys"] = [str(item) for item in jitter_keys if str(item)] or ["action"]
    for key in (
        "accelerationRobustZ",
        "jerkRobustZ",
        "minimumDimensionRange",
        "minimumAccelerationRangeRatio",
        "minimumJerkRangeRatio",
    ):
        motion[key] = max(0.0, float(motion[key]))
    motion["derivativeAlignmentFrames"] = min(
        5,
        max(0, int(motion.get("derivativeAlignmentFrames", 1))),
    )
    return config

def config_hash(config: dict[str, Any]) -> str:
    payload = json.dumps(config, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]
