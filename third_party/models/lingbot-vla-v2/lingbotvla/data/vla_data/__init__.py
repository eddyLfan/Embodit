"""Dataset classes are optional for standalone policy preprocessing."""
from importlib import import_module

__all__ = ["VLADataset", "MultiVLADataset"]

def __getattr__(name):
    modules = {"VLADataset": ".base_dataset", "MultiVLADataset": ".multi_vla_dataset"}
    if name not in modules:
        raise AttributeError(name)
    value = getattr(import_module(modules[name], __name__), name)
    globals()[name] = value
    return value
