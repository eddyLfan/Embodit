import dataclasses
import sys
from types import ModuleType, SimpleNamespace

import numpy as np
from PIL import Image

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
    @dataclasses.dataclass(frozen=True)
    class ModelConfig:
        action_horizon: int = 10

    @dataclasses.dataclass(frozen=True)
    class TrainConfig:
        name: str
        model: ModelConfig = dataclasses.field(default_factory=ModelConfig)

    class Policy:
        metadata = {"action_dim": 2}

        def infer(self, observation):
            assert observation["state"].dtype == np.float32
            return {"actions": np.asarray([[1.0, 2.0]])}

    def get_config(name):
        if name != "pi0_droid":
            raise KeyError(name)
        return TrainConfig(name=name)

    training_config = SimpleNamespace(get_config=get_config)
    received = {}
    def create_trained_policy(config, *_args, **_kwargs):
        received["action_horizon"] = config.model.action_horizon
        return Policy()
    policy_config = SimpleNamespace(create_trained_policy=create_trained_policy)
    _module(monkeypatch, "openpi")
    _module(monkeypatch, "openpi.policies", policy_config=policy_config)
    _module(monkeypatch, "openpi.training", config=training_config)

    adapter = OpenPIAdapter()
    adapter.load("/root/checkpoints/pi0_droid/10000", action_horizon=50)
    actions = adapter.predict({"state": [0.1, 0.2]})
    assert adapter.specification["config_name"] == "pi0_droid"
    assert adapter.specification["action_horizon"] == 50
    assert received["action_horizon"] == 50
    assert actions.tolist() == [[1.0, 2.0]]


def test_openpi_adapter_can_override_checkpoint_norm_stats_asset(monkeypatch) -> None:
    @dataclasses.dataclass(frozen=True)
    class ModelConfig:
        action_horizon: int = 50

    @dataclasses.dataclass(frozen=True)
    class AssetsConfig:
        asset_id: str | None = None

    @dataclasses.dataclass(frozen=True)
    class DataFactory:
        assets: AssetsConfig = dataclasses.field(default_factory=AssetsConfig)

    @dataclasses.dataclass(frozen=True)
    class TrainConfig:
        model: ModelConfig = dataclasses.field(default_factory=ModelConfig)
        data: DataFactory = dataclasses.field(default_factory=DataFactory)

    class Policy:
        metadata = {}

    received = {}

    def create_trained_policy(config, *_args, **_kwargs):
        received["asset_id"] = config.data.assets.asset_id
        return Policy()

    training_config = SimpleNamespace(get_config=lambda _name: TrainConfig())
    policy_config = SimpleNamespace(create_trained_policy=create_trained_policy)
    _module(monkeypatch, "openpi")
    _module(monkeypatch, "openpi.policies", policy_config=policy_config)
    _module(monkeypatch, "openpi.training", config=training_config)

    adapter = OpenPIAdapter()
    adapter.load(
        "/root/checkpoints/astribot/10000",
        config_name="pi05_astribot_full",
        norm_stats_asset_id="astribot/pick_block_into_basket_fix",
    )

    assert received["asset_id"] == "astribot/pick_block_into_basket_fix"
    assert adapter.specification["norm_stats_asset_id"] == (
        "astribot/pick_block_into_basket_fix"
    )


def test_openpi_adapter_can_match_checkpoint_training_image_resize(monkeypatch) -> None:
    @dataclasses.dataclass(frozen=True)
    class ModelConfig:
        action_horizon: int = 50

    @dataclasses.dataclass(frozen=True)
    class TrainConfig:
        model: ModelConfig = dataclasses.field(default_factory=ModelConfig)

    received = {}

    class Policy:
        metadata = {}

        def infer(self, observation):
            received.update(observation)
            return {"actions": np.asarray([[1.0, 2.0]])}

    training_config = SimpleNamespace(get_config=lambda _name: TrainConfig())
    policy_config = SimpleNamespace(create_trained_policy=lambda *_args, **_kwargs: Policy())
    _module(monkeypatch, "openpi")
    _module(monkeypatch, "openpi.policies", policy_config=policy_config)
    _module(monkeypatch, "openpi.training", config=training_config)

    source = np.asarray(
        [
            [[255, 0, 0], [0, 255, 0], [0, 0, 255], [255, 255, 255]],
            [[0, 0, 0], [64, 64, 64], [128, 128, 128], [192, 192, 192]],
        ],
        dtype=np.uint8,
    )
    expected = np.asarray(Image.fromarray(source).resize((3, 3), Image.Resampling.BICUBIC))

    adapter = OpenPIAdapter()
    adapter.load(
        "/root/checkpoints/astribot/215000",
        config_name="pi05_astribot_full",
        image_preprocessing={
            "mode": "stretch",
            "width": 3,
            "height": 3,
            "resample": "bicubic",
        },
    )
    adapter.predict({"observation/image": source, "observation/state": [0.1, 0.2]})

    assert received["observation/image"].shape == (3, 3, 3)
    assert np.array_equal(received["observation/image"], expected)
    assert adapter.specification["image_preprocessing"] == {
        "mode": "stretch",
        "width": 3,
        "height": 3,
        "resample": "bicubic",
    }


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
