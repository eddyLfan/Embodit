"""Write augmented LeRobot datasets episode-by-episode."""

from __future__ import annotations

import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from augment.video_io import encode_video_mp4
from datasets.stats import StatsCollector
from datasets.tabular import episode_frame_table


def _safe_cam(camera: str) -> str:
    """Sanitize a camera key for filesystem paths (feature keys keep the original)."""
    return camera.replace("/", "_").replace("\\", "_").replace(" ", "_")


class AugmentDatasetWriter:
    """Accumulate augmented episodes into lerobot_v3 or lerobot_v21."""

    def __init__(
        self,
        output: Path,
        *,
        target_format: str,
        fps: float,
        robot_type: str | None = None,
        job_id: str | None = None,
        source_format: str | None = None,
    ) -> None:
        self.output = Path(output).expanduser().resolve()
        self.target_format = target_format
        self.fps = float(fps or 30.0)
        self.robot_type = robot_type
        self.source_format = source_format
        # Job-scoped staging dir: two jobs writing to the same output name can
        # no longer delete each other's staging area.
        suffix = f".augment-building-{job_id}" if job_id else ".augment-building"
        self.staging = self.output.parent / f".{self.output.name}{suffix}"
        if self.output.exists():
            raise FileExistsError(f"目标目录已经存在：{self.output}")
        if self.staging.exists():
            shutil.rmtree(self.staging)
        self.staging.mkdir(parents=True)
        self.episode_rows: list[dict[str, Any]] = []
        self.total_frames = 0
        self.features: dict[str, Any] = {}
        self.stats = StatsCollector()
        self._video_key_set: set[str] | None = None
        self.manifest_path = self.staging / "meta" / "augment_manifest.jsonl"
        self.manifest_path.parent.mkdir(parents=True, exist_ok=True)

    # -- streaming episode API -------------------------------------------------
    # begin_episode → add_camera_video (one camera at a time, encoded and
    # released immediately) → commit_episode. Only one camera's frames need to
    # be resident in memory at any point.

    def begin_episode(
        self,
        *,
        source_episode_index: int,
        length: int,
        tasks: list[str],
    ) -> dict[str, Any]:
        return {
            "new_index": len(self.episode_rows),
            "source_episode_index": int(source_episode_index),
            "length": int(length or 0),
            "tasks": list(tasks or []),
            "cams": {},
        }

    def _video_path(self, new_index: int, camera: str) -> Path:
        if self.target_format == "lerobot_v21":
            chunk = new_index // 1000
            return self.staging / "videos" / f"chunk-{chunk:03d}" / _safe_cam(camera) / f"episode_{new_index:06d}.mp4"
        chunk = new_index // 1000
        file_idx = new_index % 1000
        return self.staging / "videos" / _safe_cam(camera) / f"chunk-{chunk:03d}" / f"file-{file_idx:03d}.mp4"

    def add_camera_video(self, ctx: dict[str, Any], camera: str, frames: np.ndarray) -> None:
        frames = np.asarray(frames)
        if ctx["length"] <= 0:
            ctx["length"] = int(len(frames))
        if len(frames) != ctx["length"]:
            raise RuntimeError(f"{camera}: frames {len(frames)} != length {ctx['length']}")
        encode_video_mp4(frames, self._video_path(ctx["new_index"], camera), fps=self.fps)
        ctx["cams"][camera] = {
            "shape": [int(frames.shape[1]), int(frames.shape[2]), 3],
        }

    def commit_episode(
        self,
        ctx: dict[str, Any],
        *,
        state: np.ndarray | None,
        action: np.ndarray | None,
        sidecar: dict[str, Any],
    ) -> int:
        import pyarrow.parquet as pq

        new_index = int(ctx["new_index"])
        if new_index != len(self.episode_rows):
            raise RuntimeError("episode 提交顺序错误（begin/commit 必须成对且顺序执行）")
        length = int(ctx["length"])
        if state is not None:
            state = np.asarray(state)
            length = length or int(state.shape[0])
        if action is not None:
            action = np.asarray(action)
            length = length or int(action.shape[0])
        if length <= 0:
            raise ValueError("episode length must be > 0")
        if not ctx["cams"]:
            raise RuntimeError("episode 没有任何相机视频")

        # All episodes must expose the same camera set, otherwise the dataset
        # features become inconsistent across episodes and break training.
        cam_set = set(ctx["cams"])
        if self._video_key_set is None:
            self._video_key_set = cam_set
        elif cam_set != self._video_key_set:
            raise RuntimeError(
                f"episode {ctx['source_episode_index']} 的相机集合 {sorted(cam_set)} 与之前的 "
                f"{sorted(self._video_key_set)} 不一致，拒绝写入残缺 episode"
            )

        if state is not None:
            state = state[:length]
            self.stats.update("observation.state", state)
        if action is not None:
            action = action[:length]
            self.stats.update("action", action)

        table = episode_frame_table(
            episode_index=new_index,
            length=length,
            fps=self.fps,
            base_index=self.total_frames,
            state=state,
            action=action,
        )
        if self.target_format == "lerobot_v21":
            chunk = new_index // 1000
            data_path = self.staging / "data" / f"chunk-{chunk:03d}" / f"episode_{new_index:06d}.parquet"
        else:
            chunk = new_index // 1000
            file_idx = new_index % 1000
            data_path = self.staging / "data" / f"chunk-{chunk:03d}" / f"file-{file_idx:03d}.parquet"
        data_path.parent.mkdir(parents=True, exist_ok=True)
        pq.write_table(table, data_path, compression="zstd")

        for cam, info in ctx["cams"].items():
            self.features[cam] = {
                "dtype": "video",
                "shape": info["shape"],
                "names": ["height", "width", "channels"],
            }
        if state is not None:
            self.features["observation.state"] = {
                "dtype": "float32",
                "shape": [int(state.shape[-1])],
                "names": None,
            }
        if action is not None:
            self.features["action"] = {
                "dtype": "float32",
                "shape": [int(action.shape[-1])],
                "names": None,
            }

        row = {
            "episode_index": new_index,
            "length": length,
            "tasks": ctx["tasks"],
            "source_episode_index": ctx["source_episode_index"],
        }
        if self.target_format != "lerobot_v21":
            row.update(
                {
                    "dataset_from_index": self.total_frames,
                    "dataset_to_index": self.total_frames + length,
                    "data/chunk_index": new_index // 1000,
                    "data/file_index": new_index % 1000,
                }
            )
            for cam in ctx["cams"]:
                row[f"videos/{cam}/chunk_index"] = new_index // 1000
                row[f"videos/{cam}/file_index"] = new_index % 1000
                row[f"videos/{cam}/from_timestamp"] = 0.0
                row[f"videos/{cam}/to_timestamp"] = length / self.fps

        self.episode_rows.append(row)
        self.total_frames += length
        with self.manifest_path.open("a", encoding="utf-8") as handle:
            handle.write(
                json.dumps(
                    {
                        "ok": True,
                        "episode_index": new_index,
                        "source_episode_index": ctx["source_episode_index"],
                        **sidecar,
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
        return new_index

    def abort_episode(self, ctx: dict[str, Any]) -> None:
        """Remove any video files already written for a failed episode."""
        for cam in list(ctx["cams"]):
            path = self._video_path(ctx["new_index"], cam)
            path.unlink(missing_ok=True)
        ctx["cams"].clear()

    def add_episode(
        self,
        *,
        source_episode_index: int,
        length: int,
        tasks: list[str],
        state: np.ndarray | None,
        action: np.ndarray | None,
        videos: dict[str, np.ndarray],
        sidecar: dict[str, Any],
    ) -> int:
        """Non-streaming convenience wrapper around begin/add/commit."""
        ctx = self.begin_episode(source_episode_index=source_episode_index, length=length, tasks=tasks)
        try:
            for cam, frames in videos.items():
                self.add_camera_video(ctx, cam, frames)
            return self.commit_episode(ctx, state=state, action=action, sidecar=sidecar)
        except Exception:
            self.abort_episode(ctx)
            raise

    def cleanup(self) -> None:
        """Remove the staging directory (call on failure)."""
        shutil.rmtree(self.staging, ignore_errors=True)

    def finalize(self) -> dict[str, Any]:
        import pyarrow as pa
        import pyarrow.parquet as pq

        meta_dir = self.staging / "meta"
        meta_dir.mkdir(parents=True, exist_ok=True)
        if self.target_format == "lerobot_v21":
            with (meta_dir / "episodes.jsonl").open("w", encoding="utf-8") as handle:
                for row in self.episode_rows:
                    handle.write(
                        json.dumps(
                            {
                                "episode_index": row["episode_index"],
                                "length": row["length"],
                                "tasks": row.get("tasks") or [],
                            },
                            ensure_ascii=False,
                        )
                        + "\n"
                    )
            info = {
                "codebase_version": "v2.1",
                "robot_type": self.robot_type,
                "fps": self.fps,
                "total_episodes": len(self.episode_rows),
                "total_frames": self.total_frames,
                "total_tasks": len({t for row in self.episode_rows for t in (row.get("tasks") or [])}),
                "splits": {"train": f"0:{len(self.episode_rows)}"},
                "data_path": "data/chunk-{episode_chunk:03d}/episode_{episode_index:06d}.parquet",
                "video_path": "videos/chunk-{episode_chunk:03d}/{video_key}/episode_{episode_index:06d}.mp4",
                "features": self.features,
            }
        else:
            ep_dir = meta_dir / "episodes" / "chunk-000"
            ep_dir.mkdir(parents=True, exist_ok=True)
            pq.write_table(pa.Table.from_pylist(self.episode_rows), ep_dir / "file-000.parquet", compression="zstd")
            info = {
                "codebase_version": "v3.0",
                "robot_type": self.robot_type,
                "fps": self.fps,
                "total_episodes": len(self.episode_rows),
                "total_frames": self.total_frames,
                "total_tasks": len({t for row in self.episode_rows for t in (row.get("tasks") or [])}),
                "splits": {"train": f"0:{len(self.episode_rows)}"},
                "data_path": "data/chunk-{chunk_index:03d}/file-{file_index:03d}.parquet",
                "video_path": "videos/{video_key}/chunk-{chunk_index:03d}/file-{file_index:03d}.mp4",
                "features": self.features,
                "chunks_size": 1000,
            }
        (meta_dir / "info.json").write_text(json.dumps(info, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        (meta_dir / "stats.json").write_text(
            json.dumps(self.stats.to_stats_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tasks = sorted({task for row in self.episode_rows for task in (row.get("tasks") or [])})
        with (meta_dir / "tasks.jsonl").open("w", encoding="utf-8") as handle:
            for index, task in enumerate(tasks):
                handle.write(json.dumps({"task_index": index, "task": task}, ensure_ascii=False) + "\n")
        report = {
            "operation": "visual_augmentation",
            "sourceFormat": self.source_format,
            "targetFormat": self.target_format,
            "fidelity": "partial",
            "preserved": [
                "camera videos",
                "observation.state (when available)",
                "action (when available)",
                "episode tasks",
            ],
            "knownLosses": [
                "Only the standard state/action/video fields are reconstructed.",
                "Custom tabular features, calibration, source statistics and format-specific metadata are not copied.",
                "Video streams are decoded and re-encoded with H.264.",
            ],
            "manifest": "meta/augment_manifest.jsonl",
        }
        (meta_dir / "augmentation_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(self.staging, self.output)
        return {
            "output": str(self.output),
            "totalEpisodes": len(self.episode_rows),
            "totalFrames": self.total_frames,
            "format": self.target_format,
            "createdAt": datetime.now(timezone.utc).isoformat(),
        }
