#!/usr/bin/env python3
"""Local web server for multi-format embodied dataset review."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import Cookie, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, HTMLResponse, Response
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from convert.pipeline import convert_dataset  # noqa: E402
from convert.registry import list_conversion_targets, pair_capability  # noqa: E402
from convert.jobs import (  # noqa: E402
    cancel_job as cancel_convert_job,
    create_job as create_convert_job,
    default_jobs_dir as default_convert_jobs_dir,
    delete_job as delete_convert_job,
    launch_detached_worker as launch_convert_worker,
    list_jobs as list_convert_jobs,
    read_job as read_convert_job,
    refresh_job_liveness as refresh_convert_liveness,
    write_job as write_convert_job,
)
from augment.algorithms import parse_prompts  # noqa: E402
from augment.capabilities import capabilities_payload, config_fingerprint  # noqa: E402
from augment.colors import options_payload as augment_options_payload  # noqa: E402
from augment.jobs import (  # noqa: E402
    cancel_job as cancel_augment_job,
    create_job as create_augment_job,
    default_jobs_dir as default_augment_jobs_dir,
    delete_job as delete_augment_job,
    launch_detached_worker as launch_augment_worker,
    list_jobs as list_augment_jobs,
    read_job as read_augment_job,
    refresh_job_liveness as refresh_augment_liveness,
    write_job as write_augment_job,
)
import settings  # noqa: E402

from augment.paths import DEFAULT_PREVIEW_DIR  # noqa: E402
from datasets.detect import dataset_brief, detect_format, list_entries  # noqa: E402
from datasets.export import (  # noqa: E402
    DECISION_PASS,
    episodes_for_export,
    normalize_decision,
)
from datasets.registry import open_dataset  # noqa: E402
from datasets.view import FORMAT_LABELS, SUPPORTED_FORMATS  # noqa: E402
from labels.store import (  # noqa: E402
    default_labels_path,
    delete_label,
    load_labels,
    now_iso,
    preset_tags,
    save_labels,
    upsert_label,
)
from qc.jobs import (  # noqa: E402
    cancel_job as cancel_qc_job,
    create_job as create_qc_job,
    default_jobs_dir as default_qc_jobs_dir,
    delete_job as delete_qc_job,
    launch_detached_worker as launch_qc_worker,
    list_jobs as list_qc_jobs,
    pause_job as pause_qc_job,
    read_job as read_qc_job,
    refresh_job_liveness as refresh_qc_liveness,
    resume_job as resume_qc_job,
    write_job as write_qc_job,
)
from qc.paths import find_report as find_qc_report  # noqa: E402
from qc.store import (  # noqa: E402
    episode_detail as qc_episode_detail,
    query_episodes as query_qc_episodes,
    report_csv as qc_report_csv,
    review_episode as review_qc_episode,
    review_finding as review_qc_finding,
    selected_episode_indices as qc_selected_episode_indices,
    summary as qc_summary,
)



class InspectRequest(BaseModel):
    dataset: str


class ProgressRequest(BaseModel):
    path: str
    dataset: str
    states: dict[str, str] = Field(default_factory=dict)
    quarantineReasons: dict[str, str] = Field(default_factory=dict)


class ProgressLoadRequest(BaseModel):
    path: str


class CreateRequest(BaseModel):
    dataset: str
    output: str
    episodes: list[int] | None = None
    states: dict[str, str] | None = None
    mediaMode: str = "hardlink"
    targetFormat: str | None = None
    includeReview: bool = False
    mapping: dict[str, Any] = Field(default_factory=dict)
    copyLabels: bool = True


class ConvertRequest(BaseModel):
    dataset: str
    output: str
    targetFormat: str
    episodes: list[int] | None = None
    mapping: dict[str, Any] = Field(default_factory=dict)


class AugmentRequest(BaseModel):
    dataset: str
    output: str | None = None
    mode: str = "batch"
    augType: str = "brightness"
    applyMode: str = "object_recolor"
    samPrompts: list[str] | str = Field(default_factory=list)
    colorMode: str = "random"
    colorName: str | None = None
    colorRgb: list[int] | None = None
    brightnessMode: str = "auto"
    brightnessGain: float | None = None
    brightnessGamma: float | None = None
    gpuId: int = 0
    episodes: list[int] | None = None
    sampleCount: int | None = None
    previewEpisode: int | None = None
    targetFormat: str | None = None
    cameraPolicy: str = "strict"
    previewJobId: str | None = None


class LabelsLoadRequest(BaseModel):
    dataset: str
    path: str | None = None


class LabelsSaveRequest(BaseModel):
    dataset: str
    path: str | None = None
    labels: list[dict[str, Any]]


class LabelUpsertRequest(BaseModel):
    dataset: str
    path: str | None = None
    label: dict[str, Any]


class QCScanRequest(BaseModel):
    dataset: str
    config: dict[str, Any] = Field(default_factory=dict)
    useCache: bool = True


class QCQueryRequest(BaseModel):
    filters: dict[str, Any] = Field(default_factory=dict)


class QCFindingReviewRequest(BaseModel):
    reviewStatus: str = "unreviewed"
    startS: float | None = None
    endS: float | None = None
    severity: str | None = None
    issueCode: str | None = None
    note: str = ""


class QCEpisodeReviewRequest(BaseModel):
    decision: str | None = None
    note: str = ""

def build_app(token: str, browse_root: Path, web_root: Path) -> FastAPI:
    app = FastAPI(title="Embodit · Embodied Intelligence Toolkit", docs_url=None, redoc_url=None)
    browse_root = existing_root(browse_root)
    images_root = web_root.parent / "images"
    jobs_dir = default_convert_jobs_dir()
    augment_jobs_dir = default_augment_jobs_dir()
    qc_jobs_dir = default_qc_jobs_dir()

    def authorize(
        query_token: str | None = Query(default=None, alias="token"),
        header_token: str | None = Header(default=None, alias="X-LeRobot-Token"),
        cookie_token: str | None = Cookie(default=None, alias="embodit_token"),
    ) -> None:
        supplied = header_token or query_token or cookie_token
        if supplied != token:
            raise HTTPException(status_code=401, detail="无效或缺失的访问令牌")

    def sandboxed(raw: str | Path, *, what: str = "路径") -> Path:
        """Resolve a client-supplied path; confine to browse_root only when
        EMBODIT_SANDBOX=1 is set (off by default so any directory can be used)."""
        resolved = Path(raw).expanduser().resolve()
        if settings.SANDBOX_PATHS and not is_inside(browse_root, resolved):
            raise HTTPException(status_code=403, detail=f"{what}超出允许的根目录：{browse_root}")
        return resolved

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> HTMLResponse:
        # First visit carries ?token=... which we exchange for an HttpOnly
        # cookie; the token is no longer embedded in the page or asset URLs.
        query_token = request.query_params.get("token")
        cookie_token = request.cookies.get("embodit_token")
        if query_token != token and cookie_token != token:
            raise HTTPException(status_code=401, detail="无效或缺失的访问令牌")
        page = (web_root / "index.html").read_text(encoding="utf-8")
        page = page.replace("__LEROBOT_TOKEN__", "")
        # Inline i18n so language packs load even if /i18n.js route is missing
        # (e.g. server started before that route existed). index.html is read fresh.
        i18n_path = web_root / "i18n.js"
        if i18n_path.is_file() and "<!--I18N_INLINE-->" in page:
            page = page.replace(
                "<!--I18N_INLINE-->",
                f"<script>\n{i18n_path.read_text(encoding='utf-8')}\n</script>",
            )
        response = HTMLResponse(page, headers={"Cache-Control": "no-store"})
        if query_token == token and cookie_token != token:
            response.set_cookie(
                "embodit_token",
                token,
                httponly=True,
                samesite="lax",
                max_age=30 * 24 * 3600,
            )
        return response

    @app.get("/app.js")
    def javascript() -> FileResponse:
        return FileResponse(web_root / "app.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/styles.css")
    def stylesheet() -> FileResponse:
        return FileResponse(web_root / "styles.css", media_type="text/css", headers={"Cache-Control": "no-store"})

    @app.get("/i18n.js")
    def i18n_javascript() -> FileResponse:
        return FileResponse(web_root / "i18n.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/utils.js")
    def utils_javascript() -> FileResponse:
        return FileResponse(web_root / "utils.js", media_type="text/javascript", headers={"Cache-Control": "no-store"})

    @app.get("/images/{name}")
    def image_asset(name: str) -> FileResponse:
        if "/" in name or "\\" in name or name.startswith("."):
            raise HTTPException(status_code=400, detail="非法资源名")
        target = (images_root / name).resolve()
        if not is_inside(images_root.resolve(), target) or not target.is_file():
            raise HTTPException(status_code=404, detail="资源不存在")
        media = "image/png" if target.suffix.lower() == ".png" else "application/octet-stream"
        return FileResponse(target, media_type=media, headers={"Cache-Control": "public, max-age=86400"})

    @app.get("/api/health", dependencies=[Depends(authorize)])
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "browseRoot": str(browse_root),
            "supportedFormats": list(SUPPORTED_FORMATS),
            "formatLabels": FORMAT_LABELS,
            "autoFilter": {"enabled": True, "status": "ready"},
        }

    @app.get("/api/list", dependencies=[Depends(authorize)])
    def list_directories(path: str | None = None) -> dict[str, Any]:
        requested = sandboxed(path, what="目录") if path else browse_root
        if not requested.is_dir():
            raise HTTPException(status_code=404, detail=f"目录不存在：{requested}")
        try:
            entries = list_entries(requested)
        except PermissionError as error:
            raise HTTPException(status_code=403, detail=f"没有权限读取：{requested}") from error
        parent = requested.parent if requested.parent != requested else None
        fmt = detect_format(requested)
        return {
            "path": str(requested),
            "parent": str(parent) if parent else None,
            "isDataset": fmt is not None,
            "format": fmt,
            "formatLabel": FORMAT_LABELS.get(fmt or "", ""),
            "brief": dataset_brief(requested, fmt),
            "entries": entries,
        }

    @app.post("/api/inspect", dependencies=[Depends(authorize)])
    async def inspect(request: InspectRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        try:
            adapter = open_dataset(dataset)
            view = await run_in_threadpool(adapter.inspect)
            return view.to_inspect_dict()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/timeseries", dependencies=[Depends(authorize)])
    async def timeseries(
        dataset: str,
        episode: int,
        keys: str | None = None,
        maxPoints: int | None = None,
    ) -> dict[str, Any]:
        try:
            adapter = open_dataset(sandboxed(dataset, what="数据集路径"))
            key_list = [item for item in (keys or "").split(",") if item] or None
            cap = int(maxPoints) if maxPoints else 0

            def _load() -> tuple[dict[str, Any], dict[str, int]]:
                import numpy as np

                arrays = adapter.get_timeseries(episode, key_list)
                series: dict[str, Any] = {}
                lengths: dict[str, int] = {}
                for key, value in arrays.items():
                    total = int(value.shape[0]) if value.ndim else 0
                    lengths[key] = total
                    if cap > 0 and total > cap:
                        # Uniform stride sample keeps the curve shape; the UI
                        # positions its cursor by time ratio, not row index.
                        idx = np.linspace(0, total - 1, cap).round().astype(int)
                        value = value[idx]
                    series[key] = value.tolist()
                return series, lengths

            data, lengths = await run_in_threadpool(_load)
            return {"episode": episode, "series": data, "lengths": lengths}
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/video", dependencies=[Depends(authorize)])
    def video(dataset: str, relative: str) -> FileResponse:
        dataset_root = sandboxed(dataset, what="数据集路径")
        if dataset_root.is_file():
            dataset_root = dataset_root.parent
        relative_path = Path(relative)
        if relative_path.is_absolute():
            raise HTTPException(status_code=400, detail="视频路径必须是相对路径")
        video_path = (dataset_root / relative_path).resolve()
        if not is_inside(dataset_root, video_path) or not video_path.is_file():
            raise HTTPException(status_code=404, detail="视频文件不存在")
        response = FileResponse(
            video_path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, max-age=3600"},
        )
        response.chunk_size = 1024 * 1024
        return response

    @app.get("/api/mcap/video", dependencies=[Depends(authorize)])
    async def mcap_video(dataset: str, episode: int, topic: str) -> FileResponse:
        """Materialize an MCAP CompressedImage topic into a cached MP4 for playback."""
        try:
            adapter = open_dataset(sandboxed(dataset, what="数据集路径"))
            if getattr(adapter, "format_id", None) != "mcap":
                raise ValueError("仅 MCAP 数据集支持 topic 视频预览")
            path = await run_in_threadpool(adapter.materialize_topic_video, episode, topic)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        response = FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, max-age=86400"},
        )
        response.chunk_size = 1024 * 1024
        return response

    @app.get("/api/hdf5/video", dependencies=[Depends(authorize)])
    async def hdf5_video(dataset: str, episode: int, camera: str) -> FileResponse:
        """Materialize in-HDF5 image frames into a cached MP4 for playback."""
        try:
            adapter = open_dataset(sandboxed(dataset, what="数据集路径"))
            if getattr(adapter, "format_id", None) != "hdf5":
                raise ValueError("仅 HDF5 数据集支持 frames 视频预览")
            path = await run_in_threadpool(adapter.materialize_camera_video, episode, camera)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        response = FileResponse(
            path,
            media_type="video/mp4",
            headers={"Cache-Control": "private, max-age=86400"},
        )
        response.chunk_size = 1024 * 1024
        return response

    @app.post("/api/progress/save", dependencies=[Depends(authorize)])
    def save_progress(request: ProgressRequest) -> dict[str, Any]:
        target = sandboxed(request.path, what="进度文件路径")
        target.parent.mkdir(parents=True, exist_ok=True)
        normalized = {key: normalize_decision(value) for key, value in request.states.items()}
        quarantine_reasons = {
            key: str(value).strip()
            for key, value in request.quarantineReasons.items()
            if normalized.get(key) == "quarantine" and str(value).strip()
        }
        document = {
            "version": 3,
            "dataset": request.dataset,
            "updatedAt": now_iso(),
            "updatedBy": os.environ.get("USER", "unknown"),
            "states": normalized,
            "quarantineReasons": quarantine_reasons,
        }
        temporary = target.with_name(f".{target.name}.tmp-{os.getpid()}")
        temporary.write_text(json.dumps(document, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(temporary, target)
        return {"path": str(target), "states": normalized, "quarantineReasons": quarantine_reasons}

    @app.post("/api/progress/load", dependencies=[Depends(authorize)])
    def load_progress(request: ProgressLoadRequest) -> dict[str, Any]:
        target = sandboxed(request.path, what="进度文件路径")
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"进度文件不存在：{target}")
        document = json.loads(target.read_text(encoding="utf-8"))
        states = document.get("states") or {}
        document["states"] = {key: normalize_decision(value) for key, value in states.items()}
        reasons = document.get("quarantineReasons") or {}
        document["quarantineReasons"] = {
            key: str(value).strip()
            for key, value in reasons.items()
            if document["states"].get(key) == "quarantine" and str(value).strip()
        }
        return document

    @app.post("/api/create", dependencies=[Depends(authorize)])
    async def create(request: CreateRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        output_path = sandboxed(request.output, what="输出路径")
        if request.mediaMode not in {"hardlink", "copy"}:
            raise HTTPException(status_code=400, detail="mediaMode 只能是 hardlink 或 copy")
        if request.episodes is not None:
            episodes = sorted(set(request.episodes))
        elif request.states is not None:
            episodes = episodes_for_export(request.states, include_review=request.includeReview)
        else:
            raise HTTPException(status_code=400, detail="需要 episodes 或 states")
        if not episodes:
            raise HTTPException(status_code=400, detail="没有可导出的 episode（需要 pass 决策）")
        labels_path = default_labels_path(dataset) if request.copyLabels else None
        try:
            # Export runs as a detached background job (same infra as convert)
            # instead of blocking this request for potentially many minutes.
            job = create_convert_job(
                dataset=dataset,
                output=output_path,
                target_format=request.targetFormat or "",
                mapping=request.mapping,
                episodes=episodes,
                jobs_dir=jobs_dir,
                kind="export",
                extra={
                    "mediaMode": request.mediaMode,
                    "labelsPath": str(labels_path) if labels_path and labels_path.is_file() else None,
                },
            )
            job = launch_convert_worker(job["jobId"], jobs_dir=jobs_dir)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            **job,
            "detached": True,
            "episodeCount": len(episodes),
            "hint": "导出已在独立后台进程运行，可在转换任务面板查看进度。",
        }

    @app.get("/api/convert/targets", dependencies=[Depends(authorize)])
    def convert_targets(sourceFormat: str | None = None) -> dict[str, Any]:
        source = sourceFormat or ""
        targets = list_conversion_targets(source) if source else list(SUPPORTED_FORMATS)
        rows = []
        for fmt in targets:
            capability = pair_capability(source, fmt) if source else None
            rows.append(
                {
                    "id": fmt,
                    "label": FORMAT_LABELS.get(fmt, fmt),
                    "fidelity": (capability or {}).get("fidelity"),
                    "notes": (capability or {}).get("notes") or [],
                }
            )
        return {"formats": rows}

    @app.post("/api/convert/start", dependencies=[Depends(authorize)])
    def start_convert(request: ConvertRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        output = sandboxed(request.output, what="输出路径")
        if request.targetFormat not in SUPPORTED_FORMATS:
            raise HTTPException(status_code=400, detail=f"不支持的目标格式：{request.targetFormat}")
        if not dataset.exists():
            raise HTTPException(status_code=404, detail=f"源路径不存在：{dataset}")
        try:
            job = create_convert_job(
                dataset=dataset,
                output=output,
                target_format=request.targetFormat,
                mapping=request.mapping,
                episodes=request.episodes,
                jobs_dir=jobs_dir,
            )
            job = launch_convert_worker(job["jobId"], jobs_dir=jobs_dir)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            **job,
            "detached": True,
            "hint": "任务已在独立后台进程运行，关闭网页或终端不会中断（需服务机保持开机）。",
        }

    @app.get("/api/convert/status/{job_id}", dependencies=[Depends(authorize)])
    def convert_status(job_id: str) -> dict[str, Any]:
        job = read_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        refreshed = refresh_convert_liveness(job)
        if refreshed.get("status") != job.get("status"):
            write_convert_job(jobs_dir, refreshed)
        return refreshed

    @app.get("/api/convert/jobs", dependencies=[Depends(authorize)])
    def convert_jobs_list(limit: int = 30) -> dict[str, Any]:
        rows = []
        for job in list_convert_jobs(jobs_dir, limit=limit):
            refreshed = refresh_convert_liveness(job)
            if refreshed.get("status") != job.get("status"):
                write_convert_job(jobs_dir, refreshed)
            rows.append(refreshed)
        return {"jobs": rows, "jobsDir": str(jobs_dir)}

    @app.post("/api/convert/jobs/{job_id}/dismiss", dependencies=[Depends(authorize)])
    def convert_job_dismiss(job_id: str) -> dict[str, Any]:
        job = read_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        if job.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=400, detail="进行中的任务不能直接清除，请先取消或等待结束")
        deleted = delete_convert_job(jobs_dir, job_id)
        return {"ok": deleted, "jobId": job_id}

    @app.post("/api/convert/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def convert_job_cancel(job_id: str) -> dict[str, Any]:
        job = read_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        return cancel_convert_job(jobs_dir, job_id)

    @app.get("/api/augment/options", dependencies=[Depends(authorize)])
    def augment_options() -> dict[str, Any]:
        return {**augment_options_payload(), "capabilities": capabilities_payload()}

    def _start_augment_job(request: AugmentRequest, mode: str) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        if not dataset.exists():
            raise HTTPException(status_code=404, detail=f"源路径不存在：{dataset}")
        aug_type = request.augType if request.augType in {"brightness", "color"} else "brightness"
        capabilities = capabilities_payload() if aug_type == "color" else None
        if capabilities is not None and not capabilities[aug_type]["available"]:
            raise HTTPException(
                status_code=400,
                detail=capabilities[aug_type].get("reason") or f"{aug_type} 增强不可用",
            )
        prompts = parse_prompts(request.samPrompts)
        if aug_type == "color" and not prompts:
            raise HTTPException(status_code=400, detail="颜色增强需要填写 SAM3 查询词")
        apply_mode = (
            request.applyMode
            if request.applyMode in {"object_recolor", "background_replace"}
            else "object_recolor"
        )
        color_mode = request.colorMode if request.colorMode in {"random", "fixed"} else "random"
        if color_mode == "fixed" and request.colorRgb is not None:
            if len(request.colorRgb) != 3 or any(value < 0 or value > 255 for value in request.colorRgb):
                raise HTTPException(status_code=400, detail="colorRgb 必须是 0–255 范围内的三个整数")
        if request.gpuId < 0:
            raise HTTPException(status_code=400, detail="gpuId 不能为负数")
        if capabilities is not None and request.gpuId >= capabilities["color"].get("gpuCount", 0):
            raise HTTPException(status_code=400, detail=f"GPU ID 超出范围：{request.gpuId}")
        if request.sampleCount is not None and request.sampleCount < 1:
            raise HTTPException(status_code=400, detail="sampleCount 必须是正整数")
        if request.episodes is not None and not request.episodes:
            raise HTTPException(status_code=400, detail="episodes 不能为空列表")
        if mode == "batch" and not request.output:
            raise HTTPException(status_code=400, detail="批量增强需要输出路径")
        output_path = sandboxed(request.output, what="输出路径") if request.output else None
        if output_path is not None:
            if output_path.exists():
                raise HTTPException(status_code=400, detail=f"输出路径已经存在：{output_path}")
            if output_path == dataset or (dataset.is_dir() and is_inside(dataset, output_path)):
                raise HTTPException(status_code=400, detail="输出路径不能等于或位于源数据集内部")
        output = str(output_path) if output_path else None
        try:
            config = {
                "dataset": str(dataset),
                "output": output,
                "mode": mode,
                "augType": aug_type,
                "applyMode": apply_mode,
                "samPrompts": prompts,
                "colorMode": color_mode,
                "colorName": request.colorName,
                "colorRgb": request.colorRgb,
                "brightnessMode": (
                    request.brightnessMode
                    if request.brightnessMode in {"auto", "manual"}
                    else "auto"
                ),
                "brightnessGain": request.brightnessGain,
                "brightnessGamma": request.brightnessGamma,
                "gpuId": int(request.gpuId or 0),
                "episodes": request.episodes,
                "sampleCount": request.sampleCount,
                "previewEpisode": request.previewEpisode,
                "targetFormat": request.targetFormat,
                "cameraPolicy": request.cameraPolicy if request.cameraPolicy in {"strict", "partial"} else "strict",
                "previewJobId": request.previewJobId,
            }
            if mode == "batch":
                if not request.previewJobId:
                    raise HTTPException(status_code=400, detail="批量增强必须引用一次成功的预览任务")
                preview_job = read_augment_job(augment_jobs_dir, request.previewJobId)
                if not preview_job or preview_job.get("mode") != "preview":
                    raise HTTPException(status_code=400, detail="预览任务不存在")
                if preview_job.get("status") != "completed":
                    raise HTTPException(status_code=400, detail="预览任务尚未成功完成")
                if preview_job.get("configFingerprint") != config_fingerprint(config):
                    raise HTTPException(status_code=400, detail="增强参数已在预览后改变，请重新生成预览")
            job = create_augment_job(config=config, jobs_dir=augment_jobs_dir)
            if mode == "preview":
                preview_dir = DEFAULT_PREVIEW_DIR / job["jobId"]
                preview_dir.mkdir(parents=True, exist_ok=True)
                job["previewDir"] = str(preview_dir)
                write_augment_job(augment_jobs_dir, job)
            job = launch_augment_worker(job["jobId"], jobs_dir=augment_jobs_dir)
        except HTTPException:
            raise
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            **job,
            "detached": True,
            "hint": "任务已在独立后台进程运行，关闭网页不会中断。",
        }

    @app.post("/api/augment/preview", dependencies=[Depends(authorize)])
    def augment_preview(request: AugmentRequest) -> dict[str, Any]:
        return _start_augment_job(request, mode="preview")

    @app.post("/api/augment/start", dependencies=[Depends(authorize)])
    def augment_start(request: AugmentRequest) -> dict[str, Any]:
        return _start_augment_job(request, mode="batch")

    @app.get("/api/augment/status/{job_id}", dependencies=[Depends(authorize)])
    def augment_status(job_id: str) -> dict[str, Any]:
        job = read_augment_job(augment_jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="增强任务不存在")
        refreshed = refresh_augment_liveness(job)
        if refreshed.get("status") != job.get("status"):
            write_augment_job(augment_jobs_dir, refreshed)
        return refreshed

    @app.get("/api/augment/jobs", dependencies=[Depends(authorize)])
    def augment_jobs_list(limit: int = 30) -> dict[str, Any]:
        rows = []
        for job in list_augment_jobs(augment_jobs_dir, limit=limit):
            refreshed = refresh_augment_liveness(job)
            if refreshed.get("status") != job.get("status"):
                write_augment_job(augment_jobs_dir, refreshed)
            rows.append(refreshed)
        return {"jobs": rows, "jobsDir": str(augment_jobs_dir)}

    @app.post("/api/augment/jobs/{job_id}/dismiss", dependencies=[Depends(authorize)])
    def augment_job_dismiss(job_id: str) -> dict[str, Any]:
        job = read_augment_job(augment_jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="增强任务不存在")
        if job.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=400, detail="进行中的任务不能直接清除，请先取消或等待结束")
        deleted = delete_augment_job(augment_jobs_dir, job_id)
        return {"ok": deleted, "jobId": job_id}

    @app.post("/api/augment/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def augment_job_cancel(job_id: str) -> dict[str, Any]:
        job = read_augment_job(augment_jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="增强任务不存在")
        return cancel_augment_job(augment_jobs_dir, job_id)

    @app.get("/api/augment/preview-asset/{job_id}/{asset_path:path}", dependencies=[Depends(authorize)])
    def augment_preview_asset(job_id: str, asset_path: str) -> FileResponse:
        job = read_augment_job(augment_jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="增强任务不存在")
        root = Path(job.get("previewDir") or (DEFAULT_PREVIEW_DIR / job_id)).resolve()
        target = (root / asset_path).resolve()
        if not is_inside(root, target) or not target.is_file():
            raise HTTPException(status_code=404, detail="预览文件不存在")
        suffix = target.suffix.lower()
        if suffix in {".mp4", ".webm"}:
            media = "video/mp4" if suffix == ".mp4" else "video/webm"
        elif suffix in {".jpg", ".jpeg"}:
            media = "image/jpeg"
        elif suffix == ".png":
            media = "image/png"
        else:
            media = "application/octet-stream"
        return FileResponse(target, media_type=media, headers={"Cache-Control": "no-store"})

    @app.post("/api/labels/load", dependencies=[Depends(authorize)])
    def labels_load(request: LabelsLoadRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        path = sandboxed(request.path, what="标签文件路径") if request.path else default_labels_path(dataset)
        return {"path": str(path), "labels": load_labels(path), "presets": preset_tags()}

    @app.post("/api/labels/save", dependencies=[Depends(authorize)])
    def labels_save(request: LabelsSaveRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        path = sandboxed(request.path, what="标签文件路径") if request.path else default_labels_path(dataset)
        try:
            save_labels(path, request.labels)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"path": str(path), "count": len(request.labels)}

    @app.post("/api/labels/upsert", dependencies=[Depends(authorize)])
    def labels_upsert(request: LabelUpsertRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        path = sandboxed(request.path, what="标签文件路径") if request.path else default_labels_path(dataset)
        label = dict(request.label)
        label.setdefault("updated_at", now_iso())
        label.setdefault("updated_by", os.environ.get("USER", "user"))
        try:
            labels = upsert_label(path, label)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"path": str(path), "labels": labels}

    @app.post("/api/labels/delete", dependencies=[Depends(authorize)])
    def labels_delete(request: LabelUpsertRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        path = sandboxed(request.path, what="标签文件路径") if request.path else default_labels_path(dataset)
        try:
            labels = delete_label(path, dict(request.label))
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"path": str(path), "labels": labels}

    @app.get("/api/auto-filter/status", dependencies=[Depends(authorize)])
    def auto_filter_status() -> dict[str, Any]:
        return {
            "enabled": True,
            "status": "ready",
            "message": "自动质检可用：完整性、动作、视频与夹爪规则已启用。",
        }

    def resolve_qc_report(scan_id: str) -> Path:
        job = read_qc_job(qc_jobs_dir, scan_id)
        raw = job.get("reportPath") if job else None
        path = Path(raw).expanduser().resolve() if raw else find_qc_report(scan_id)
        if path is None or not path.is_file():
            raise HTTPException(status_code=404, detail="QC 报告不存在或尚未生成")
        if not is_inside(settings.QC_CACHE_DIR.resolve(), path):
            raise HTTPException(status_code=403, detail="QC 报告路径非法")
        return path

    @app.post("/api/qc/scans", dependencies=[Depends(authorize)])
    def qc_start(request: QCScanRequest) -> dict[str, Any]:
        dataset = sandboxed(request.dataset, what="数据集路径")
        try:
            job = create_qc_job(
                dataset=dataset,
                config=request.config,
                use_cache=request.useCache,
                jobs_dir=qc_jobs_dir,
            )
            return launch_qc_worker(job["jobId"], jobs_dir=qc_jobs_dir)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/qc/scans", dependencies=[Depends(authorize)])
    def qc_scans(dataset: str | None = None) -> dict[str, Any]:
        target = str(sandboxed(dataset, what="数据集路径")) if dataset else None
        rows = []
        for job in list_qc_jobs(qc_jobs_dir, limit=100):
            patched = refresh_qc_liveness(job)
            if patched != job:
                write_qc_job(qc_jobs_dir, patched)
            if target is None or patched.get("dataset") == target:
                rows.append(patched)
        return {"jobs": rows}

    @app.get("/api/qc/scans/{scan_id}/status", dependencies=[Depends(authorize)])
    def qc_scan_status(scan_id: str) -> dict[str, Any]:
        job = read_qc_job(qc_jobs_dir, scan_id)
        if job is None:
            raise HTTPException(status_code=404, detail="QC 任务不存在")
        patched = refresh_qc_liveness(job)
        if patched != job:
            write_qc_job(qc_jobs_dir, patched)
        return patched

    @app.post("/api/qc/scans/{scan_id}/pause", dependencies=[Depends(authorize)])
    def qc_pause(scan_id: str) -> dict[str, Any]:
        try:
            return pause_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/resume", dependencies=[Depends(authorize)])
    def qc_resume(scan_id: str) -> dict[str, Any]:
        try:
            return resume_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/cancel", dependencies=[Depends(authorize)])
    def qc_cancel(scan_id: str) -> dict[str, Any]:
        try:
            return cancel_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/dismiss", dependencies=[Depends(authorize)])
    def qc_dismiss(scan_id: str) -> dict[str, Any]:
        return {"deleted": delete_qc_job(qc_jobs_dir, scan_id), "jobId": scan_id}

    @app.get("/api/qc/scans/{scan_id}/summary", dependencies=[Depends(authorize)])
    def qc_report_summary(scan_id: str) -> dict[str, Any]:
        return qc_summary(resolve_qc_report(scan_id))

    @app.post("/api/qc/scans/{scan_id}/episodes/query", dependencies=[Depends(authorize)])
    def qc_report_episodes(scan_id: str, request: QCQueryRequest) -> dict[str, Any]:
        try:
            return query_qc_episodes(resolve_qc_report(scan_id), request.filters)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get(
        "/api/qc/scans/{scan_id}/episodes/{episode_index}",
        dependencies=[Depends(authorize)],
    )
    def qc_report_episode(scan_id: str, episode_index: int) -> dict[str, Any]:
        try:
            return qc_episode_detail(resolve_qc_report(scan_id), episode_index)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"Episode 不存在：{episode_index}") from error

    @app.post(
        "/api/qc/scans/{scan_id}/findings/{finding_id}/review",
        dependencies=[Depends(authorize)],
    )
    def qc_finding_review(
        scan_id: str,
        finding_id: str,
        request: QCFindingReviewRequest,
    ) -> dict[str, Any]:
        try:
            return review_qc_finding(
                resolve_qc_report(scan_id),
                finding_id,
                request.model_dump(),
                os.environ.get("USER", "user"),
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Finding 不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/qc/scans/{scan_id}/episodes/{episode_index}/review",
        dependencies=[Depends(authorize)],
    )
    def qc_episode_review(
        scan_id: str,
        episode_index: int,
        request: QCEpisodeReviewRequest,
    ) -> dict[str, Any]:
        try:
            return review_qc_episode(
                resolve_qc_report(scan_id),
                episode_index,
                request.decision,
                request.note,
                os.environ.get("USER", "user"),
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail="Episode 不存在") from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/qc/scans/{scan_id}/selection/preview",
        dependencies=[Depends(authorize)],
    )
    def qc_selection_preview(scan_id: str, request: QCQueryRequest) -> dict[str, Any]:
        return qc_selected_episode_indices(resolve_qc_report(scan_id), request.filters)

    @app.get("/api/qc/scans/{scan_id}/export-report", dependencies=[Depends(authorize)])
    def qc_export_report(scan_id: str, kind: str = "episodes") -> Response:
        if kind not in {"episodes", "findings"}:
            raise HTTPException(status_code=400, detail="kind 只能是 episodes 或 findings")
        content = qc_report_csv(resolve_qc_report(scan_id), kind)
        return Response(
            content,
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="qc-{scan_id[:8]}-{kind}.csv"'},
        )

    return app


def existing_root(path: Path) -> Path:
    resolved = path.expanduser().resolve()
    if resolved.is_dir():
        return resolved
    return Path.home().resolve()


def is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def default_review_path(dataset: Path) -> Path:
    dataset = dataset.expanduser().resolve()
    if dataset.is_file():
        return dataset.with_name(dataset.name + ".review.json")
    return dataset.with_name(dataset.name + ".review.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--browse-root", type=Path, default=Path.cwd())
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    # Migrate old cache folders and apply bounded retention once per service
    # start. Maintenance failures should not make datasets unavailable.
    try:
        from cache_manager import cleanup as cleanup_cache
        from cache_manager import maintain

        maintenance = maintain()
        if maintenance.get("removed") or (maintenance.get("migration") or {}).get("moved"):
            print(
                "cache maintenance:",
                f"removed={maintenance.get('removed', 0)}",
                f"bytes={maintenance.get('reclaimedBytes', 0)}",
                file=sys.stderr,
            )

        try:
            maintenance_hours = float(os.environ.get("EMBODIT_MAINTENANCE_INTERVAL_HOURS", "24"))
        except ValueError:
            maintenance_hours = 24.0
        if maintenance_hours > 0:
            interval_seconds = max(3600.0, maintenance_hours * 3600.0)

            def _periodic_cache_maintenance() -> None:
                while True:
                    threading.Event().wait(interval_seconds)
                    try:
                        cleanup_cache("auto")
                    except Exception as periodic_error:  # noqa: BLE001
                        print(
                            "cache maintenance warning:",
                            f"{type(periodic_error).__name__}: {periodic_error}",
                            file=sys.stderr,
                        )

            threading.Thread(
                target=_periodic_cache_maintenance,
                name="embodit-cache-maintenance",
                daemon=True,
            ).start()
    except Exception as error:  # noqa: BLE001
        print(f"cache maintenance warning: {type(error).__name__}: {error}", file=sys.stderr)
    web_root = Path(__file__).resolve().parent.parent / "web"
    app = build_app(args.token, args.browse_root, web_root)
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning", access_log=False)


if __name__ == "__main__":
    main()
