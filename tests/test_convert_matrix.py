"""Conversion matrix smoke tests using synthetic minimal datasets."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from convert.pipeline import convert_dataset
from datasets.registry import open_dataset
from datasets.view import FORMAT_HDF5, FORMAT_LEROBOT_V21, FORMAT_LEROBOT_V3, FORMAT_MCAP


def _write_minimal_v21(root: Path, n_frames: int = 4) -> Path:
    root.mkdir(parents=True)
    (root / "meta").mkdir()
    (root / "data" / "chunk-000").mkdir(parents=True)
    rows = []
    for i in range(n_frames):
        rows.append(
            {
                "index": i,
                "episode_index": 0,
                "frame_index": i,
                "timestamp": i / 10.0,
                "observation.state": [float(i), float(i) * 0.1],
                "action": [float(i) * 0.2],
            }
        )
    pq.write_table(pa.Table.from_pylist(rows), root / "data" / "chunk-000" / "episode_000000.parquet")
    (root / "meta" / "episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "length": n_frames, "tasks": ["demo"]}) + "\n",
        encoding="utf-8",
    )
    info = {
        "codebase_version": "v2.1",
        "fps": 10,
        "total_episodes": 1,
        "total_frames": n_frames,
        "total_tasks": 1,
        "features": {
            "observation.state": {"dtype": "float32", "shape": [2]},
            "action": {"dtype": "float32", "shape": [1]},
        },
    }
    (root / "meta" / "info.json").write_text(json.dumps(info, indent=2), encoding="utf-8")
    (root / "meta" / "tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "demo"}) + "\n", encoding="utf-8"
    )
    return root


def test_v21_inspect_and_timeseries(tmp_path: Path):
    root = _write_minimal_v21(tmp_path / "src_v21")
    adapter = open_dataset(root)
    view = adapter.inspect()
    assert view.format_id == FORMAT_LEROBOT_V21
    assert len(view.episodes) == 1
    series = adapter.get_timeseries(0)
    assert "action" in series
    assert series["action"].shape[0] == 4


def test_v21_to_v3_convert(tmp_path: Path):
    root = _write_minimal_v21(tmp_path / "src_v21")
    out = tmp_path / "out_v3"
    result = convert_dataset(root, out, target_format=FORMAT_LEROBOT_V3)
    assert out.is_dir()
    assert (out / "meta" / "info.json").is_file()
    info = json.loads((out / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["codebase_version"] == "v3.0"
    assert result["report"]["episodes"] == 1
    adapter = open_dataset(out)
    assert adapter.inspect().format_id == FORMAT_LEROBOT_V3


def test_v21_to_hdf5_and_back(tmp_path: Path):
    h5py = pytest.importorskip("h5py")
    root = _write_minimal_v21(tmp_path / "src_v21")
    hdf5_out = tmp_path / "out.hdf5"
    result = convert_dataset(root, hdf5_out, target_format=FORMAT_HDF5)
    assert Path(result["output"]).is_file()
    adapter = open_dataset(Path(result["output"]))
    view = adapter.inspect()
    assert view.format_id == FORMAT_HDF5
    assert view.episodes[0].length == 4

    v21_back = tmp_path / "back_v21"
    convert_dataset(Path(result["output"]), v21_back, target_format=FORMAT_LEROBOT_V21)
    assert (v21_back / "meta" / "info.json").is_file()


def test_v21_to_mcap(tmp_path: Path):
    pytest.importorskip("mcap")
    root = _write_minimal_v21(tmp_path / "src_v21")
    out = tmp_path / "out.mcap"
    result = convert_dataset(root, out, target_format=FORMAT_MCAP)
    assert Path(result["output"]).is_file()
    adapter = open_dataset(Path(result["output"]))
    view = adapter.inspect()
    assert view.format_id == FORMAT_MCAP
    assert len(view.episodes) >= 1


def _write_hdf5_with_images(path: Path, episodes: int = 2, frames: int = 6) -> Path:
    h5py = pytest.importorskip("h5py")
    with h5py.File(path, "w") as handle:
        data = handle.create_group("data")
        rng = np.random.default_rng(0)
        for idx in range(episodes):
            group = data.create_group(f"demo_{idx}")
            group.create_dataset("actions", data=rng.random((frames, 3)).astype(np.float32))
            group.create_dataset("states", data=rng.random((frames, 4)).astype(np.float32))
            obs = group.create_group("obs")
            obs.create_dataset(
                "agentview_image",
                data=rng.integers(0, 255, size=(frames, 32, 32, 3), dtype=np.uint8),
            )
    return path


def _write_astribot_hdf5(path: Path, frames: int = 4) -> Path:
    h5py = pytest.importorskip("h5py")
    cv2 = pytest.importorskip("cv2")
    path.parent.mkdir(parents=True, exist_ok=True)
    action = np.arange(frames * 35, dtype=np.float64).reshape(frames, 35)
    state = np.arange(frames * 37, dtype=np.float64).reshape(frames, 37)
    with h5py.File(path, "w") as handle:
        handle.attrs["created_at"] = "2026_04_20_14_10_05"
        commands = handle.create_group("command_poses_dict")
        commands.create_dataset("command", data=action)
        commands.create_dataset("timestamp", data=1000.0 + np.arange(frames) / 30.0)
        poses = handle.create_group("poses_dict")
        poses.create_dataset("merge_pose", data=state)
        poses.create_dataset("astribot_arm_left", data=state[:, :7])
        poses.create_dataset("astribot_arm_right", data=state[:, 7:14])
        handle.create_dataset("time", data=1000.0 + np.arange(frames) / 30.0)

        images = handle.create_group("images_dict")
        for camera, rgb_value in (("head", (200, 40, 10)), ("left", (20, 160, 60))):
            encoded_frames = []
            for index in range(frames):
                rgb = np.full((24, 32, 3), rgb_value, dtype=np.uint8)
                rgb[index % 24, :, :] = (index * 20, 10, 220)
                ok, encoded = cv2.imencode(
                    ".jpg", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
                )
                assert ok
                encoded_frames.append(np.asarray(encoded, dtype=np.uint8))
            camera_group = images.create_group(camera)
            camera_group.create_dataset("rgb", data=np.concatenate(encoded_frames))
            camera_group.create_dataset(
                "rgb_size",
                data=np.asarray([len(item) for item in encoded_frames], dtype=np.float64),
            )
            camera_group.create_dataset(
                "rgb_timestamp", data=1000.0 + np.arange(frames) / 30.0
            )
    return path


def test_astribot_hdf5_inspect_timeseries_and_frames(tmp_path: Path):
    src = _write_astribot_hdf5(
        tmp_path / "hdf5_output_pick_cube" / "pick_cube_episode_1.hdf5"
    )
    adapter = open_dataset(src)
    view = adapter.inspect()

    assert view.extras["dialect"] == "astribot"
    assert view.robot_type == "Astribot"
    assert view.fps == pytest.approx(30.0, rel=1e-5)
    assert view.episodes[0].length == 4
    assert view.episodes[0].tasks == ["pick_cube"]
    assert sorted(view.episodes[0].cameras) == ["head", "left"]
    assert view.features["head"]["shape"] == [24, 32, 3]
    assert view.features["action"]["shape"] == [35]
    assert view.features["observation.state"]["shape"] == [37]

    series = adapter.get_timeseries(0)
    assert series["action"].shape == (4, 35)
    assert series["observation.state"].shape == (4, 37)
    assert series["eef.astribot_arm_left"].shape == (4, 7)

    decoded = list(adapter.get_frames(0, "head", chunk=2))
    assert len(decoded) == 4
    assert decoded[0].shape == (24, 32, 3)
    assert decoded[0].dtype == np.uint8
    assert int(decoded[0][10, 10, 0]) > int(decoded[0][10, 10, 1])
    assert int(decoded[0][10, 10, 1]) > int(decoded[0][10, 10, 2])


def test_astribot_hdf5_materializes_browser_video(tmp_path: Path):
    pytest.importorskip("imageio_ffmpeg")
    src = _write_astribot_hdf5(
        tmp_path / "hdf5_output_pick_cube" / "pick_cube_episode_1.hdf5"
    )
    adapter = open_dataset(src)
    video = adapter.materialize_camera_video(0, "head")
    assert video.is_file()
    assert video.stat().st_size > 0


def test_hdf5_images_to_v3_per_episode_videos(tmp_path: Path):
    pytest.importorskip("imageio_ffmpeg")
    src = _write_hdf5_with_images(tmp_path / "src.hdf5")
    out = tmp_path / "out_v3"
    result = convert_dataset(src, out, target_format=FORMAT_LEROBOT_V3)
    assert result["totalEpisodes"] == 2
    # Regression: each episode must land in its own video shard.
    shards = sorted((out / "videos" / "agentview_image" / "chunk-000").glob("file-*.mp4"))
    assert [p.name for p in shards] == ["file-000.mp4", "file-001.mp4"]
    info = json.loads((out / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["features"]["agentview_image"]["dtype"] == "video"
    stats = json.loads((out / "meta" / "stats.json").read_text(encoding="utf-8"))
    assert "action" in stats and stats["action"]["count"] == [12]


def test_hdf5_images_to_v21_preserves_camera(tmp_path: Path):
    pytest.importorskip("imageio_ffmpeg")
    src = _write_hdf5_with_images(tmp_path / "src.hdf5")
    out = tmp_path / "out_v21"
    convert_dataset(src, out, target_format=FORMAT_LEROBOT_V21)
    videos = sorted((out / "videos" / "chunk-000" / "agentview_image").glob("episode_*.mp4"))
    assert len(videos) == 2


def test_convert_to_mcap_requires_camera_loss_ack(tmp_path: Path):
    pytest.importorskip("mcap")
    src = _write_hdf5_with_images(tmp_path / "src.hdf5")
    out = tmp_path / "out.mcap"
    with pytest.raises(ValueError, match="allow_camera_loss"):
        convert_dataset(src, out, target_format=FORMAT_MCAP)
    result = convert_dataset(
        src, tmp_path / "out2.mcap", target_format=FORMAT_MCAP, mapping={"allow_camera_loss": True}
    )
    assert Path(result["output"]).is_file()
