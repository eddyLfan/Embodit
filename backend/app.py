#!/usr/bin/env python3
"""Local web server for multi-format embodied dataset review."""

from __future__ import annotations

import argparse
import json
import os
import sys
import threading
from contextlib import asynccontextmanager
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

MODEL_PROVIDER_CATALOG = [
    {
        "id": "openpi",
        "name": "OpenPI",
        "checkpointOnly": True,
        "source": "https://github.com/Physical-Intelligence/openpi",
        "path": "third_party/models/openpi",
        "revision": "15a9616a00943ada6c20a0f158e3adb39df2ccac",
        "license": "Apache-2.0 (Gemma components may have additional terms)",
    },
    {
        "id": "lerobot",
        "name": "LeRobot",
        "checkpointOnly": True,
        "source": "https://github.com/huggingface/lerobot",
        "path": "third_party/models/lerobot",
        "revision": "f66e5128ecb2456e8c54a63d15404fa59c16aebc",
        "license": "Apache-2.0",
    },
    {
        "id": "starvla",
        "name": "StarVLA",
        "checkpointOnly": True,
        "source": "https://github.com/starVLA/starVLA",
        "path": "third_party/models/starvla",
        "revision": "312fac890ab75b7651d2bc4f8f8c8dbb5e055184",
        "license": "MIT",
    },
]

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
from review_config import review_config_payload  # noqa: E402

from augment.paths import DEFAULT_PREVIEW_DIR  # noqa: E402
from datasets.detect import dataset_brief, detect_format, list_entries  # noqa: E402
from datasets.export import (  # noqa: E402
    DECISION_PASS,
    episodes_for_export,
    normalize_decision,
)
from datasets.registry import open_dataset  # noqa: E402
from datasets.view import FORMAT_LABELS, SUPPORTED_FORMATS  # noqa: E402
from deploy.orchestrator import OrchestrationRegistry  # noqa: E402
from deploy.recipe import (  # noqa: E402
    compose_recipe as compose_deployment_recipe,
    parse_deployment_config,
    parse_recipe as parse_deployment_recipe,
    redact_recipe,
    split_recipe as split_deployment_recipe,
)
from deploy.store import DeploymentConfigStore, RecipeStore  # noqa: E402
from deploy.transport import RecipeSshRunner, require_remote_ok  # noqa: E402
from merge.pipeline import preflight_merge  # noqa: E402
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


class MergeRequest(BaseModel):
    sources: list[str]
    output: str | None = None
    mediaMode: str = "hardlink"
    copyLabels: bool = True


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


class DeploymentRecipeRequest(BaseModel):
    recipe: dict[str, Any]


class DeploymentConfigRequest(BaseModel):
    config: dict[str, Any]


class DeploymentComposeRequest(BaseModel):
    robot: dict[str, Any]
    model: dict[str, Any]
    deployment_id: str | None = None
    name: str | None = None
    runtime: dict[str, Any] | None = None


class DeploymentConfirmationRequest(BaseModel):
    confirmation: str


class DeploymentEmergencyStopRequest(BaseModel):
    reason: str = Field(default="网页急停", max_length=500)


class DeploymentOrchestrationStartRequest(BaseModel):
    recipe: dict[str, Any]
    mode: str | None = None


class DeploymentDryRunRequest(BaseModel):
    taskPrompt: str = Field(min_length=1, max_length=2000)


class DeploymentPoseRecordRequest(BaseModel):
    name: str | None = Field(default=None, max_length=100)


class DeploymentPoseMoveRequest(BaseModel):
    durationS: float = Field(default=3.0, gt=0, le=60)


class DeploymentSchedulerRequest(BaseModel):
    mode: str
    actionSteps: int = Field(ge=1, le=10_000)
    requestAfterSteps: int | str = "auto"
    latencyMarginMs: float = Field(default=30, ge=0, le=10_000)


class DeploymentOrchestrationLogsRequest(BaseModel):
    component: str
    lines: int = Field(default=100, ge=1, le=1000)

def build_app(token: str, browse_root: Path, web_root: Path) -> FastAPI:
    browse_root = existing_root(browse_root)
    images_root = web_root.parent / "images"
    jobs_dir = default_convert_jobs_dir()
    augment_jobs_dir = default_augment_jobs_dir()
    qc_jobs_dir = default_qc_jobs_dir()
    deploy_root = settings.CACHE_DIR / "deploy"
    deployment_recipes = RecipeStore(deploy_root / "recipes")
    deployment_configs = {
        kind: DeploymentConfigStore(
            deploy_root / "configs",
            kind,
            discovery_roots=[settings.CONFIG_DIR / "local"],
        )
        for kind in ("robot", "model")
    }
    deployment_orchestrations = OrchestrationRegistry(deploy_root / "orchestrations")

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        deployment_orchestrations.stop_all()

    app = FastAPI(
        title="Embodit · Embodied Intelligence Toolkit",
        docs_url=None,
        redoc_url=None,
        lifespan=lifespan,
    )

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
            "review": review_config_payload(settings.REVIEW_CONFIG_PATH),
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

    @app.post("/api/merge/preflight", dependencies=[Depends(authorize)])
    def merge_preflight(request: MergeRequest) -> dict[str, Any]:
        if len(request.sources) < 2:
            raise HTTPException(status_code=400, detail="至少需要两个源数据集")
        sources = [sandboxed(path, what="源数据集路径") for path in request.sources]
        try:
            return preflight_merge(sources)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/merge/start", dependencies=[Depends(authorize)])
    def start_merge(request: MergeRequest) -> dict[str, Any]:
        if len(request.sources) < 2:
            raise HTTPException(status_code=400, detail="至少需要两个源数据集")
        if not request.output:
            raise HTTPException(status_code=400, detail="需要输出路径")
        if request.mediaMode not in {"hardlink", "copy"}:
            raise HTTPException(status_code=400, detail="mediaMode 只能是 hardlink 或 copy")
        sources = [sandboxed(path, what="源数据集路径") for path in request.sources]
        output = sandboxed(request.output, what="输出路径")
        try:
            preflight = preflight_merge(sources)
            if not preflight["compatible"]:
                messages = "；".join(item["message"] for item in preflight["conflicts"][:8])
                raise ValueError(f"数据集不兼容：{messages}")
            job = create_convert_job(
                dataset=sources[0],
                output=output,
                target_format=str(preflight["format"]),
                jobs_dir=jobs_dir,
                kind="merge",
                extra={
                    "sources": [str(path) for path in sources],
                    "mediaMode": request.mediaMode,
                    "copyLabels": request.copyLabels,
                    "sourceCount": len(sources),
                },
            )
            job = launch_convert_worker(job["jobId"], jobs_dir=jobs_dir)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            **job,
            "detached": True,
            "episodeCount": int(preflight["totalEpisodes"]),
            "hint": "合并已在独立后台进程运行，可在任务面板查看进度。",
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

    @app.post("/api/deploy/recipes/validate", dependencies=[Depends(authorize)])
    def validate_deployment_recipe(request: DeploymentRecipeRequest) -> dict[str, Any]:
        try:
            recipe = parse_deployment_recipe(request.recipe)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        payload = redact_recipe(recipe.model_dump(mode="json"))
        return {"valid": True, "recipe": payload, "version": 2}

    @app.post("/api/deploy/configs/validate", dependencies=[Depends(authorize)])
    def validate_deployment_config(request: DeploymentConfigRequest) -> dict[str, Any]:
        try:
            config = parse_deployment_config(request.config)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "valid": True,
            "kind": config.kind,
            "config": redact_recipe(config.model_dump(mode="json")),
            "version": config.version,
        }

    @app.post("/api/deploy/compose", dependencies=[Depends(authorize)])
    def compose_deployment(request: DeploymentComposeRequest) -> dict[str, Any]:
        try:
            recipe = compose_deployment_recipe(
                request.robot,
                request.model,
                deployment_id=request.deployment_id,
                name=request.name,
                runtime=request.runtime,
            )
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "valid": True,
            "recipe": recipe.model_dump(mode="json"),
            "robotConfigId": request.robot.get("config_id"),
            "modelConfigId": request.model.get("config_id"),
            "version": 2,
        }

    @app.post("/api/deploy/recipes/split", dependencies=[Depends(authorize)])
    def split_deployment(request: DeploymentRecipeRequest) -> dict[str, Any]:
        try:
            robot, model = split_deployment_recipe(request.recipe)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {
            "robot": robot.model_dump(mode="json"),
            "model": model.model_dump(mode="json"),
        }

    @app.get("/api/deploy/configs/{kind}", dependencies=[Depends(authorize)])
    def list_deployment_configs(kind: str) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        configs = store.list()
        return {"kind": kind, "configs": configs, "count": len(configs)}

    @app.post("/api/deploy/configs/{kind}", dependencies=[Depends(authorize)])
    def save_deployment_config(kind: str, request: DeploymentConfigRequest) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        try:
            return {"saved": True, "config": store.save(request.config)}
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/configs/{kind}/{config_id}", dependencies=[Depends(authorize)])
    def get_deployment_config(kind: str, config_id: str) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        try:
            return {"config": store.get(config_id), "kind": kind, "version": 1}
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/deploy/configs/{kind}/{config_id}", dependencies=[Depends(authorize)])
    def delete_deployment_config(kind: str, config_id: str) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        try:
            return {"deleted": store.delete(config_id), "kind": kind, "configId": config_id}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/recipes", dependencies=[Depends(authorize)])
    def list_deployment_recipes() -> dict[str, Any]:
        recipes = deployment_recipes.list()
        return {"recipes": recipes, "count": len(recipes)}

    @app.post("/api/deploy/recipes", dependencies=[Depends(authorize)])
    def save_deployment_recipe(request: DeploymentRecipeRequest) -> dict[str, Any]:
        try:
            return {"saved": True, "recipe": deployment_recipes.save(request.recipe)}
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/recipes/{recipe_id}", dependencies=[Depends(authorize)])
    def get_deployment_recipe(recipe_id: str) -> dict[str, Any]:
        try:
            return {"recipe": deployment_recipes.get(recipe_id), "version": 2}
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete("/api/deploy/recipes/{recipe_id}", dependencies=[Depends(authorize)])
    def delete_deployment_recipe(recipe_id: str) -> dict[str, Any]:
        try:
            deleted = deployment_recipes.delete(recipe_id)
            return {"deleted": deleted, "deploymentId": recipe_id}
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/capabilities", dependencies=[Depends(authorize)])
    def deployment_capabilities() -> dict[str, Any]:
        return {
            "recipeVersion": 2,
            "componentConfigVersion": 1,
            "componentConfigKinds": ["robot", "model"],
            "modelProviders": ["python", "openpi", "lerobot", "starvla", "external"],
            "checkpointModelProviders": [item["id"] for item in MODEL_PROVIDER_CATALOG],
            "robotClients": ["ros2_standard", "python_adapter", "custom"],
            "features": {
                "recipeOrchestration": True,
                "independentRobotModelConfigs": True,
                "composableDeployment": True,
                "managedSshTunnel": True,
                "remoteSystemd": True,
                "localModelHost": True,
                "rosReadiness": True,
                "continuousLoop": True,
                "recording": True,
                "actionLimits": True,
                "manualArmConfirmation": True,
                "emergencyStop": True,
            },
        }

    @app.get("/api/deploy/model-catalog", dependencies=[Depends(authorize)])
    def deployment_model_catalog() -> dict[str, Any]:
        return {"models": MODEL_PROVIDER_CATALOG, "count": len(MODEL_PROVIDER_CATALOG)}

    @app.post("/api/deploy/doctor", dependencies=[Depends(authorize)])
    def deployment_doctor(request: DeploymentRecipeRequest) -> dict[str, Any]:
        try:
            recipe = parse_deployment_recipe(request.recipe)
            return {
                "ok": True,
                "deploymentId": recipe.deployment_id,
                "recipeVersion": 2,
                "summary": {"passed": 5, "warnings": 0, "failed": 0},
                "checks": [
                    {"code": "recipe.schema", "status": "pass", "message": "Recipe schema 有效", "details": {}},
                    {"code": "recipe.topology", "status": "pass", "message": "模型、本体与隧道主机引用有效", "details": {}},
                    {"code": "recipe.model", "status": "pass", "message": "模型 Provider 与 Checkpoint 配置有效", "details": {}},
                    {"code": "recipe.ros", "status": "pass", "message": "ROS 环境与 readiness 契约已配置", "details": {}},
                    {"code": "recipe.rollback", "status": "pass", "message": "停止与自动回滚策略有效", "details": {}},
                ],
                "note": "SSH/本地执行、systemd、模型、隧道和 ROS 动态检查将在启动编排中按顺序执行",
            }
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/deploy/robot-connection", dependencies=[Depends(authorize)])
    def check_deployment_robot_connection(request: DeploymentConfigRequest) -> dict[str, Any]:
        try:
            config = parse_deployment_config(request.config)
            if config.kind != "robot":
                raise ValueError("连接检测仅接受本体配置")
            check_root = deploy_root / "connection-checks" / config.config_id
            runner = RecipeSshRunner(config.host, check_root / "known_hosts", check_root / "askpass")
            result = require_remote_ok(
                runner.run(
                    ["python3", "-c", "import platform; print(platform.node())"],
                    timeout=config.host.connect_timeout_s + 5,
                ),
                "连接本体",
            )
            return {
                "connected": True,
                "configId": config.config_id,
                "host": config.host.address,
                "hostname": result.stdout.strip(),
            }
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/examples/{name}", dependencies=[Depends(authorize)])
    def deployment_example(name: str) -> dict[str, Any]:
        if name not in {"recipe", "robot-config", "model-config", "component-configs"}:
            raise HTTPException(status_code=404, detail="部署示例不存在")
        path = settings.DEPLOYMENT_CONFIG_DIR / "recipe.example.json"
        recipe = json.loads(path.read_text(encoding="utf-8"))
        if name != "recipe":
            robot = json.loads(
                (settings.DEPLOYMENT_CONFIG_DIR / "robot.example.json").read_text(encoding="utf-8")
            )
            model = json.loads(
                (settings.DEPLOYMENT_CONFIG_DIR / "models" / "python.example.json").read_text(encoding="utf-8")
            )
            values = {
                "robot-config": robot,
                "model-config": model,
                "component-configs": {
                    "robot": robot,
                    "model": model,
                },
            }
            return {"name": name, "config": values[name]} if name != "component-configs" else {"name": name, **values[name]}
        return {"name": name, "recipe": recipe}

    @app.get("/api/deploy/orchestrations", dependencies=[Depends(authorize)])
    def list_deployment_orchestrations() -> dict[str, Any]:
        values = deployment_orchestrations.list()
        return {"orchestrations": values, "count": len(values)}

    @app.post("/api/deploy/orchestrations", dependencies=[Depends(authorize)])
    def start_deployment_orchestration(request: DeploymentOrchestrationStartRequest) -> dict[str, Any]:
        try:
            item = deployment_orchestrations.create(request.recipe, mode=request.mode)
            return item.start()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/deploy/orchestrations/prepare-model", dependencies=[Depends(authorize)])
    def prepare_deployment_model(request: DeploymentOrchestrationStartRequest) -> dict[str, Any]:
        try:
            item = deployment_orchestrations.create(request.recipe, mode="dry_run")
            return item.prepare_model()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/orchestrations/{orchestration_id}", dependencies=[Depends(authorize)])
    def get_deployment_orchestration(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).snapshot()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/start-dry-run",
        dependencies=[Depends(authorize)],
    )
    def start_deployment_dry_run(
        orchestration_id: str,
        request: DeploymentDryRunRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).start(task_prompt=request.taskPrompt)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/start-evaluation",
        dependencies=[Depends(authorize)],
    )
    def start_deployment_evaluation(
        orchestration_id: str,
        request: DeploymentDryRunRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).start_evaluation(
                task_prompt=request.taskPrompt
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/prompt",
        dependencies=[Depends(authorize)],
    )
    def update_deployment_prompt(
        orchestration_id: str,
        request: DeploymentDryRunRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).update_task_prompt(
                request.taskPrompt
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/scheduler",
        dependencies=[Depends(authorize)],
    )
    def update_deployment_scheduler(
        orchestration_id: str,
        request: DeploymentSchedulerRequest,
    ) -> dict[str, Any]:
        try:
            request_after: int | str = request.requestAfterSteps
            if isinstance(request_after, str) and request_after != "auto":
                request_after = int(request_after)
            return deployment_orchestrations.get(orchestration_id).update_action_scheduler(
                mode=request.mode,
                action_steps=request.actionSteps,
                request_after_steps=request_after,
                latency_margin_ms=request.latencyMarginMs,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/disconnect-robot",
        dependencies=[Depends(authorize)],
    )
    def disconnect_deployment_robot(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).disconnect_robot()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/close-model",
        dependencies=[Depends(authorize)],
    )
    def close_deployment_model(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).close_model()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/poses",
        dependencies=[Depends(authorize)],
    )
    def record_deployment_pose(
        orchestration_id: str,
        request: DeploymentPoseRecordRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).record_pose(request.name)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/poses/{pose_id}/move",
        dependencies=[Depends(authorize)],
    )
    def move_deployment_pose(
        orchestration_id: str,
        pose_id: str,
        request: DeploymentPoseMoveRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).move_to_recorded_pose(
                pose_id,
                duration_s=request.durationS,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.delete(
        "/api/deploy/orchestrations/{orchestration_id}/poses/{pose_id}",
        dependencies=[Depends(authorize)],
    )
    def delete_deployment_pose(orchestration_id: str, pose_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).delete_pose(pose_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/arm-challenge",
        dependencies=[Depends(authorize)],
    )
    def deployment_orchestration_arm_challenge(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).arm_challenge()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/start-live",
        dependencies=[Depends(authorize)],
    )
    def deployment_orchestration_start_live(
        orchestration_id: str,
        request: DeploymentConfirmationRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).promote_live(request.confirmation)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/stop-evaluation",
        dependencies=[Depends(authorize)],
    )
    def deployment_orchestration_stop_evaluation(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).stop_evaluation()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/stop",
        dependencies=[Depends(authorize)],
    )
    def stop_deployment_orchestration(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).stop()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/emergency-stop",
        dependencies=[Depends(authorize)],
    )
    def emergency_stop_deployment_orchestration(
        orchestration_id: str,
        request: DeploymentEmergencyStopRequest,
    ) -> dict[str, Any]:
        try:
            item = deployment_orchestrations.get(orchestration_id)
            item.last_error = request.reason
            return item.stop(emergency=True)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/logs",
        dependencies=[Depends(authorize)],
    )
    def deployment_orchestration_logs(
        orchestration_id: str,
        request: DeploymentOrchestrationLogsRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).component_logs(
                request.component,
                request.lines,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/components/{component}/restart",
        dependencies=[Depends(authorize)],
    )
    def restart_deployment_orchestration_component(
        orchestration_id: str,
        component: str,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).restart_component(component)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get(
        "/api/deploy/orchestrations/{orchestration_id}/manifest",
        dependencies=[Depends(authorize)],
    )
    def deployment_orchestration_manifest(orchestration_id: str) -> Response:
        try:
            payload = deployment_orchestrations.get(orchestration_id).manifest()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        return Response(
            json.dumps(payload, ensure_ascii=False, indent=2),
            media_type="application/json",
            headers={"Content-Disposition": f'attachment; filename="embodit-orchestration-{orchestration_id[:8]}.json"'},
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
