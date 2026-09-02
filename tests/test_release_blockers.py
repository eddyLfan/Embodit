from __future__ import annotations

import json
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import app as app_module
from convert.pipeline import _emit
from datasets import export as export_module
from datasets.lerobot_v21 import LeRobotV21Adapter
from datasets.lerobot_v3_lib import validate_dataset as validate_v3_dataset
from datasets.mcap_adapter import McapAdapter, McapWriter


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def test_v21_subset_recomputes_statistics_after_renumbering(tmp_path: Path) -> None:
    source = tmp_path / "source"
    meta = source / "meta"
    data = source / "data" / "chunk-000"
    data.mkdir(parents=True)
    _write_json(
        meta / "info.json",
        {
            "codebase_version": "v2.1",
            "fps": 10,
            "total_episodes": 8,
            "total_frames": 22,
            "total_tasks": 1,
            "features": {
                "observation.state": {"dtype": "float32", "shape": [1]},
                "action": {"dtype": "float32", "shape": [1]},
            },
        },
    )
    (meta / "episodes.jsonl").write_text(
        json.dumps({"episode_index": 7, "length": 2, "tasks": ["pick"]}) + "\n",
        encoding="utf-8",
    )
    (meta / "tasks.jsonl").write_text(
        json.dumps({"task_index": 0, "task": "pick"}) + "\n",
        encoding="utf-8",
    )
    _write_json(meta / "stats.json", {"action": {"min": [999], "count": [999]}})
    (meta / "episodes_stats.jsonl").write_text(
        json.dumps({"episode_index": 7, "stats": {"action": {"min": [999]}}}) + "\n",
        encoding="utf-8",
    )
    table = pa.table(
        {
            "index": pa.array([20, 21], type=pa.int64()),
            "episode_index": pa.array([7, 7], type=pa.int64()),
            "frame_index": pa.array([0, 1], type=pa.int64()),
            "timestamp": pa.array([0.0, 0.1], type=pa.float64()),
            "observation.state": pa.array([[1.0], [3.0]], type=pa.list_(pa.float64())),
            "action": pa.array([[10.0], [12.0]], type=pa.list_(pa.float64())),
        }
    )
    pq.write_table(table, data / "episode_000007.parquet")

    output = tmp_path / "subset"
    LeRobotV21Adapter(source).export_subset(output, [7], media_mode="copy")

    exported = pq.read_table(output / "data" / "chunk-000" / "episode_000000.parquet")
    assert exported["index"].to_pylist() == [0, 1]
    assert exported["episode_index"].to_pylist() == [0, 0]

    stats = json.loads((output / "meta" / "stats.json").read_text(encoding="utf-8"))
    assert stats["action"]["min"] == [10.0]
    assert stats["action"]["max"] == [12.0]
    assert stats["action"]["count"] == [2]

    episode_rows = [
        json.loads(line)
        for line in (output / "meta" / "episodes_stats.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    assert len(episode_rows) == 1
    assert episode_rows[0]["episode_index"] == 0
    assert episode_rows[0]["stats"]["episode_index"]["min"] == [0.0]
    assert episode_rows[0]["stats"]["action"]["mean"] == [11.0]


def test_v21_rejects_data_directory_symlink_outside_dataset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_json(
        source / "meta" / "info.json",
        {
            "codebase_version": "v2.1",
            "fps": 10,
            "features": {"action": {"dtype": "float32", "shape": [1]}},
        },
    )
    external_data = tmp_path / "external-data"
    shard = external_data / "chunk-000" / "episode_000000.parquet"
    shard.parent.mkdir(parents=True)
    pq.write_table(
        pa.table(
            {
                "episode_index": pa.array([0], type=pa.int64()),
                "action": pa.array([[42.0]], type=pa.list_(pa.float64())),
            }
        ),
        shard,
    )
    (source / "data").symlink_to(external_data, target_is_directory=True)

    with pytest.raises(ValueError, match="超出数据集目录"):
        LeRobotV21Adapter(source).get_timeseries(0)


def test_v3_rejects_data_directory_symlink_outside_dataset(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _write_json(
        source / "meta" / "info.json",
        {"codebase_version": "v3.0", "fps": 10, "features": {}},
    )
    episode_shard = source / "meta" / "episodes" / "chunk-000" / "file-000.parquet"
    episode_shard.parent.mkdir(parents=True)
    pq.write_table(
        pa.table({"episode_index": pa.array([0], type=pa.int64())}),
        episode_shard,
    )
    external_data = tmp_path / "external-v3-data"
    data_shard = external_data / "chunk-000" / "file-000.parquet"
    data_shard.parent.mkdir(parents=True)
    pq.write_table(
        pa.table({"episode_index": pa.array([0], type=pa.int64())}),
        data_shard,
    )
    (source / "data").symlink_to(external_data, target_is_directory=True)

    with pytest.raises(ValueError, match="超出数据集目录"):
        validate_v3_dataset(source)


def test_progress_callback_failure_propagates_for_cancellation() -> None:
    class Cancelled(Exception):
        pass

    def cancel(_payload: dict) -> None:
        raise Cancelled("cancelled")

    with pytest.raises(Cancelled, match="cancelled"):
        _emit(cancel, stage="extract", progress=0.5)


def test_mcap_writer_removes_staging_file_after_interrupted_stream(tmp_path: Path) -> None:
    class Interrupted(Exception):
        pass

    def episodes():
        yield {
            "episode_index": 0,
            "length": 1,
            "state": [[0.1, 0.2]],
            "action": [[0.3, 0.4]],
        }
        raise Interrupted("stop writing")

    output = tmp_path / "cancelled.mcap"
    with pytest.raises(Interrupted, match="stop writing"):
        McapWriter().write_from_episodes(output, episodes(), {"fps": 10})

    assert not output.exists()
    assert list(tmp_path.glob(".cancelled.mcap.building-*")) == []


def test_same_format_export_propagates_cancellation_before_writing(
    tmp_path: Path, monkeypatch
) -> None:
    class Cancelled(BaseException):
        pass

    class Adapter:
        format_id = "mcap"

        def export_subset(self, *_args, **_kwargs):
            raise AssertionError("cancelled export must not start")

    monkeypatch.setattr(export_module, "open_dataset", lambda _path: Adapter())

    def cancel(_payload: dict) -> None:
        raise Cancelled("cancelled")

    with pytest.raises(Cancelled, match="cancelled"):
        export_module.export_dataset(
            tmp_path / "source.mcap",
            tmp_path / "output.mcap",
            [0],
            progress_callback=cancel,
        )


def test_mcap_subset_export_removes_staging_after_interruption(
    tmp_path: Path, monkeypatch
) -> None:
    import mcap.reader as mcap_reader

    source = tmp_path / "source.mcap"
    McapWriter().write_from_episodes(
        source,
        [
            {
                "episode_index": 0,
                "length": 2,
                "state": [[0.1], [0.2]],
                "action": [[0.3], [0.4]],
            }
        ],
        {"fps": 10},
    )
    adapter = McapAdapter(source)
    view = adapter.inspect()
    monkeypatch.setattr(adapter, "inspect", lambda: view)
    original_make_reader = mcap_reader.make_reader

    class Interrupted(BaseException):
        pass

    def interrupted_reader(handle):
        reader = original_make_reader(handle)

        class Reader:
            def iter_messages(self):
                for message in reader.iter_messages():
                    yield message
                    raise Interrupted("stop export")

        return Reader()

    monkeypatch.setattr(mcap_reader, "make_reader", interrupted_reader)
    output = tmp_path / "subset.mcap"
    with pytest.raises(Interrupted, match="stop export"):
        adapter.export_subset(output, [view.episodes[0].episode_index])

    assert not output.exists()
    assert list(tmp_path.glob(".subset.mcap.building-*")) == []
