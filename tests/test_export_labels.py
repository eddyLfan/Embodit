"""Label store and decision normalization tests."""

from __future__ import annotations

from pathlib import Path

from datasets.export import export_dataset, normalize_decision, episodes_for_export
from labels.store import delete_label, load_labels, save_labels, upsert_label


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


def test_file_export_filters_remaps_and_uses_dataset_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    source_labels = tmp_path / "source.labels.jsonl"
    save_labels(
        source_labels,
        [
            {"target": "episode", "episode_index": 7, "tags": ["seven"]},
            {
                "target": "interval",
                "episode_index": 2,
                "start_s": 0.0,
                "end_s": 1.0,
                "tags": ["two"],
            },
            {"target": "episode", "episode_index": 99, "tags": ["not-selected"]},
        ],
    )
    parent_labels = tmp_path / "labels.jsonl"
    parent_labels.write_text("parent-dataset-labels\n", encoding="utf-8")

    class Adapter:
        format_id = "hdf5"

        def export_subset(self, output, episode_indices, **_kwargs):
            assert episode_indices == [2, 7]
            actual = Path(output).with_suffix(".hdf5")
            actual.write_bytes(b"hdf5")
            return {"output": str(actual), "totalEpisodes": 2}

    monkeypatch.setattr("datasets.export.open_dataset", lambda _source: Adapter())
    result = export_dataset(
        tmp_path / "source.hdf5",
        tmp_path / "subset",
        [7, 2],
        labels_path=source_labels,
    )

    output = Path(result["output"])
    sidecar = output.with_name(output.name + ".labels.jsonl")
    assert sidecar.is_file()
    labels = load_labels(sidecar)
    assert [(item["episode_index"], item["tags"]) for item in labels] == [
        (1, ["seven"]),
        (0, ["two"]),
    ]
    assert parent_labels.read_text(encoding="utf-8") == "parent-dataset-labels\n"
    assert not (tmp_path / "subset.labels.jsonl").exists()
