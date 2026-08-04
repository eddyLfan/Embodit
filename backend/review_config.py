"""Human-review options loaded from one user-editable JSON file."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from pathlib import Path
from typing import Any


_REASON_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

DEFAULT_QUARANTINE_REASONS = [
    {"id": "task_failed", "label": {"zh": "任务未完成", "en": "Task not completed"}, "enabled": True},
    {
        "id": "unsafe_collision",
        "label": {"zh": "碰撞或不安全行为", "en": "Collision or unsafe behavior"},
        "enabled": True,
    },
    {"id": "human_intervention", "label": {"zh": "人工干预", "en": "Human intervention"}, "enabled": True},
    {"id": "poor_execution", "label": {"zh": "执行质量差", "en": "Poor execution"}, "enabled": True},
    {"id": "sensor_data", "label": {"zh": "传感器或数据异常", "en": "Sensor or data issue"}, "enabled": True},
    {"id": "other", "label": {"zh": "其他", "en": "Other"}, "enabled": True},
]


def _validate_reasons(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("quarantineReasons 必须是数组")
    reasons: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"quarantineReasons[{index}] 必须是对象")
        reason_id = str(item.get("id", "")).strip()
        if not _REASON_ID.fullmatch(reason_id):
            raise ValueError(
                f"quarantineReasons[{index}].id 无效：请使用 1-64 位字母、数字、点、下划线或连字符"
            )
        if reason_id in seen:
            raise ValueError(f"不合格原因 id 重复：{reason_id}")
        seen.add(reason_id)
        labels = item.get("label")
        if not isinstance(labels, dict):
            raise ValueError(f"quarantineReasons[{index}].label 必须是包含 zh/en 的对象")
        zh = str(labels.get("zh", "")).strip()
        en = str(labels.get("en", "")).strip()
        if not zh:
            raise ValueError(f"quarantineReasons[{index}].label.zh 不能为空")
        enabled = item.get("enabled", True)
        if not isinstance(enabled, bool):
            raise ValueError(f"quarantineReasons[{index}].enabled 必须是 true 或 false")
        reasons.append({
            "id": reason_id,
            "label": {"zh": zh, "en": en or zh},
            "enabled": enabled,
        })
    return reasons


def load_review_config(path: Path) -> dict[str, Any]:
    document = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(document, dict):
        raise ValueError("人工复核配置根节点必须是对象")
    return {"quarantineReasons": _validate_reasons(document.get("quarantineReasons"))}


def review_config_payload(path: Path) -> dict[str, Any]:
    """Read on every request so a browser refresh is enough after an edit."""
    try:
        payload = load_review_config(path)
        payload["source"] = str(path)
        return payload
    except (OSError, ValueError) as error:
        return {
            "quarantineReasons": deepcopy(DEFAULT_QUARANTINE_REASONS),
            "source": str(path),
            "configError": str(error),
        }
