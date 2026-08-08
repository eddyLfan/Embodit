import importlib.util
from pathlib import Path

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "backend" / "deploy" / "assets" / "ros2_robot_client.py"
SPEC = importlib.util.spec_from_file_location("embodit_ros2_robot_client", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def client_for_validation():
    client = MODULE.RobotClient.__new__(MODULE.RobotClient)
    client.config = {
        "action": {
            "joints": ["j1", "j2"],
            "horizon": 2,
            "baseline_observation": "joint_position",
            "limits": {
                "minimum": [-2, -2],
                "maximum": [2, 2],
                "max_step": [0.2, 0.2],
            },
        }
    }
    return client


def test_robot_client_validates_action_against_explicit_joint_baseline() -> None:
    client = client_for_validation()
    result = client.validate_action(
        [[0.1, -0.1], [0.2, -0.2]],
        {"other_vector": [100, 100], "joint_position": [0, 0]},
    )
    assert result == [[0.1, -0.1], [0.2, -0.2]]


def test_robot_client_rejects_non_finite_action() -> None:
    client = client_for_validation()
    with pytest.raises(ValueError, match="NaN"):
        client.validate_action([[float("nan"), 0], [0, 0]], {"joint_position": [0, 0]})


def test_robot_client_rejects_large_step() -> None:
    client = client_for_validation()
    with pytest.raises(ValueError, match="max_step"):
        client.validate_action([[0.5, 0], [0.5, 0]], {"joint_position": [0, 0]})


def test_robot_client_rejects_wrong_horizon() -> None:
    client = client_for_validation()
    with pytest.raises(ValueError, match="horizon"):
        client.validate_action([[0, 0]], {"joint_position": [0, 0]})


def test_robot_client_rejects_numeric_strings_from_untrusted_model() -> None:
    client = client_for_validation()
    with pytest.raises(ValueError, match="JSON number"):
        client.validate_action([["0.1", 0], [0, 0]], {"joint_position": [0, 0]})
