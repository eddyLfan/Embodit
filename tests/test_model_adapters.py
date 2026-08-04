import sys
from types import ModuleType, SimpleNamespace

import numpy as np

from deploy.assets.model_adapters import LeRobotAdapter, OpenPIAdapter, StarVLAAdapter, native_observation


def _module(monkeypatch, name: str, **values):
    module = ModuleType(name)
    for key, value in values.items():
        setattr(module, key, value)
    monkeypatch.setitem(sys.modules, name, module)
    return module


def test_native_observation_decodes_ros_rgb_image() -> None:
    image = native_observation({
        "data": bytes([255, 0, 0, 0, 255, 0]),
        "width": 2,
        "height": 1,
        "encoding": "rgb8",
    })
    assert image.shape == (1, 2, 3)
    assert image.tolist() == [[[255, 0, 0], [0, 255, 0]]]


def test_openpi_adapter_loads_and_predicts_from_checkpoint(monkeypatch) -> None:
    class Policy:
        metadata = {"action_dim": 2}

        def infer(self, observation):
            assert observation["state"].dtype == np.float32
            return {"actions": np.asarray([[1.0, 2.0]])}

    def get_config(name):
        if name != "pi0_droid":
            raise KeyError(name)
        return SimpleNamespace(name=name)

    training_config = SimpleNamespace(get_config=get_config)
    policy_config = SimpleNamespace(create_trained_policy=lambda *_args, **_kwargs: Policy())
    _module(monkeypatch, "openpi")
    _module(monkeypatch, "openpi.policies", policy_config=policy_config)
    _module(monkeypatch, "openpi.training", config=training_config)

    adapter = OpenPIAdapter()
    adapter.load("/root/checkpoints/pi0_droid/10000")
    actions = adapter.predict({"state": [0.1, 0.2]})
    assert adapter.specification["config_name"] == "pi0_droid"
    assert actions.tolist() == [[1.0, 2.0]]


def test_lerobot_adapter_uses_checkpoint_embedded_config(monkeypatch) -> None:
    config = SimpleNamespace(
        type="act",
        device="cpu",
        input_features={"observation.state": object()},
        output_features={"action": object()},
    )

    class PreTrainedConfig:
        @staticmethod
        def from_pretrained(checkpoint, revision=None):
            assert checkpoint == "/root/checkpoints/act"
            return config

    class Policy:
        def eval(self): pass
        def reset(self): pass

    class PolicyClass:
        @staticmethod
        def from_pretrained(checkpoint, config=None, revision=None):
            return Policy()

    factory = ModuleType("lerobot.policies.factory")
    factory.get_policy_class = lambda _kind: PolicyClass
    factory.make_pre_post_processors = lambda **_kwargs: (lambda value: value, lambda value: value)
    _module(monkeypatch, "lerobot")
    _module(monkeypatch, "lerobot.configs", PreTrainedConfig=PreTrainedConfig)
    _module(monkeypatch, "lerobot.policies")
    monkeypatch.setitem(sys.modules, "lerobot.policies.factory", factory)

    adapter = LeRobotAdapter()
    adapter.load("/root/checkpoints/act")
    assert adapter.specification["policy_type"] == "act"
    assert adapter.specification["input_features"] == ["observation.state"]


def test_starvla_adapter_uses_official_policy_wrapper(monkeypatch) -> None:
    class Wrapper:
        metadata = {"action_dim": 2}

        def __init__(self, checkpoint, **kwargs):
            assert checkpoint == "/root/checkpoints/starvla"

        def predict_action(self, examples, **kwargs):
            assert examples[0]["lang"] == "pick cube"
            return {"actions": np.asarray([[[0.2, 0.3]]])}

    _module(monkeypatch, "deployment")
    _module(monkeypatch, "deployment.model_server")
    _module(monkeypatch, "deployment.model_server.policy_wrapper", PolicyServerWrapper=Wrapper)

    adapter = StarVLAAdapter()
    adapter.load("/root/checkpoints/starvla", default_prompt="pick cube")
    actions = adapter.predict({"joint_state": [0.0, 1.0]})
    assert actions.tolist() == [[0.2, 0.3]]
