"""Common QC records shared by detectors, storage and API layers."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

SEVERITIES = {"info", "warning", "error", "fatal"}
INTEGRITY_STATES = {"valid", "invalid", "unknown"}
DECISIONS = {"pass", "review", "quarantine"}
REVIEW_STATES = {"unreviewed", "confirmed", "rejected", "modified"}


@dataclass
class Finding:
    episode_index: int
    issue_code: str
    category: str
    severity: str = "warning"
    confidence: float = 1.0
    detector_id: str = "unknown"
    detector_version: str = "1"
    start_s: float | None = None
    end_s: float | None = None
    camera_key: str | None = None
    signal_key: str | None = None
    dimension_indices: list[int] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)
    threshold: dict[str, Any] = field(default_factory=dict)
    explanation: str = ""
    suggested_decision: str = "review"
    hard_invalid: bool = False
    finding_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def validate(self) -> "Finding":
        if self.severity not in SEVERITIES:
            raise ValueError(f"非法 severity：{self.severity}")
        if self.suggested_decision not in DECISIONS:
            raise ValueError(f"非法 suggested_decision：{self.suggested_decision}")
        self.episode_index = int(self.episode_index)
        self.confidence = min(1.0, max(0.0, float(self.confidence)))
        if self.start_s is not None:
            self.start_s = max(0.0, float(self.start_s))
        if self.end_s is not None:
            self.end_s = max(0.0, float(self.end_s))
        if self.start_s is not None and self.end_s is not None and self.end_s < self.start_s:
            raise ValueError("finding end_s 不能早于 start_s")
        return self

    def signature(self) -> str:
        stable = {
            "episode": self.episode_index,
            "issue": self.issue_code,
            "detector": self.detector_id,
            "start": None if self.start_s is None else round(self.start_s, 3),
            "end": None if self.end_s is None else round(self.end_s, 3),
            "camera": self.camera_key,
            "signal": self.signal_key,
            "dims": self.dimension_indices,
        }
        raw = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        result = asdict(self)
        result["stable_signature"] = self.signature()
        return result


@dataclass
class DetectorOutcome:
    detector_id: str
    version: str = "1"
    status: str = "completed"
    skip_reason: str | None = None
    findings: list[Finding] = field(default_factory=list)
    coverage_weight: float = 1.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "detectorId": self.detector_id,
            "version": self.version,
            "status": self.status,
            "skipReason": self.skip_reason,
            "coverageWeight": float(self.coverage_weight),
            "findings": [item.to_dict() for item in self.findings],
        }
