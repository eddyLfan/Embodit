"""Augmentation readiness checks and stable configuration fingerprints."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from functools import lru_cache
from pathlib import Path
from typing import Any

from augment.paths import SAM3_CHECKPOINT

FINGERPRINT_FIELDS = (
    "dataset",
    "augType",
    "applyMode",
    "samPrompts",
    "colorMode",
    "colorName",
    "colorRgb",
    "brightnessMode",
    "brightnessGain",
    "brightnessGamma",
    "gpuId",
    "targetFormat",
    "cameraPolicy",
)


def config_fingerprint(config: dict[str, Any]) -> str:
    normalized = {key: config.get(key) for key in FINGERPRINT_FIELDS}
    payload = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _sam_python() -> Path:
    override = os.environ.get("AUGMENT_PYTHON", "").strip()
    return Path(override).expanduser().absolute() if override else Path(sys.executable).absolute()


def _probe_sam_environment(python_bin: Path) -> tuple[bool, str | None, dict[str, Any]]:
    if not python_bin.is_file():
        return False, f"Python 不存在：{python_bin}", {}
    # h5py/mcap are format-specific and imported lazily.  The worker baseline
    # only requires the packages used for every LeRobot color job.
    required = ("torch", "sam3", "numpy", "pyarrow", "av", "cv2", "PIL")
    names = json.dumps(required)
    script = (
        "import importlib.util, json; names=" + names + "; "
        "result={name: importlib.util.find_spec(name) is not None for name in names}; "
        "exec('import torch\\nresult[\\\"cuda\\\"] = torch.cuda.is_available()\\nresult[\\\"gpuCount\\\"] = torch.cuda.device_count()') if result['torch'] else None; "
        "print(json.dumps(result))"
    )
    try:
        completed = subprocess.run(  # noqa: S603
            [str(python_bin), "-c", script],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return False, f"无法检查 SAM3 Python：{error}", {}
    if completed.returncode != 0:
        return False, completed.stderr.strip() or "SAM3 Python 检查失败", {}
    try:
        result = json.loads(completed.stdout.strip())
    except json.JSONDecodeError:
        return False, "SAM3 Python 返回了无效检查结果", {}
    missing = [name for name in required if not result.get(name)]
    if missing:
        return False, "缺少 Python 包：" + ", ".join(missing), result
    if not result.get("cuda"):
        return False, "PyTorch 未检测到可用的 CUDA GPU", result
    return True, None, result


@lru_cache(maxsize=1)
def capabilities_payload() -> dict[str, Any]:
    checkpoint = Path(os.environ.get("AUGMENT_SAM3_CHECKPOINT", "") or SAM3_CHECKPOINT).expanduser()
    python_bin = _sam_python()
    environment_ok, environment_error, environment = _probe_sam_environment(python_bin)
    checkpoint_ok = checkpoint.is_file()
    reasons: list[str] = []
    if environment_error:
        reasons.append(environment_error)
    if not checkpoint_ok:
        reasons.append(f"SAM3 checkpoint 不存在：{checkpoint}")
    return {
        "brightness": {
            "available": True,
            "builtIn": True,
        },
        "color": {
            "available": environment_ok and checkpoint_ok,
            "builtInEffects": True,
            "python": str(python_bin),
            "checkpoint": str(checkpoint),
            "gpuCount": int(environment.get("gpuCount") or 0),
            "reason": "；".join(reasons) if reasons else None,
        },
    }
