from __future__ import annotations

import base64

import numpy as np
import pytest

from datasets.view import CameraRef, DatasetView, EpisodeView, FORMAT_HDF5
from deploy.offline_evaluation import (
    evaluate_dataset_frame,
    load_dataset_action_replay,
)


class FakeAdapter:
    def __init__(self) -> None:
        episode = EpisodeView(
            episode_index=3,
            length=5,
            duration=0.5,
            tasks=["pick the cube"],
            cameras={"observation.images.front": CameraRef("observation.images.front", "frames")},
        )
        self.view = DatasetView(
            format_id=FORMAT_HDF5,
            path="/data/demo.hdf5",
            name="demo.hdf5",
            fps=10,
            robot_type="test",
            features={
                "action": {"dtype": "float32", "shape": [2], "names": ["joint_a", "joint_b"]},
                "observation.state": {"dtype": "float32", "shape": [2]},
            },
            episodes=[episode],
        )

    def inspect(self):
        return self.view

    def get_timeseries(self, episode_index):
        assert episode_index == 3
        return {
            "observation.state": np.arange(10, dtype=np.float32).reshape(5, 2),
            "action": np.asarray([[0, 0], [1, 2], [2, 4], [3, 6], [4, 8]], dtype=np.float32),
        }

    def get_frames(self, episode_index, camera_key):
        assert episode_index == 3
        assert camera_key == "observation.images.front"
        for index in range(5):
            yield np.full((4, 6, 3), index * 10, dtype=np.uint8)



class RedundantAdapter(FakeAdapter):
    def __init__(self) -> None:
        super().__init__()
        self.view.features["action"] = {
            "dtype": "float32",
            "shape": [5],
            "names": ["unused_a", "joint_b", "unused_b", "joint_a", "unused_c"],
        }
        self.view.features["observation.state"] = {
            "dtype": "float32",
            "shape": [4],
            "names": ["unused_state", "joint_b", "joint_a", "unused_state_2"],
        }

    def get_timeseries(self, episode_index):
        assert episode_index == 3
        frame = np.arange(5, dtype=np.float32)
        return {
            "action": np.column_stack((100 + frame, 10 + frame, 200 + frame, 20 + frame, 300 + frame)),
            "observation.state": np.column_stack((400 + frame, 30 + frame, 40 + frame, 500 + frame)),
        }
def test_load_dataset_action_replay_keeps_raw_frames_and_names() -> None:
    result = load_dataset_action_replay(
        FakeAdapter(),
        episode_index=3,
        start_frame=1,
        end_frame=4,
        action_names=["joint_a", "joint_b"],
    )

    assert result["fps"] == 10
    assert result["startFrame"] == 1
    assert result["endFrame"] == 4
    assert result["action"]["names"] == ["joint_a", "joint_b"]
    assert result["action"]["values"] == [
        [1.0, 2.0],
        [2.0, 4.0],
        [3.0, 6.0],
    ]



def test_offline_evaluation_compares_every_action_dimension() -> None:
    captured = {}

    def predict(observations):
        captured.update(observations)
        return {"action": {"values": [[1.5, 1.0], [2.5, 5.0], [3.5, 5.5]]}}

    result = evaluate_dataset_frame(
        FakeAdapter(),
        episode_index=3,
        frame_index=1,
        predictor=predict,
    )

    assert captured["observation.state"] == [2.0, 3.0]
    assert captured["prompt"] == "pick the cube"
    encoded = captured["observation.images.front"]["$binary"]
    assert base64.b64decode(encoded).startswith(b"\xff\xd8")
    assert result["action"]["groundTruth"] == [[1.0, 2.0], [2.0, 4.0], [3.0, 6.0]]
    assert result["action"]["names"] == ["joint_a", "joint_b"]
    assert result["action"]["dimensions"][0]["mae"] == pytest.approx(0.5)
    assert result["action"]["dimensions"][1]["maxAbsError"] == pytest.approx(1.0)
    assert result["images"][0]["dataUrl"].startswith("data:image/jpeg;base64,")


def test_replay_selects_and_reorders_only_configured_joints() -> None:
    result = load_dataset_action_replay(
        RedundantAdapter(),
        episode_index=3,
        start_frame=1,
        end_frame=3,
        action_names=["joint_a", "joint_b"],
    )

    assert result["action"]["names"] == ["joint_a", "joint_b"]
    assert result["action"]["values"] == [[21.0, 11.0], [22.0, 12.0]]
    assert result["action"]["sourceWidth"] == 5
    assert result["action"]["sourceIndices"] == [3, 1]
    assert result["action"]["droppedDimensions"] == 3


def test_offline_evaluation_projects_redundant_action_and_state() -> None:
    captured = {}
    result = evaluate_dataset_frame(
        RedundantAdapter(),
        episode_index=3,
        frame_index=1,
        predictor=lambda observations: captured.update(observations)
        or {"action": {"values": [[21, 11], [22, 12]]}},
        action_names=["joint_a", "joint_b"],
        state_names=["joint_a", "joint_b"],
    )

    assert captured["observation.state"] == [41.0, 31.0]
    assert result["state"]["names"] == ["joint_a", "joint_b"]
    assert result["state"]["sourceIndices"] == [2, 1]
    assert result["action"]["groundTruth"] == [[21.0, 11.0], [22.0, 12.0]]
    assert result["action"]["overallMae"] == 0


def test_replay_rejects_unsafe_wider_anonymous_action() -> None:
    adapter = RedundantAdapter()
    adapter.view.features["action"].pop("names")
    with pytest.raises(ValueError, match="无法将数据集 action 5 维映射"):
        load_dataset_action_replay(
            adapter,
            episode_index=3,
            action_names=["joint_a", "joint_b"],
        )


def test_offline_evaluation_truncates_prediction_at_episode_end() -> None:
    result = evaluate_dataset_frame(
        FakeAdapter(),
        episode_index=3,
        frame_index=4,
        predictor=lambda _observations: {"action": {"values": [[4, 8], [5, 10]]}},
    )
    assert result["action"]["requestedHorizon"] == 2
    assert result["action"]["comparedHorizon"] == 1


def test_offline_evaluation_respects_declared_available_frames() -> None:
    adapter = FakeAdapter()
    adapter.view.episodes[0].length = 3

    result = evaluate_dataset_frame(
        adapter,
        episode_index=3,
        frame_index=2,
        predictor=lambda _observations: {"action": {"values": [[2, 4], [3, 6], [4, 8]]}},
    )

    assert result["action"]["requestedHorizon"] == 3
    assert result["action"]["comparedHorizon"] == 1


def test_offline_evaluation_preserves_split_state_observation_keys() -> None:
    class SplitStateAdapter(FakeAdapter):
        def get_timeseries(self, episode_index):
            values = super().get_timeseries(episode_index)
            values.pop("observation.state")
            values["left_state"] = np.arange(5, dtype=np.float32)[:, None]
            values["right_state"] = np.arange(5, 10, dtype=np.float32)[:, None]
            return values

    captured = {}
    result = evaluate_dataset_frame(
        SplitStateAdapter(),
        episode_index=3,
        frame_index=1,
        predictor=lambda observations: captured.update(observations)
        or {"action": {"values": [[1, 2]]}},
    )

    assert captured["left_state"] == [1.0]
    assert captured["right_state"] == [6.0]
    assert "left_state+right_state" not in captured
    assert result["state"] == {"key": "left_state+right_state", "values": [1.0, 6.0]}


def test_offline_evaluation_rejects_action_width_mismatch() -> None:
    with pytest.raises(ValueError, match="维度"):
        evaluate_dataset_frame(
            FakeAdapter(),
            episode_index=3,
            frame_index=0,
            predictor=lambda _observations: {"action": {"values": [[1, 2, 3]]}},
        )


def test_offline_evaluation_rejects_multi_item_action_batch() -> None:
    with pytest.raises(ValueError, match="batch.*1"):
        evaluate_dataset_frame(
            FakeAdapter(),
            episode_index=3,
            frame_index=0,
            predictor=lambda _observations: {
                "action": {"values": [[[0, 0]], [[1, 2]]]}
            },
        )
