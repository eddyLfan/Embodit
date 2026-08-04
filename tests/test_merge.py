"""Strict same-format dataset merge tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from convert.pipeline import convert_dataset
from datasets.lerobot_v3 import LeRobotV3Writer
from datasets.registry import open_dataset
from labels.store import default_labels_path, load_labels, save_labels
from merge.pipeline import merge_datasets, preflight_merge


def _write_v21(root: Path, *, episodes: int = 2, state_dim: int = 2, task: str = "demo") -> Path:
    (root / "meta").mkdir(parents=True)
    (root / "data" / "chunk-000").mkdir(parents=True)
    episode_rows = []
    total_frames = 0
    for episode in range(episodes):
        length = 3 + episode
        rows = []
        for frame in range(length):
            rows.append(
                {
                    "index": total_frames + frame,
                    "episode_index": episode,
                    "frame_index": frame,
                    "timestamp": frame / 10.0,
                    "task_index": 0,
                    "observation.state": [float(frame)] * state_dim,
                    "action": [float(episode), float(frame)],
                    "custom.signal": float(episode * 10 + frame),
                }
            )
        pq.write_table(
            pa.Table.from_pylist(rows),
            root / "data" / "chunk-000" / f"episode_{episode:06d}.parquet",
        )
        episode_rows.append({"episode_index": episode, "length": length, "tasks": [task]})
        total_frames += length
    (root / "meta" / "episodes.jsonl").write_text(
        "".join(json.dumps(row) + "\n" for row in episode_rows), encoding="utf-8"
    )
    (root / "meta" / "tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": task}) + "\n", encoding="utf-8"
    )
    info = {
        "codebase_version": "v2.1",
        "fps": 10,
        "robot_type": "testbot",
        "total_episodes": episodes,
        "total_frames": total_frames,
        "total_tasks": 1,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [state_dim]},
            "action": {"dtype": "float32", "shape": [2]},
            "custom.signal": {"dtype": "float32", "shape": [1]},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return root


def test_v21_merge_preserves_columns_order_and_labels(tmp_path: Path):
    first = _write_v21(tmp_path / "first", task="pick")
    second = _write_v21(tmp_path / "second", episodes=1, task="place")
    save_labels(
        default_labels_path(first),
        [{"target": "episode", "episode_index": 1, "tags": ["from-first"]}],
    )
    save_labels(
        default_labels_path(second),
        [{"target": "interval", "episode_index": 0, "start_s": 0, "end_s": 0.2, "tags": ["from-second"]}],
    )

    checked = preflight_merge([first, second])
    assert checked["compatible"] is True
    assert checked["totalEpisodes"] == 3

    output = tmp_path / "merged"
    result = merge_datasets([first, second], output)
    assert result["totalEpisodes"] == 3
    assert result["labelsMerged"] == 2
    view = open_dataset(output).inspect()
    assert [episode.episode_index for episode in view.episodes] == [0, 1, 2]
    assert [episode.tasks for episode in view.episodes] == [["pick"], ["pick"], ["place"]]
    third = pq.read_table(output / "data" / "chunk-000" / "episode_000002.parquet")
    assert "custom.signal" in third.column_names
    assert set(third["episode_index"].to_pylist()) == {2}
    assert third["index"].to_pylist() == [7, 8, 9]
    labels = load_labels(output / "labels.jsonl")
    assert sorted(label["episode_index"] for label in labels) == [1, 2]
    manifest = json.loads((output / "merge_manifest.json").read_text(encoding="utf-8"))
    assert manifest["episode_index_mapping"][-1]["source_episode_index"] == 0
    assert manifest["episode_index_mapping"][-1]["output_episode_index"] == 2


def test_preflight_rejects_schema_mismatch(tmp_path: Path):
    first = _write_v21(tmp_path / "first", state_dim=2)
    second = _write_v21(tmp_path / "second", state_dim=3)
    checked = preflight_merge([first, second])
    assert checked["compatible"] is False
    assert "feature_mismatch" in {item["code"] for item in checked["conflicts"]}
    with pytest.raises(ValueError, match="不兼容"):
        merge_datasets([first, second], tmp_path / "merged")


def _write_hdf5(path: Path, value: float) -> Path:
    h5py = pytest.importorskip("h5py")
    with h5py.File(path, "w") as handle:
        data = handle.create_group("data")
        data.attrs["env_args"] = json.dumps({"env_name": "testbot", "control_freq": 10})
        group = data.create_group("demo_0")
        group.attrs["source_value"] = value
        group.create_dataset("actions", data=np.full((3, 2), value, dtype=np.float32))
        group.create_dataset("states", data=np.full((3, 2), value + 1, dtype=np.float32))
        custom = group.create_group("custom")
        custom.create_dataset("raw", data=np.arange(3))
    return path


def test_hdf5_merge_copies_native_groups(tmp_path: Path):
    h5py = pytest.importorskip("h5py")
    first = _write_hdf5(tmp_path / "one.hdf5", 1)
    second = _write_hdf5(tmp_path / "two.hdf5", 2)
    result = merge_datasets([first, second], tmp_path / "merged")
    output = Path(result["output"])
    assert output.suffix == ".hdf5"
    with h5py.File(output, "r") as handle:
        assert sorted(handle["data"].keys()) == ["demo_0", "demo_1"]
        assert handle["data"]["demo_1"].attrs["source_value"] == 2
        assert handle["data"]["demo_0"]["custom"]["raw"][:].tolist() == [0, 1, 2]
    assert output.with_name(output.name + ".merge_manifest.json").is_file()


def test_v3_merge_rewrites_frame_and_episode_indices(tmp_path: Path):
    first_v21 = _write_v21(tmp_path / "first_v21", episodes=1)
    second_v21 = _write_v21(tmp_path / "second_v21", episodes=1)
    first = tmp_path / "first_v3"
    second = tmp_path / "second_v3"
    convert_dataset(first_v21, first, target_format="lerobot_v3")
    convert_dataset(second_v21, second, target_format="lerobot_v3")

    output = tmp_path / "merged_v3"
    result = merge_datasets([first, second], output)
    assert result["totalEpisodes"] == 2
    data = pq.read_table(output / "data" / "chunk-000" / "file-000.parquet")
    assert data["episode_index"].to_pylist() == [0, 0, 0, 1, 1, 1]
    assert data["index"].to_pylist() == list(range(6))
    assert len(open_dataset(output).inspect().episodes) == 2


def test_v3_merge_keeps_distinct_encoded_video_shards(tmp_path: Path):
    writer = LeRobotV3Writer()
    video_a = tmp_path / "a.mp4"
    video_b = tmp_path / "b.mp4"
    video_a.write_bytes(b"encoded-video-a")
    video_b.write_bytes(b"encoded-video-b")

    def episode(video: Path):
        return [{
            "episode_index": 0,
            "length": 3,
            "tasks": ["demo"],
            "state": np.ones((3, 2), dtype=np.float32),
            "action": np.ones((3, 1), dtype=np.float32),
            "video_paths": {"camera": str(video)},
        }]

    first = tmp_path / "first_v3"
    second = tmp_path / "second_v3"
    writer.write_from_episodes(first, episode(video_a), {"fps": 10, "robot_type": "testbot"})
    writer.write_from_episodes(second, episode(video_b), {"fps": 10, "robot_type": "testbot"})
    output = tmp_path / "merged_v3"
    result = merge_datasets([first, second], output, media_mode="copy")
    assert result["copiedVideoFiles"] == 2

    rows = pq.read_table(sorted(output.glob("meta/episodes/chunk-*/*.parquet"))).to_pylist()
    payloads = []
    for row in rows:
        path = output / (
            f"videos/camera/chunk-{int(row['videos/camera/chunk_index']):03d}/"
            f"file-{int(row['videos/camera/file_index']):03d}.mp4"
        )
        payloads.append(path.read_bytes())
    assert payloads == [b"encoded-video-a", b"encoded-video-b"]


def test_mcap_merge_rebases_episodes(tmp_path: Path):
    pytest.importorskip("mcap")
    first_v21 = _write_v21(tmp_path / "first_v21", episodes=1)
    second_v21 = _write_v21(tmp_path / "second_v21", episodes=1)
    first = tmp_path / "first.mcap"
    second = tmp_path / "second.mcap"
    convert_dataset(first_v21, first, target_format="mcap")
    convert_dataset(second_v21, second, target_format="mcap")

    result = merge_datasets([first, second], tmp_path / "merged")
    output = Path(result["output"])
    assert result["totalEpisodes"] == 2
    assert output.suffix == ".mcap"
    assert len(open_dataset(output).inspect().episodes) == 2


def test_merge_rejects_duplicate_and_nested_output(tmp_path: Path):
    first = _write_v21(tmp_path / "first", episodes=1)
    second = _write_v21(tmp_path / "second", episodes=1)
    with pytest.raises(ValueError, match="重复"):
        preflight_merge([first, first])
    with pytest.raises(ValueError, match="源数据集内部"):
        merge_datasets([first, second], first / "nested-output")


def test_cancelled_merge_removes_staging_output(tmp_path: Path):
    first = _write_v21(tmp_path / "first", episodes=1)
    second = _write_v21(tmp_path / "second", episodes=1)
    output = tmp_path / "merged"

    def cancel_on_first_episode(payload):
        if payload.get("stage") == "merge":
            raise KeyboardInterrupt("cancel test")

    with pytest.raises(KeyboardInterrupt, match="cancel test"):
        merge_datasets([first, second], output, progress_callback=cancel_on_first_episode)
    assert not output.exists()
    assert not list(tmp_path.glob(".merged.building-*"))
