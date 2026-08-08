"""MCAP augmentation fast-path tests."""

from __future__ import annotations

from pathlib import Path

from datasets.mcap_adapter import McapAdapter


def test_augmentation_view_probes_only_selected_files(tmp_path: Path, monkeypatch):
    files = []
    for index in range(20):
        path = tmp_path / f"{index:05d}.mcap"
        path.touch()
        files.append(path)

    calls: list[Path] = []

    def fake_meta(path: Path):
        calls.append(path)
        return {
            "topics": {
                "/camera/compressed": {
                    "count": 100,
                    "schema": "foxglove.CompressedImage",
                    "encoding": "protobuf",
                }
            },
            "start_ns": 1_000_000_000,
            "end_ns": 5_000_000_000,
            "message_count": 100,
        }

    adapter = McapAdapter(tmp_path)
    monkeypatch.setattr(adapter, "_episode_files", lambda: files)
    monkeypatch.setattr("datasets.mcap_adapter._quick_file_meta", fake_meta)

    view = adapter.inspect_for_augmentation([13])

    assert calls == [files[13]]
    assert [episode.episode_index for episode in view.episodes] == [13]
    assert view.episodes[0].extras["mcapFile"] == str(files[13])
    assert view.extras["augmentationView"] is True
    assert adapter._episode_mcap(13) == (files[13], view.episodes[0])
