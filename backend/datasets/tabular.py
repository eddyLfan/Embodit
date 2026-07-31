"""Columnar helpers for building per-episode frame tables without Python loops."""

from __future__ import annotations

import numpy as np
import pyarrow as pa


def vector_list_array(values: np.ndarray) -> pa.Array:
    """Convert an (N, D) float array into a list<double> Arrow column in one shot."""
    values = np.asarray(values, dtype=np.float64)
    if values.ndim == 1:
        values = values[:, None]
    n = values.shape[0]
    d = int(np.prod(values.shape[1:])) if values.ndim > 1 else 1
    flat = pa.array(values.reshape(n * d), type=pa.float64())
    offsets = pa.array(np.arange(0, (n + 1) * d, d, dtype=np.int32))
    return pa.ListArray.from_arrays(offsets, flat)


def episode_frame_table(
    *,
    episode_index: int,
    length: int,
    fps: float,
    base_index: int,
    state: np.ndarray | None = None,
    action: np.ndarray | None = None,
) -> pa.Table:
    """Build the standard per-frame table (index/episode_index/frame_index/timestamp/state/action)."""
    length = int(length)
    columns: dict[str, pa.Array] = {
        "index": pa.array(np.arange(base_index, base_index + length, dtype=np.int64)),
        "episode_index": pa.array(np.full(length, episode_index, dtype=np.int64)),
        "frame_index": pa.array(np.arange(length, dtype=np.int64)),
        "timestamp": pa.array(np.arange(length, dtype=np.float64) / float(fps)),
    }
    if state is not None:
        columns["observation.state"] = vector_list_array(np.asarray(state)[:length])
    if action is not None:
        columns["action"] = vector_list_array(np.asarray(action)[:length])
    return pa.Table.from_pydict(columns)


def column_to_ndarray(column: pa.ChunkedArray | pa.Array) -> np.ndarray:
    """Convert a (possibly list-typed) Arrow column to a 1D/2D float64 ndarray without to_pylist."""
    import pyarrow.compute as pc

    if isinstance(column, pa.ChunkedArray):
        column = column.combine_chunks()
    if pa.types.is_list(column.type) or pa.types.is_large_list(column.type) or pa.types.is_fixed_size_list(
        column.type
    ):
        flat = pc.list_flatten(column).to_numpy(zero_copy_only=False).astype(np.float64)
        n = len(column)
        if n == 0:
            return flat.reshape(0, -1)
        return flat.reshape(n, -1)
    return column.to_numpy(zero_copy_only=False).astype(np.float64)


def align_to_schema(table: pa.Table, schema: pa.Schema) -> pa.Table:
    """Reorder/cast/null-fill a table so it can be appended to an existing ParquetWriter."""
    arrays: list[pa.Array] = []
    for field in schema:
        if field.name in table.column_names:
            column = table[field.name]
            if column.type != field.type:
                column = column.cast(field.type)
            arrays.append(column)
        else:
            arrays.append(pa.nulls(table.num_rows, field.type))
    return pa.Table.from_arrays(arrays, schema=schema)
