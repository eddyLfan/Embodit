#!/usr/bin/env python3
"""Unified cache layout, legacy migration, and retention-based cleanup."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import settings

DAY = 24 * 60 * 60
TERMINAL_JOB_STATES = {"completed", "failed", "cancelled"}


@dataclass(frozen=True)
class CacheLayout:
    root: Path
    jobs: Path
    convert_jobs: Path
    augment_jobs: Path
    qc_jobs: Path
    previews: Path
    augment_previews: Path
    reusable: Path
    sam_tracks: Path
    media: Path
    hdf5_media: Path
    mcap_media: Path
    reports: Path
    qc_reports: Path

    @classmethod
    def under(cls, root: Path) -> "CacheLayout":
        root = root.expanduser().resolve()
        return cls(
            root=root,
            jobs=root / "jobs",
            convert_jobs=root / "jobs" / "convert",
            augment_jobs=root / "jobs" / "augment",
            qc_jobs=root / "jobs" / "qc",
            previews=root / "previews",
            augment_previews=root / "previews" / "augment",
            reusable=root / "reusable",
            sam_tracks=root / "reusable" / "sam_tracks",
            media=root / "media",
            hdf5_media=root / "media" / "hdf5",
            mcap_media=root / "media" / "mcap",
            reports=root / "reports",
            qc_reports=root / "reports" / "qc",
        )

    def managed_dirs(self) -> tuple[Path, ...]:
        return (
            self.convert_jobs,
            self.augment_jobs,
            self.qc_jobs,
            self.augment_previews,
            self.sam_tracks,
            self.hdf5_media,
            self.mcap_media,
            self.qc_reports,
        )


def default_layout() -> CacheLayout:
    return CacheLayout.under(settings.CACHE_DIR)


def validate_layout(layout: CacheLayout) -> None:
    forbidden = {
        Path(layout.root.anchor).resolve(),
        Path.home().resolve(),
        Path(tempfile.gettempdir()).resolve(),
        settings.PROJECT_ROOT.resolve(),
    }
    if layout.root in forbidden:
        raise ValueError(
            "EMBODIT_CACHE_DIR 必须是专用子目录，不能指向文件系统根目录、用户目录、"
            f"系统临时目录或项目根目录：{layout.root}"
        )


def ensure_layout(layout: CacheLayout | None = None) -> CacheLayout:
    layout = layout or default_layout()
    validate_layout(layout)
    for path in layout.managed_dirs():
        path.mkdir(parents=True, exist_ok=True)
    return layout


def _unique_destination(path: Path) -> Path:
    if not path.exists() and not path.is_symlink():
        return path
    for index in range(1, 10_000):
        candidate = path.with_name(f"{path.name}.legacy-{index}")
        if not candidate.exists() and not candidate.is_symlink():
            return candidate
    raise RuntimeError(f"无法为旧缓存生成无冲突路径：{path}")


def _merge_tree(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for child in list(source.iterdir()):
        target = destination / child.name
        if child.is_dir() and not child.is_symlink() and target.is_dir():
            _merge_tree(child, target)
            try:
                child.rmdir()
            except OSError:
                pass
            continue
        shutil.move(str(child), str(_unique_destination(target)))
    try:
        source.rmdir()
    except OSError:
        pass


def _move_legacy(source: Path, destination: Path) -> bool:
    if source.is_symlink():
        return False
    if not source.exists() or source.resolve() == destination.resolve():
        return False
    destination.parent.mkdir(parents=True, exist_ok=True)
    if not destination.exists():
        shutil.move(str(source), str(destination))
    elif source.is_dir():
        _merge_tree(source, destination)
    else:
        shutil.move(str(source), str(_unique_destination(destination / source.name)))
    return True


def _replace_prefix(value: str, replacements: list[tuple[Path, Path]]) -> str:
    for old, new in replacements:
        old_text = str(old)
        if value == old_text:
            return str(new)
        prefix = old_text + os.sep
        if value.startswith(prefix):
            return str(new / value[len(prefix) :])
    return value


def _rewrite_job_paths(layout: CacheLayout, replacements: list[tuple[Path, Path]]) -> int:
    changed = 0
    for folder in (layout.convert_jobs, layout.augment_jobs, layout.qc_jobs):
        for path in folder.glob("*.json"):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            dirty = False
            for key in ("logPath", "previewDir", "reportPath"):
                value = payload.get(key)
                if not isinstance(value, str):
                    continue
                replacement = _replace_prefix(value, replacements)
                if replacement != value:
                    payload[key] = replacement
                    dirty = True
            if dirty:
                temporary = path.with_name(f".{path.name}.migrate-{os.getpid()}")
                temporary.write_text(
                    json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
                )
                os.replace(temporary, path)
                changed += 1
    return changed


def migrate_legacy(
    layout: CacheLayout | None = None,
    *,
    project_root: Path | None = None,
    temp_root: Path | None = None,
) -> dict[str, Any]:
    """Move pre-unification directories into the unified cache tree."""
    layout = layout or default_layout()
    validate_layout(layout)
    project_root = (project_root or settings.PROJECT_ROOT).expanduser().resolve()
    temp_root = (temp_root or Path(tempfile.gettempdir())).expanduser().resolve()
    layout.root.mkdir(parents=True, exist_ok=True)
    removed_links = remove_legacy_links(layout, project_root)
    mappings = [
        (project_root / ".convert_jobs", layout.convert_jobs),
        (project_root / ".augment_jobs", layout.augment_jobs),
        (project_root / ".augment_previews", layout.augment_previews),
        (project_root / ".augment_cache" / "sam_tracks", layout.sam_tracks),
        (layout.root / "qc_jobs", layout.qc_jobs),
        (layout.root / "qc", layout.qc_reports),
        (temp_root / "embody-hdf5-video", layout.hdf5_media),
        (temp_root / "embody-mcap-video", layout.mcap_media),
    ]
    moved: list[str] = []
    replacements: list[tuple[Path, Path]] = []
    for source, destination in mappings:
        if _move_legacy(source, destination):
            moved.append(f"{source} -> {destination}")
        replacements.append((source, destination))
    ensure_layout(layout)
    rewritten = _rewrite_job_paths(layout, replacements)
    return {
        "moved": moved,
        "rewrittenJobs": rewritten,
        "removedLegacyLinks": removed_links,
    }


def remove_legacy_links(layout: CacheLayout | None = None, project_root: Path | None = None) -> list[str]:
    """Remove compatibility links created by older releases, never real dirs."""
    layout = layout or default_layout()
    validate_layout(layout)
    project_root = (project_root or settings.PROJECT_ROOT).expanduser().resolve()
    removed: list[str] = []
    for path in (
        project_root / ".convert_jobs",
        project_root / ".augment_jobs",
        project_root / ".augment_previews",
        project_root / ".augment_cache" / "sam_tracks",
        layout.root / "qc_jobs",
        layout.root / "qc",
    ):
        if not path.is_symlink():
            continue
        try:
            path.resolve(strict=False).relative_to(layout.root)
        except ValueError:
            continue
        path.unlink(missing_ok=True)
        removed.append(str(path))
    legacy_augment_cache = project_root / ".augment_cache"
    if legacy_augment_cache.is_dir() and not legacy_augment_cache.is_symlink():
        try:
            legacy_augment_cache.rmdir()
        except OSError:
            pass
    return removed


def _env_days(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def retention_policy() -> dict[str, int]:
    return {
        "previewDays": _env_days("EMBODIT_PREVIEW_TTL_DAYS", 7),
        "mediaDays": _env_days("EMBODIT_MEDIA_TTL_DAYS", 7),
        "samDays": _env_days("EMBODIT_SAM_CACHE_TTL_DAYS", 30),
        "jobDays": _env_days("EMBODIT_JOB_TTL_DAYS", 30),
        "tempDays": _env_days("EMBODIT_TEMP_TTL_DAYS", 1),
        "qcReportsPerDataset": _env_int("EMBODIT_QC_REPORTS_PER_DATASET", 5),
    }


def _latest_mtime(path: Path) -> float:
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_mtime
        latest = path.stat().st_mtime
        for child in path.rglob("*"):
            try:
                latest = max(latest, child.stat().st_mtime)
            except OSError:
                pass
        return latest
    except OSError:
        return 0.0


def _size(path: Path) -> int:
    try:
        if path.is_file() or path.is_symlink():
            return path.stat().st_size
        return sum(child.stat().st_size for child in path.rglob("*") if child.is_file())
    except OSError:
        return 0


def _job_timestamp(payload: dict[str, Any], path: Path) -> float:
    for key in ("updatedAt", "createdAt"):
        value = payload.get(key)
        if not value:
            continue
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return _latest_mtime(path)


class Cleaner:
    def __init__(self, layout: CacheLayout, *, dry_run: bool, now: float) -> None:
        self.layout = layout
        self.dry_run = dry_run
        self.now = now
        self.actions: list[dict[str, Any]] = []

    def remove(self, path: Path, reason: str) -> None:
        if not path.exists() and not path.is_symlink():
            return
        try:
            path.resolve(strict=False).relative_to(self.layout.root)
        except ValueError as error:
            raise RuntimeError(f"拒绝清理缓存根目录之外的路径：{path}") from error
        size = _size(path)
        self.actions.append({"path": str(path), "reason": reason, "bytes": size})
        if self.dry_run:
            return
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        else:
            path.unlink(missing_ok=True)

    def expired(self, path: Path, days: int) -> bool:
        return self.now - _latest_mtime(path) >= days * DAY


def _read_job(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _clean_terminal_jobs(cleaner: Cleaner, policy: dict[str, int]) -> None:
    families = (
        ("convert", cleaner.layout.convert_jobs),
        ("augment", cleaner.layout.augment_jobs),
        ("qc", cleaner.layout.qc_jobs),
    )
    for family, folder in families:
        for job_path in folder.glob("*.json"):
            payload = _read_job(job_path)
            if not payload or payload.get("status") not in TERMINAL_JOB_STATES:
                continue
            days = policy["jobDays"]
            if family == "augment" and payload.get("mode") == "preview":
                days = min(days, policy["previewDays"])
            if cleaner.now - _job_timestamp(payload, job_path) < days * DAY:
                continue
            job_id = str(payload.get("jobId") or job_path.stem)
            cleaner.remove(job_path, f"过期的{family}终态任务")
            cleaner.remove(folder / f"{job_id}.log", f"过期的{family}任务日志")
            if family == "augment":
                cleaner.remove(cleaner.layout.augment_previews / job_id, "过期预览任务的资源")


def _clean_orphan_previews(cleaner: Cleaner, policy: dict[str, int]) -> None:
    if not cleaner.layout.augment_previews.is_dir():
        return
    for path in cleaner.layout.augment_previews.iterdir():
        if not path.is_dir():
            continue
        job_path = cleaner.layout.augment_jobs / f"{path.name}.json"
        if not job_path.is_file() and cleaner.expired(path, policy["tempDays"]):
            cleaner.remove(path, "无任务引用的预览资源")


def _clean_old_files(cleaner: Cleaner, root: Path, days: int, reason: str) -> None:
    for path in root.rglob("*"):
        if path.is_file() and cleaner.expired(path, days):
            cleaner.remove(path, reason)


def _report_references(layout: CacheLayout) -> set[str]:
    references: set[str] = set()
    for job_path in layout.qc_jobs.glob("*.json"):
        payload = _read_job(job_path) or {}
        value = payload.get("reportPath")
        if isinstance(value, str):
            references.add(str(Path(value).expanduser().resolve()))
    return references


def _clean_qc_reports(cleaner: Cleaner, policy: dict[str, int]) -> None:
    keep = policy["qcReportsPerDataset"]
    references = _report_references(cleaner.layout)
    if not cleaner.layout.qc_reports.is_dir():
        return
    for dataset_dir in cleaner.layout.qc_reports.iterdir():
        if not dataset_dir.is_dir():
            continue
        reports = sorted(dataset_dir.glob("*.qc.sqlite3"), key=_latest_mtime, reverse=True)
        for report in reports[keep:]:
            if str(report.resolve()) in references:
                continue
            cleaner.remove(report, f"超过每数据集最近 {keep} 份的 QC 报告")
            cleaner.remove(Path(str(report) + "-wal"), "已删除 QC 报告的 WAL")
            cleaner.remove(Path(str(report) + "-shm"), "已删除 QC 报告的共享内存文件")
        for auxiliary in dataset_dir.glob("*.qc.sqlite3-*"):
            base = Path(str(auxiliary).rsplit("-", 1)[0])
            if not base.exists() and cleaner.expired(auxiliary, policy["tempDays"]):
                cleaner.remove(auxiliary, "无主文件的 QC 辅助文件")


def _clean_stale_atomic_files(cleaner: Cleaner, policy: dict[str, int]) -> None:
    for root in (cleaner.layout.jobs, cleaner.layout.reusable):
        for path in root.rglob("*"):
            if not path.is_file() or not cleaner.expired(path, policy["tempDays"]):
                continue
            name = path.name
            if name.startswith(".") and (".tmp" in name or ".migrate-" in name):
                cleaner.remove(path, "崩溃遗留的原子写入临时文件")
            elif name.endswith(".part"):
                cleaner.remove(path, "崩溃遗留的部分文件")


def cleanup(
    mode: str = "auto",
    *,
    dry_run: bool = False,
    layout: CacheLayout | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """Clean by retention policy, all disposable caches, or the entire root."""
    if mode not in {"auto", "cache", "all"}:
        raise ValueError(f"未知清理模式：{mode}")
    layout = layout or default_layout()
    validate_layout(layout)
    if not dry_run:
        ensure_layout(layout)
    cleaner = Cleaner(layout, dry_run=dry_run, now=now if now is not None else time.time())
    policy = retention_policy()
    if mode == "all":
        # Delete only named managed trees.  Even a misconfigured cache root
        # must never turn `clean --all` into a recursive wipe of unrelated
        # files that happen to share that directory.
        for path in (
            layout.jobs,
            layout.previews,
            layout.reusable,
            layout.media,
            layout.reports,
            layout.root / "qc_jobs",
            layout.root / "qc",
        ):
            cleaner.remove(path, "手动清理全部 Embodit 缓存和任务记录")
    elif mode == "cache":
        for path, reason in (
            (layout.previews, "手动清理全部预览缓存"),
            (layout.reusable, "手动清理全部可复用计算缓存"),
            (layout.media, "手动清理全部播放媒体缓存"),
        ):
            cleaner.remove(path, reason)
    else:
        _clean_terminal_jobs(cleaner, policy)
        _clean_orphan_previews(cleaner, policy)
        _clean_old_files(cleaner, layout.media, policy["mediaDays"], "过期播放媒体缓存")
        _clean_old_files(cleaner, layout.sam_tracks, policy["samDays"], "过期 SAM3 分割缓存")
        _clean_qc_reports(cleaner, policy)
        _clean_stale_atomic_files(cleaner, policy)
    if not dry_run:
        ensure_layout(layout)
    return {
        "mode": mode,
        "dryRun": dry_run,
        "root": str(layout.root),
        "policy": policy,
        "removed": len(cleaner.actions),
        "reclaimedBytes": sum(item["bytes"] for item in cleaner.actions),
        "actions": cleaner.actions,
    }


def maintain() -> dict[str, Any]:
    migration = migrate_legacy()
    result = cleanup("auto")
    result["migration"] = migration
    return result


def _format_bytes(value: int) -> str:
    amount = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if amount < 1024 or unit == "TiB":
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{amount:.1f} TiB"


def print_summary(result: dict[str, Any]) -> None:
    migration = result.get("migration") or {}
    for item in migration.get("moved") or []:
        print(f"migrate  {item}")
    for item in result.get("actions") or []:
        prefix = "would remove" if result.get("dryRun") else "removed"
        print(f"{prefix:12} {_format_bytes(int(item['bytes'])):>10}  {item['path']}  ({item['reason']})")
    verb = "would reclaim" if result.get("dryRun") else "reclaimed"
    print(
        f"Embodit cache: {result['removed']} item(s), {verb} "
        f"{_format_bytes(int(result['reclaimedBytes']))}; root={result['root']}"
    )


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Maintain Embodit's unified cache directory")
    parser.add_argument("mode", choices=("maintain", "auto", "cache", "all"), nargs="?", default="auto")
    parser.add_argument("--dry-run", action="store_true", help="show what would be removed")
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.mode == "maintain":
        if args.dry_run:
            parser.error("maintain does not support --dry-run")
        result = maintain()
    else:
        if not args.dry_run:
            migrate_legacy()
        result = cleanup(args.mode, dry_run=args.dry_run)
    print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
