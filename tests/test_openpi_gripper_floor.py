import dataclasses
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
import pytest

from deploy.assets.model_adapters import OpenPIAdapter


def adapter_for(actions, enabled=True):
    adapter = OpenPIAdapter()
    adapter.policy = SimpleNamespace(infer=lambda _: {"actions": actions})
    adapter.observation_map = {}
    adapter.image_preprocessing = None
    adapter.clamp_astribot_gripper_min_to_zero = enabled
    return adapter


def test_only_negative_grippers_are_zeroed_without_mutating_policy_output():
    original = np.full((50, 16), -0.25, dtype=np.float64)
    original[0, 7], original[0, 15] = -1.00905, -0.638989
    original[1, 7], original[1, 15] = 101.0, 34.0
    untouched = original.copy()
    result = adapter_for(original).predict({})
    assert result[0, 7] == result[0, 15] == 0
    assert result[1, 7] == 101.0  # still reaches the independent upper-limit gate
    assert result[1, 15] == 34.0
    arm_indices = [i for i in range(16) if i not in (7, 15)]
    np.testing.assert_array_equal(result[:, arm_indices], original[:, arm_indices])
    np.testing.assert_array_equal(original, untouched)


def test_other_models_are_unchanged_by_default():
    actions = np.full((50, 16), -1.0)
    assert adapter_for(actions, enabled=False).predict({}) is actions


@pytest.mark.parametrize('bad', [float('nan'), float('inf'), -float('inf')])
@pytest.mark.parametrize('index', [0, 7, 15])
def test_nonfinite_values_are_rejected_before_clamping(bad, index):
    actions = np.zeros((50, 16)); actions[0, index] = bad
    with pytest.raises(ValueError, match='NaN'):
        adapter_for(actions).predict({})


@pytest.mark.parametrize('shape', [(16,), (50, 15), (50, 32), (0, 16)])
def test_wrong_astribot_dimensions_are_rejected(shape):
    with pytest.raises(ValueError, match='H×16'):
        adapter_for(np.zeros(shape)).predict({})


def test_flag_is_not_forwarded_to_openpi_sampler(monkeypatch):
    @dataclasses.dataclass
    class Config:
        model: object = None
    captured = {}
    def create(*args, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(metadata={}, infer=lambda _: {'actions': np.full((50,16), -0.1)})
    for name, values in {
        'openpi': {},
        'openpi.policies': {'policy_config': SimpleNamespace(create_trained_policy=create)},
        'openpi.training': {'config': SimpleNamespace(get_config=lambda _: Config())},
    }.items():
        module = ModuleType(name)
        module.__dict__.update(values)
        monkeypatch.setitem(sys.modules, name, module)
    adapter = OpenPIAdapter()
    adapter.load('/fixture', config_name='astribot-test', clamp_astribot_gripper_min_to_zero=True, num_steps=10)
    assert captured['sample_kwargs'] == {'num_steps': 10}
    assert adapter.specification['clamp_astribot_gripper_min_to_zero'] is True
    assert adapter.predict({})[0, 15] == 0


def test_nonboolean_flag_rejected_before_loading():
    with pytest.raises(ValueError, match='布尔'):
        OpenPIAdapter().load('/fixture', clamp_astribot_gripper_min_to_zero='true')
