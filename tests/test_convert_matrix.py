"""Conversion matrix smoke tests using synthetic minimal datasets."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from convert.pipeline import convert_dataset, iter_episode_payloads
from datasets import media
from datasets import lerobot_v3_lib
from datasets.hdf5_robomimic import Hdf5Writer
from datasets.lerobot_v3 import LeRobotV3Writer
from datasets.payload import EpisodePayload, validate_camera_key
from datasets.registry import open_dataset
from datasets.view import (
    FORMAT_HDF5,
    FORMAT_LEROBOT_V21,
    FORMAT_LEROBOT_V3,
    FORMAT_MCAP,
    DatasetView,
    EpisodeView,
)


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


def test_v21_rejects_invalid_camera_directory_name(tmp_path: Path) -> None:
    root = _write_minimal_v21(tmp_path / "src_v21")
    info_path = root / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["features"]["bad\\camera"] = {"dtype": "video", "shape": [1, 1, 3]}
    info_path.write_text(json.dumps(info), encoding="utf-8")
    video = root / "videos" / "chunk-000" / "bad\\camera" / "episode_000000.mp4"
    video.parent.mkdir(parents=True)
    video.write_bytes(b"not-needed-for-key-validation")

    with pytest.raises(ValueError, match="相机"):
        open_dataset(root).inspect()


def test_payload_iteration_preserves_requested_episode_order(tmp_path: Path):
    view = DatasetView(
        format_id=FORMAT_LEROBOT_V3,
        path=str(tmp_path),
        name="unordered",
        fps=10,
        robot_type=None,
        features={},
        episodes=[
            EpisodeView(episode_index=7, length=1, duration=0.1),
            EpisodeView(episode_index=2, length=1, duration=0.1),
        ],
    )

    class Adapter:
        def get_timeseries(self, episode_index):
            return {"action": np.asarray([[episode_index]], dtype=np.float32)}

    payloads = list(iter_episode_payloads(Adapter(), view, [2, 7], {}, []))
    assert [payload["episode_index"] for payload in payloads] == [2, 7]


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


def test_v3_writer_preserves_optional_series_introduced_after_first_episode(
    tmp_path: Path,
):
    episodes = [
        {
            "episode_index": 4,
            "length": 2,
            "state": np.ones((2, 3), dtype=np.float32),
        },
        {
            "episode_index": 9,
            "length": 2,
            "action": np.full((2, 2), 7.0, dtype=np.float32),
        },
    ]
    output = tmp_path / "late_action_v3"
    LeRobotV3Writer().write_from_episodes(output, iter(episodes), {"fps": 10})

    table = pq.read_table(output / "data" / "chunk-000" / "file-000.parquet")
    assert "observation.state" in table.column_names
    assert "action" in table.column_names
    assert table["action"].to_pylist() == [None, None, [7.0, 7.0], [7.0, 7.0]]
    info = json.loads((output / "meta" / "info.json").read_text(encoding="utf-8"))
    assert info["features"]["observation.state"]["shape"] == [3]
    assert info["features"]["action"]["shape"] == [2]


@pytest.mark.parametrize("camera", ["", ".", "..", "../escape", "a\\b", "bad\x00key"])
def test_camera_keys_cannot_escape_dataset_paths(camera: str) -> None:
    with pytest.raises(ValueError, match="相机"):
        validate_camera_key(camera)
    with pytest.raises(ValueError, match="相机"):
        EpisodePayload(episode_index=0, length=1, images={camera: []}).validate()


@pytest.mark.parametrize("camera", ["front cam", "front.camera", "前置相机"])
def test_camera_keys_preserve_compatible_names(camera: str) -> None:
    assert validate_camera_key(camera) == camera


@pytest.mark.parametrize(
    "template",
    ["../outside/{video_key}.mp4", "/tmp/{video_key}.mp4", "videos\\{video_key}.mp4"],
)
def test_v3_video_template_must_stay_relative(template: str) -> None:
    with pytest.raises(ValueError, match="video_path"):
        lerobot_v3_lib.format_video_path(template, "camera", 0, 0)


def test_v3_data_template_must_stay_relative() -> None:
    with pytest.raises(ValueError, match="data_path"):
        lerobot_v3_lib.format_data_path("../../outside-{file_index}.parquet", 0, 0)


def _write_v3_with_shared_video_shard(root: Path) -> Path:
    (root / "meta" / "episodes" / "chunk-000").mkdir(parents=True)
    (root / "data" / "chunk-000").mkdir(parents=True)
    video = root / "videos" / "camera" / "chunk-000" / "file-000.mp4"
    frames = [np.full((24, 24, 3), value, dtype=np.uint8) for value in range(0, 120, 20)]
    media.encode_frames_to_mp4(frames, video, fps=5.0)
    data_rows = [
        {
            "index": index,
            "episode_index": index // 3,
            "frame_index": index % 3,
            "timestamp": (index % 3) / 5.0,
            "action": [float(index)],
        }
        for index in range(6)
    ]
    pq.write_table(
        pa.Table.from_pylist(data_rows),
        root / "data" / "chunk-000" / "file-000.parquet",
    )
    episode_rows = []
    for episode_index in range(2):
        start = episode_index * 3 / 5.0
        episode_rows.append(
            {
                "episode_index": episode_index,
                "length": 3,
                "dataset_from_index": episode_index * 3,
                "dataset_to_index": episode_index * 3 + 3,
                "tasks": ["demo"],
                "data/chunk_index": 0,
                "data/file_index": 0,
                "videos/camera/chunk_index": 0,
                "videos/camera/file_index": 0,
                "videos/camera/from_timestamp": start,
                "videos/camera/to_timestamp": start + 3 / 5.0,
            }
        )
    pq.write_table(
        pa.Table.from_pylist(episode_rows),
        root / "meta" / "episodes" / "chunk-000" / "file-000.parquet",
    )
    info = {
        "codebase_version": "v3.0",
        "fps": 5,
        "total_episodes": 2,
        "total_frames": 6,
        "total_tasks": 1,
        "features": {
            "action": {"dtype": "float32", "shape": [1]},
            "camera": {"dtype": "video", "shape": [24, 24, 3]},
        },
        "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
        "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
    }
    (root / "meta" / "info.json").write_text(json.dumps(info), encoding="utf-8")
    return root


def test_v3_export_rejects_video_path_collision(tmp_path: Path) -> None:
    root = _write_v3_with_shared_video_shard(tmp_path / "source")
    info_path = root / "meta" / "info.json"
    info = json.loads(info_path.read_text(encoding="utf-8"))
    info["video_path"] = "meta/info.json"
    info_path.write_text(json.dumps(info), encoding="utf-8")

    with pytest.raises(FileExistsError, match="冲突"):
        open_dataset(root).export_subset(tmp_path / "output", [0])


def test_v3_inspect_rejects_video_symlink_outside_dataset(tmp_path: Path) -> None:
    root = _write_v3_with_shared_video_shard(tmp_path / "source")
    video = root / "videos" / "camera" / "chunk-000" / "file-000.mp4"
    external = tmp_path / "outside.mp4"
    external.write_bytes(video.read_bytes())
    video.unlink()
    video.symlink_to(external)

    with pytest.raises(ValueError, match="超出数据集目录"):
        open_dataset(root).inspect()


def test_cross_format_conversion_slices_shared_v3_video_shard(tmp_path: Path):
    h5py = pytest.importorskip("h5py")
    pytest.importorskip("av")
    source = _write_v3_with_shared_video_shard(tmp_path / "shared_v3")
    result = convert_dataset(source, tmp_path / "sliced", target_format=FORMAT_HDF5)

    with h5py.File(result["output"], "r") as handle:
        first = np.asarray(handle["data/demo_0/obs/camera"])
        second = np.asarray(handle["data/demo_1/obs/camera"])
    assert first.shape[0] == second.shape[0] == 3
    assert np.allclose(first.mean(axis=(1, 2, 3)), [0, 20, 40], atol=8)
    assert np.allclose(second.mean(axis=(1, 2, 3)), [60, 80, 100], atol=8)


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


def test_hdf5_subset_checks_normalized_output_without_truncating(tmp_path: Path):
    pytest.importorskip("h5py")
    source = _write_hdf5_with_images(tmp_path / "source.hdf5", episodes=1, frames=2)
    existing = tmp_path / "subset.hdf5"
    existing.write_bytes(b"do-not-overwrite")

    with pytest.raises(FileExistsError):
        open_dataset(source).export_subset(tmp_path / "subset", [0])

    assert existing.read_bytes() == b"do-not-overwrite"


def test_hdf5_writer_failure_leaves_no_partial_output(tmp_path: Path):
    pytest.importorskip("h5py")

    def episodes():
        yield {
            "episode_index": 0,
            "length": 2,
            "action": np.ones((2, 1), dtype=np.float32),
        }
        raise RuntimeError("synthetic failure")

    output = tmp_path / "failed"
    with pytest.raises(RuntimeError, match="synthetic failure"):
        Hdf5Writer().write_from_episodes(output, episodes(), {"fps": 10})

    assert not output.with_suffix(".hdf5").exists()
    assert not list(tmp_path.glob(".failed.hdf5.building-*"))


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
    joint_action = np.arange(frames * 25, dtype=np.float64).reshape(frames, 25)
    joint_state = joint_action + 0.25
    with h5py.File(path, "w") as handle:
        handle.attrs["created_at"] = "2026_04_20_14_10_05"
        commands = handle.create_group("command_poses_dict")
        commands.create_dataset("command", data=action)
        commands.create_dataset("timestamp", data=1000.0 + np.arange(frames) / 30.0)
        poses = handle.create_group("poses_dict")
        poses.create_dataset("merge_pose", data=state)
        poses.create_dataset("astribot_arm_left", data=state[:, :7])
        poses.create_dataset("astribot_arm_right", data=state[:, 7:14])
        joints = handle.create_group("joints_dict")
        joints.create_dataset("joints_position_command", data=joint_action)
        joints.create_dataset("joints_position_state", data=joint_state)
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
    assert view.features["action"]["shape"] == [25]
    assert view.features["observation.state"]["shape"] == [25]
    assert view.features["action"]["names"][7:23] == (
        [f"left_arm_{index}" for index in range(1, 8)]
        + ["left_gripper"]
        + [f"right_arm_{index}" for index in range(1, 8)]
        + ["right_gripper"]
    )

    series = adapter.get_timeseries(0)
    assert series["action"].shape == (4, 25)
    assert series["observation.state"].shape == (4, 25)
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


def test_convert_to_mcap_preserves_camera_as_compressed_image(tmp_path: Path):
    pytest.importorskip("mcap")
    src = _write_hdf5_with_images(tmp_path / "src.hdf5")
    out = tmp_path / "out.mcap"
    result = convert_dataset(src, out, target_format=FORMAT_MCAP)
    assert Path(result["output"]).is_file()
    adapter = open_dataset(out)
    view = adapter.inspect()
    assert view.episodes[0].cameras
    camera = next(iter(view.episodes[0].cameras.values()))
    frames = list(adapter.iter_topic_frames(view.episodes[0], camera.topic))
    assert len(frames) == 6
    assert frames[0].shape == (32, 32, 3)
