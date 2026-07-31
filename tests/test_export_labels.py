"""Label store and decision normalization tests."""

from __future__ import annotations

from pathlib import Path

from datasets.export import normalize_decision, episodes_for_export
from labels.store import delete_label, load_labels, upsert_label


def test_normalize_decision_legacy():
    assert normalize_decision("keep") == "pass"
    assert normalize_decision("exclude") == "quarantine"
    assert normalize_decision("pending") == "review"
    assert normalize_decision("pass") == "pass"


def test_episodes_for_export():
    states = {"0": "pass", "1": "review", "2": "quarantine", "3": "keep"}
    assert episodes_for_export(states) == [0, 3]
    assert episodes_for_export(states, include_review=True) == [0, 1, 3]


def test_labels_roundtrip(tmp_path: Path):
    path = tmp_path / "labels.jsonl"
    upsert_label(
        path,
        {
            "target": "episode",
            "episode_index": 1,
            "tags": ["adaptation_frame"],
            "quality_score": 4,
            "success": True,
            "note": "ok",
        },
    )
    upsert_label(
        path,
        {
            "target": "interval",
            "episode_index": 1,
            "start_s": 0.5,
            "end_s": 1.5,
            "tags": ["collision"],
        },
    )
    labels = load_labels(path)
    assert len(labels) == 2
    assert labels[0]["quality_score"] == 4 or labels[1]["quality_score"] == 4


def test_multiple_intervals_and_delete(tmp_path: Path):
    path = tmp_path / "labels.jsonl"
    upsert_label(
        path,
        {
            "target": "interval",
            "episode_index": 0,
            "start_s": 0.0,
            "end_s": 1.0,
            "tags": ["a"],
        },
    )
    upsert_label(
        path,
        {
            "target": "interval",
            "episode_index": 0,
            "start_s": 2.0,
            "end_s": 3.5,
            "tags": ["b"],
        },
    )
    # Same span updates in place (does not create a third record).
    upsert_label(
        path,
        {
            "target": "interval",
            "episode_index": 0,
            "start_s": 0.0,
            "end_s": 1.0,
            "tags": ["a-updated"],
            "note": "n1",
        },
    )
    labels = load_labels(path)
    intervals = [item for item in labels if item["target"] == "interval"]
    assert len(intervals) == 2
    first = next(item for item in intervals if item["start_s"] == 0.0)
    assert first["tags"] == ["a-updated"]
    assert first["note"] == "n1"

    delete_label(
        path,
        {
            "target": "interval",
            "episode_index": 0,
            "start_s": 2.0,
            "end_s": 3.5,
            "tags": [],
        },
    )
    labels = load_labels(path)
    intervals = [item for item in labels if item["target"] == "interval"]
    assert len(intervals) == 1
    assert intervals[0]["end_s"] == 1.0
