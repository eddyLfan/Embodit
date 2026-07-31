"""Format detection tests."""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from datasets.detect import detect_format
from datasets.view import FORMAT_HDF5, FORMAT_LEROBOT_V21, FORMAT_LEROBOT_V3, FORMAT_MCAP


def test_detect_lerobot_v3(tmp_path: Path):
    root = tmp_path / "v3"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v3.0", "fps": 10}), encoding="utf-8"
    )
    (root / "meta" / "episodes").mkdir()
    assert detect_format(root) == FORMAT_LEROBOT_V3


def test_detect_lerobot_v21(tmp_path: Path):
    root = tmp_path / "v21"
    (root / "meta").mkdir(parents=True)
    (root / "meta" / "info.json").write_text(
        json.dumps({"codebase_version": "v2.1", "fps": 10}), encoding="utf-8"
    )
    (root / "meta" / "episodes.jsonl").write_text(
        json.dumps({"episode_index": 0, "length": 2}) + "\n", encoding="utf-8"
    )
    assert detect_format(root) == FORMAT_LEROBOT_V21


def test_detect_hdf5_file(tmp_path: Path):
    path = tmp_path / "demo.hdf5"
    path.write_bytes(b"\x89HDF\r\n\x1a\n")
    assert detect_format(path) == FORMAT_HDF5


def test_detect_hdf5_multifile_dir(tmp_path: Path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "a.hdf5").write_bytes(b"\x89HDF\r\n\x1a\n")
    (root / "b.hdf5").write_bytes(b"\x89HDF\r\n\x1a\n")
    assert detect_format(root) == FORMAT_HDF5


def test_detect_hdf5_nested_shard_dir(tmp_path: Path):
    root = tmp_path / "lift"
    shard = root / "00001"
    shard.mkdir(parents=True)
    (shard / "ep0.hdf5").write_bytes(b"\x89HDF\r\n\x1a\n")
    (shard / "ep1.h5").write_bytes(b"\x89HDF\r\n\x1a\n")
    assert detect_format(root) == FORMAT_HDF5
    parent = tmp_path / "suite"
    (parent / "lift" / "00001").mkdir(parents=True)
    (parent / "lift" / "00001" / "a.hdf5").write_bytes(b"\x89HDF\r\n\x1a\n")
    assert detect_format(parent) is None


def test_detect_mcap_file(tmp_path: Path):
    path = tmp_path / "log.mcap"
    path.write_bytes(b"\x89MCAP0\r\n")
    assert detect_format(path) == FORMAT_MCAP


def test_detect_mcap_multifile_dir(tmp_path: Path):
    root = tmp_path / "task"
    root.mkdir()
    (root / "00001.mcap").write_bytes(b"\x89MCAP0\r\n")
    (root / "00002.mcap").write_bytes(b"\x89MCAP0\r\n")
    assert detect_format(root) == FORMAT_MCAP


def test_detect_mcap_nested_shard_dir(tmp_path: Path):
    root = tmp_path / "clean_bowl"
    shard = root / "00001"
    shard.mkdir(parents=True)
    (shard / "00001.mcap").write_bytes(b"\x89MCAP0\r\n")
    (shard / "00002.mcap").write_bytes(b"\x89MCAP0\r\n")
    assert detect_format(root) == FORMAT_MCAP
    # Parent of task dirs should not match (two levels deep)
    parent = tmp_path / "Cooking"
    (parent / "clean_bowl" / "00001").mkdir(parents=True)
    (parent / "clean_bowl" / "00001" / "a.mcap").write_bytes(b"\x89MCAP0\r\n")
    assert detect_format(parent) is None
