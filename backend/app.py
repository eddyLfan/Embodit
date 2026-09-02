#!/usr/bin/env python3
"""Local web server for multi-format embodied dataset review."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import tempfile
import threading
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import urlencode

import uvicorn
from fastapi import (
    Cookie,
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path as ApiPath,
    Query,
    Request,
)
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
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
    refresh_stored_job as refresh_convert_job,
)
import settings  # noqa: E402
from review_config import review_config_payload  # noqa: E402

from datasets.detect import dataset_brief, detect_format, list_entries  # noqa: E402
from datasets.export import (  # noqa: E402
    DECISION_PASS,
    episodes_for_export,
    normalize_decision,
)
from datasets.registry import open_dataset  # noqa: E402
from datasets.view import FORMAT_LABELS, SUPPORTED_FORMATS  # noqa: E402
from deploy.orchestrator import DeploymentOrchestration, OrchestrationRegistry  # noqa: E402
from deploy.offline_evaluation import evaluate_dataset_frame, load_dataset_action_replay  # noqa: E402
from deploy.recipe import (  # noqa: E402
    compose_recipe as compose_deployment_recipe,
    parse_deployment_config,
    parse_recipe as parse_deployment_recipe,
    redact_recipe,
    split_recipe as split_deployment_recipe,
)
from deploy.store import DeploymentConfigStore, RecipeStore  # noqa: E402
from deploy.transport import (  # noqa: E402
    LocalCommandRunner,
    RecipeSshRunner,
    require_remote_ok,
)
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
    refresh_stored_job as refresh_qc_job,
    resume_job as resume_qc_job,
)
from qc.paths import find_report as find_qc_report  # noqa: E402
from qc.store import (  # noqa: E402
    episode_detail as qc_episode_detail,
    query_episodes as query_qc_episodes,
    report_csv_chunks as qc_report_csv_chunks,
    review_episode as review_qc_episode,
    review_finding as review_qc_finding,
    selected_episode_indices as qc_selected_episode_indices,
    summary as qc_summary,
)


JobIdPath = Annotated[str, ApiPath(pattern=r"^[A-Za-z0-9_-]{1,128}$")]


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
    recordVideo: bool = False


class DeploymentEmergencyStopRequest(BaseModel):
    reason: str = Field(default="网页急停", max_length=500)


class DeploymentOrchestrationStartRequest(BaseModel):
    recipe: dict[str, Any]
    mode: str | None = None
    robotConfigId: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")
    modelConfigId: str | None = Field(default=None, min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.-]+$")


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


class DeploymentOfflineEvaluationRequest(BaseModel):
    dataset: str = Field(min_length=1)
    episodeIndex: int = Field(ge=0)
    frameIndex: int = Field(default=0, ge=0)
    taskPrompt: str | None = Field(default=None, max_length=2000)


class DeploymentHardwareReplayRequest(BaseModel):
    dataset: str = Field(min_length=1)
    episodeIndex: int = Field(ge=0)
    startFrame: int = Field(default=0, ge=0)
    endFrame: int | None = Field(default=None, ge=1)
    moveToStartDurationS: float = Field(default=3.0, gt=0, le=60)


def build_app(token: str, browse_root: Path, web_root: Path) -> FastAPI:
    browse_root = existing_root(browse_root)
    images_root = web_root.parent / "images"
    jobs_dir = default_convert_jobs_dir()
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
    deployment_orchestrations = OrchestrationRegistry(
        deploy_root / "orchestrations",
        recording_root=settings.OUTPUT_DIR / "deployment-recordings",
        pose_root=settings.OUTPUT_DIR / "deployment-poses",
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        try:
            yield
        finally:
            await run_in_threadpool(deployment_orchestrations.stop_all)

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
        EMBODIT_SANDBOX=1 is set. ``embodit.sh`` enables it automatically for
        non-loopback listeners; direct app launches may opt in explicitly."""
        resolved = Path(raw).expanduser().resolve()
        if settings.SANDBOX_PATHS and not is_inside(browse_root, resolved):
            raise HTTPException(status_code=403, detail=f"{what}超出允许的根目录：{browse_root}")
        return resolved

    def review_sidecar(raw: str | Path) -> Path:
        target = sandboxed(raw, what="进度文件路径")
        if not target.name.endswith(".review.json"):
            raise HTTPException(status_code=400, detail="进度文件必须是 .review.json sidecar")
        return target

    @contextmanager
    def lock_review_sidecar(target: Path):
        """Serialize ownership checks and publication across processes."""
        target.parent.mkdir(parents=True, exist_ok=True)
        lock_path = target.with_name(f".{target.name}.lock")
        flags = os.O_CREAT | os.O_RDWR | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)

    def read_progress_document(target: Path) -> dict[str, Any]:
        try:
            document = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            raise HTTPException(status_code=400, detail=f"进度文件无法解析：{error}") from error
        if not isinstance(document, dict):
            raise HTTPException(status_code=400, detail="进度文件根节点必须是对象")
        if document.get("version") not in {2, 3}:
            raise HTTPException(status_code=400, detail="进度文件 version 必须是 2 或 3")
        if not isinstance(document.get("dataset"), str) or not document["dataset"].strip():
            raise HTTPException(status_code=400, detail="进度文件 dataset 必须是非空字符串")
        states = document.get("states")
        if not isinstance(states, dict) or not all(isinstance(value, str) for value in states.values()):
            raise HTTPException(status_code=400, detail="进度文件 states 必须是字符串映射")
        reasons = document.get("quarantineReasons", {})
        if not isinstance(reasons, dict) or not all(isinstance(value, str) for value in reasons.values()):
            raise HTTPException(
                status_code=400,
                detail="进度文件 quarantineReasons 必须是字符串映射",
            )
        normalized = {key: normalize_decision(value) for key, value in states.items()}
        document["states"] = normalized
        document["quarantineReasons"] = {
            key: value.strip()
            for key, value in reasons.items()
            if normalized.get(key) == "quarantine" and value.strip()
        }
        return document

    def labels_sidecar(dataset_raw: str | Path, requested_raw: str | Path | None) -> Path:
        dataset = sandboxed(dataset_raw, what="数据集路径")
        if not (dataset.is_file() or dataset.is_dir()):
            raise HTTPException(status_code=404, detail=f"数据集不存在：{dataset}")
        expected = default_labels_path(dataset).resolve()
        if requested_raw is not None:
            requested = sandboxed(requested_raw, what="标签文件路径")
            if requested != expected:
                raise HTTPException(
                    status_code=400,
                    detail=f"标签文件必须是该数据集的默认 sidecar：{expected}",
                )
        return expected

    security_headers = {
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }
    token_response_headers = {**security_headers, "Cache-Control": "no-store"}

    @app.middleware("http")
    async def add_security_headers(request: Request, call_next):
        response = await call_next(request)
        for name, value in security_headers.items():
            if name not in response.headers:
                response.headers[name] = value
        if request.url.path.startswith("/api/") and "Cache-Control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/", response_class=HTMLResponse)
    def index(request: Request) -> Response:
        # First visit carries ?token=... which we exchange for an HttpOnly
        # cookie; the token is no longer embedded in the page or asset URLs.
        query_token = request.query_params.get("token")
        cookie_token = request.cookies.get("embodit_token")
        if query_token != token and cookie_token != token:
            raise HTTPException(status_code=401, detail="无效或缺失的访问令牌")
        if query_token is not None:
            clean_query = urlencode(
                [(key, value) for key, value in request.query_params.multi_items() if key != "token"]
            )
            target = request.url.path + (f"?{clean_query}" if clean_query else "")
            response = RedirectResponse(target, status_code=303, headers=token_response_headers)
            if query_token == token:
                response.set_cookie(
                    "embodit_token",
                    token,
                    httponly=True,
                    samesite="lax",
                    secure=request.url.scheme == "https",
                    max_age=30 * 24 * 3600,
                )
            return response
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
        return HTMLResponse(page, headers=token_response_headers)

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
            "pathSandbox": {"enabled": settings.SANDBOX_PATHS, "root": str(browse_root)},
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
            def _inspect_dataset() -> dict[str, Any]:
                return open_dataset(dataset).inspect().to_inspect_dict()

            return await run_in_threadpool(_inspect_dataset)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/timeseries", dependencies=[Depends(authorize)])
    async def timeseries(
        dataset: str,
        episode: int,
        keys: str | None = None,
        maxPoints: Annotated[int | None, Query(ge=1, le=10_000)] = None,
    ) -> dict[str, Any]:
        dataset_path = sandboxed(dataset, what="数据集路径")
        key_list = [item for item in (keys or "").split(",") if item] or None
        cap = int(maxPoints) if maxPoints is not None else 0
        try:
            def _load() -> tuple[dict[str, Any], dict[str, int]]:
                import numpy as np

                adapter = open_dataset(dataset_path)
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
        dataset_path = sandboxed(dataset, what="数据集路径")
        try:
            def _materialize() -> Path:
                adapter = open_dataset(dataset_path)
                if getattr(adapter, "format_id", None) != "mcap":
                    raise ValueError("仅 MCAP 数据集支持 topic 视频预览")
                return adapter.materialize_topic_video(episode, topic)

            path = await run_in_threadpool(_materialize)
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
        dataset_path = sandboxed(dataset, what="数据集路径")
        try:
            def _materialize() -> Path:
                adapter = open_dataset(dataset_path)
                if getattr(adapter, "format_id", None) != "hdf5":
                    raise ValueError("仅 HDF5 数据集支持 frames 视频预览")
                return adapter.materialize_camera_video(episode, camera)

            path = await run_in_threadpool(_materialize)
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
        target = review_sidecar(request.path)
        dataset = sandboxed(request.dataset, what="数据集路径")
        normalized = {key: normalize_decision(value) for key, value in request.states.items()}
        quarantine_reasons = {
            key: str(value).strip()
            for key, value in request.quarantineReasons.items()
            if normalized.get(key) == "quarantine" and str(value).strip()
        }
        document = {
            "version": 3,
            "dataset": str(dataset),
            "updatedAt": now_iso(),
            "updatedBy": os.environ.get("USER", "unknown"),
            "states": normalized,
            "quarantineReasons": quarantine_reasons,
        }
        temporary: Path | None = None
        try:
            with lock_review_sidecar(target):
                if target.exists():
                    if not target.is_file():
                        raise HTTPException(status_code=400, detail=f"进度路径不是文件：{target}")
                    existing = read_progress_document(target)
                    existing_dataset = Path(existing["dataset"]).expanduser().resolve()
                    if existing_dataset != dataset:
                        raise HTTPException(
                            status_code=400,
                            detail=f"进度文件属于其他数据集：{existing_dataset}",
                        )
                descriptor, temporary_name = tempfile.mkstemp(
                    prefix=f".{target.name}.tmp-",
                    dir=target.parent,
                )
                temporary = Path(temporary_name)
                with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
                    json.dump(document, stream, ensure_ascii=False, indent=2)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
                temporary = None
        except HTTPException:
            raise
        except OSError as error:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise HTTPException(status_code=400, detail=f"进度文件无法保存：{error}") from error
        except BaseException:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            raise
        return {"path": str(target), "states": normalized, "quarantineReasons": quarantine_reasons}

    @app.post("/api/progress/load", dependencies=[Depends(authorize)])
    def load_progress(request: ProgressLoadRequest) -> dict[str, Any]:
        target = review_sidecar(request.path)
        if not target.is_file():
            raise HTTPException(status_code=404, detail=f"进度文件不存在：{target}")
        return read_progress_document(target)

    @app.post("/api/create", dependencies=[Depends(authorize)])
    def create(request: CreateRequest) -> dict[str, Any]:
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
    def convert_status(job_id: JobIdPath) -> dict[str, Any]:
        job = refresh_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        return job

    @app.get("/api/convert/jobs", dependencies=[Depends(authorize)])
    def convert_jobs_list(limit: int = 30) -> dict[str, Any]:
        rows = []
        for job in list_convert_jobs(jobs_dir, limit=limit):
            refreshed = refresh_convert_job(jobs_dir, str(job.get("jobId") or ""))
            if refreshed is not None:
                rows.append(refreshed)
        return {"jobs": rows, "jobsDir": str(jobs_dir)}

    @app.post("/api/convert/jobs/{job_id}/dismiss", dependencies=[Depends(authorize)])
    def convert_job_dismiss(job_id: JobIdPath) -> dict[str, Any]:
        job = read_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        if job.get("status") in {"queued", "running"}:
            raise HTTPException(status_code=400, detail="进行中的任务不能直接清除，请先取消或等待结束")
        deleted = delete_convert_job(jobs_dir, job_id)
        return {"ok": deleted, "jobId": job_id}

    @app.post("/api/convert/jobs/{job_id}/cancel", dependencies=[Depends(authorize)])
    def convert_job_cancel(job_id: JobIdPath) -> dict[str, Any]:
        job = read_convert_job(jobs_dir, job_id)
        if not job:
            raise HTTPException(status_code=404, detail="转换任务不存在")
        return cancel_convert_job(jobs_dir, job_id)

    @app.post("/api/labels/load", dependencies=[Depends(authorize)])
    def labels_load(request: LabelsLoadRequest) -> dict[str, Any]:
        path = labels_sidecar(request.dataset, request.path)
        return {"path": str(path), "labels": load_labels(path), "presets": preset_tags()}

    @app.post("/api/labels/save", dependencies=[Depends(authorize)])
    def labels_save(request: LabelsSaveRequest) -> dict[str, Any]:
        path = labels_sidecar(request.dataset, request.path)
        try:
            save_labels(path, request.labels)
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error
        return {"path": str(path), "count": len(request.labels)}

    @app.post("/api/labels/upsert", dependencies=[Depends(authorize)])
    def labels_upsert(request: LabelUpsertRequest) -> dict[str, Any]:
        path = labels_sidecar(request.dataset, request.path)
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
        path = labels_sidecar(request.dataset, request.path)
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
            patched = refresh_qc_job(qc_jobs_dir, str(job.get("jobId") or ""))
            if patched is None:
                continue
            if target is None or patched.get("dataset") == target:
                rows.append(patched)
        return {"jobs": rows}

    @app.get("/api/qc/scans/{scan_id}/status", dependencies=[Depends(authorize)])
    def qc_scan_status(scan_id: JobIdPath) -> dict[str, Any]:
        job = refresh_qc_job(qc_jobs_dir, scan_id)
        if job is None:
            raise HTTPException(status_code=404, detail="QC 任务不存在")
        return job

    @app.post("/api/qc/scans/{scan_id}/pause", dependencies=[Depends(authorize)])
    def qc_pause(scan_id: JobIdPath) -> dict[str, Any]:
        try:
            return pause_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/resume", dependencies=[Depends(authorize)])
    def qc_resume(scan_id: JobIdPath) -> dict[str, Any]:
        try:
            return resume_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/cancel", dependencies=[Depends(authorize)])
    def qc_cancel(scan_id: JobIdPath) -> dict[str, Any]:
        try:
            return cancel_qc_job(qc_jobs_dir, scan_id)
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post("/api/qc/scans/{scan_id}/dismiss", dependencies=[Depends(authorize)])
    def qc_dismiss(scan_id: JobIdPath) -> dict[str, Any]:
        job = refresh_qc_job(qc_jobs_dir, scan_id)
        if job is None:
            raise HTTPException(status_code=404, detail="QC 任务不存在")
        if job.get("status") in {"queued", "running", "paused"}:
            raise HTTPException(status_code=400, detail="活动中的 QC 任务不能清除，请先取消")
        return {"deleted": delete_qc_job(qc_jobs_dir, scan_id), "jobId": scan_id}

    @app.get("/api/qc/scans/{scan_id}/summary", dependencies=[Depends(authorize)])
    def qc_report_summary(scan_id: JobIdPath) -> dict[str, Any]:
        return qc_summary(resolve_qc_report(scan_id))

    @app.post("/api/qc/scans/{scan_id}/episodes/query", dependencies=[Depends(authorize)])
    def qc_report_episodes(scan_id: JobIdPath, request: QCQueryRequest) -> dict[str, Any]:
        try:
            return query_qc_episodes(resolve_qc_report(scan_id), request.filters)
        except (TypeError, ValueError) as error:
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get(
        "/api/qc/scans/{scan_id}/episodes/{episode_index}",
        dependencies=[Depends(authorize)],
    )
    def qc_report_episode(scan_id: JobIdPath, episode_index: int) -> dict[str, Any]:
        try:
            return qc_episode_detail(resolve_qc_report(scan_id), episode_index)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=f"Episode 不存在：{episode_index}") from error

    @app.post(
        "/api/qc/scans/{scan_id}/findings/{finding_id}/review",
        dependencies=[Depends(authorize)],
    )
    def qc_finding_review(
        scan_id: JobIdPath,
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
        scan_id: JobIdPath,
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
    def qc_selection_preview(scan_id: JobIdPath, request: QCQueryRequest) -> dict[str, Any]:
        return qc_selected_episode_indices(resolve_qc_report(scan_id), request.filters)

    @app.get("/api/qc/scans/{scan_id}/export-report", dependencies=[Depends(authorize)])
    def qc_export_report(scan_id: JobIdPath, kind: str = "episodes") -> Response:
        if kind not in {"episodes", "findings"}:
            raise HTTPException(status_code=400, detail="kind 只能是 episodes 或 findings")
        return StreamingResponse(
            qc_report_csv_chunks(resolve_qc_report(scan_id), kind),
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
    def list_deployment_configs(
        kind: str,
        source: Literal["all", "project"] = "all",
    ) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        configs = store.list_discovered() if source == "project" else store.list()
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
    def get_deployment_config(
        kind: str,
        config_id: str,
        source: Literal["all", "project"] = "all",
    ) -> dict[str, Any]:
        store = deployment_configs.get(kind)
        if store is None:
            raise HTTPException(status_code=404, detail="部署配置类型不存在")
        try:
            config = (
                store.get_discovered(config_id)
                if source == "project"
                else store.get(config_id)
            )
            return {"config": config, "kind": kind, "version": 1}
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
                "observationOnlyRobotConnection": True,
                "composableDeployment": True,
                "managedSshTunnel": True,
                "remoteSystemd": True,
                "localModelHost": True,
                "localRobotHost": True,
                "rosReadiness": True,
                "continuousLoop": True,
                "recording": True,
                "actionLimits": True,
                "manualArmConfirmation": True,
                "emergencyStop": True,
                "offlineSingleFrameEvaluation": True,
                "offlineEpisodeReplay": True,
                "hardwareDatasetReplay": True,
            },
        }

    @app.get("/api/deploy/model-catalog", dependencies=[Depends(authorize)])
    def deployment_model_catalog() -> dict[str, Any]:
        return {"models": MODEL_PROVIDER_CATALOG, "count": len(MODEL_PROVIDER_CATALOG)}

    @app.post("/api/deploy/doctor", dependencies=[Depends(authorize)])
    def deployment_doctor(request: DeploymentRecipeRequest) -> dict[str, Any]:
        try:
            recipe = parse_deployment_recipe(request.recipe)
            doctor = DeploymentOrchestration(recipe, deploy_root / "preflight" / recipe.deployment_id)
            return doctor.read_only_preflight()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/deploy/robot-connection", dependencies=[Depends(authorize)])
    def check_deployment_robot_connection(request: DeploymentConfigRequest) -> dict[str, Any]:
        try:
            config = parse_deployment_config(request.config)
            if config.kind != "robot":
                raise ValueError("连接检测仅接受本体配置")
            check_root = deploy_root / "connection-checks" / config.config_id
            runner = (
                LocalCommandRunner()
                if config.host.connection == "local"
                else RecipeSshRunner(
                    config.host,
                    check_root / "known_hosts",
                    check_root / "askpass",
                )
            )
            result = require_remote_ok(
                runner.run(
                    [
                        "python3",
                        "-c",
                        (
                            "import json,os,platform,pwd; "
                            "print(json.dumps({'hostname':platform.node(),"
                            "'user':pwd.getpwuid(os.geteuid()).pw_name}))"
                        ),
                    ],
                    timeout=config.host.connect_timeout_s + 5,
                ),
                "连接本体",
            )
            probe = json.loads(result.stdout)
            if config.host.connection == "local" and probe["user"] != config.host.user:
                raise ValueError(
                    f"本地本体主机 user={config.host.user} "
                    f"与 Embodit 运行用户 {probe['user']} 不一致"
                )
            return {
                "connected": True,
                "configId": config.config_id,
                "host": config.host.address,
                "connection": config.host.connection,
                "hostname": probe["hostname"],
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
            item = deployment_orchestrations.create(
                request.recipe,
                mode=request.mode,
                robot_config_id=request.robotConfigId,
                model_config_id=request.modelConfigId,
            )
            return item.start()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/deploy/orchestrations/prepare-model", dependencies=[Depends(authorize)])
    def prepare_deployment_model(request: DeploymentOrchestrationStartRequest) -> dict[str, Any]:
        try:
            item = deployment_orchestrations.create(
                request.recipe,
                mode="dry_run",
                robot_config_id=request.robotConfigId,
                model_config_id=request.modelConfigId,
            )
            return item.prepare_model()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post("/api/deploy/orchestrations/connect-robot", dependencies=[Depends(authorize)])
    def connect_deployment_robot_new(request: DeploymentOrchestrationStartRequest) -> dict[str, Any]:
        try:
            item = deployment_orchestrations.create(
                request.recipe,
                mode="dry_run",
                robot_config_id=request.robotConfigId,
                model_config_id=request.modelConfigId,
            )
            return item.connect_robot()
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/connect-robot",
        dependencies=[Depends(authorize)],
    )
    def connect_deployment_robot_existing(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).connect_robot()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/prepare-model",
        dependencies=[Depends(authorize)],
    )
    def prepare_deployment_model_existing(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).prepare_model()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/switch-model",
        dependencies=[Depends(authorize)],
    )
    def switch_deployment_model(
        orchestration_id: str,
        request: DeploymentOrchestrationStartRequest,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).switch_model(
                request.recipe,
                model_config_id=request.modelConfigId,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.get("/api/deploy/orchestrations/{orchestration_id}", dependencies=[Depends(authorize)])
    def get_deployment_orchestration(
        orchestration_id: str,
        include_preview: Annotated[bool, Query(alias="includePreview")] = True,
        include_model_images: Annotated[bool, Query(alias="includeModelImages")] = True,
        trajectory_points: Annotated[int, Query(alias="trajectoryPoints", ge=0, le=5000)] = 360,
    ) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).snapshot(
                include_preview=include_preview,
                include_model_images=include_model_images,
                trajectory_max_points=trajectory_points or None,
            )
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.get(
        "/api/deploy/orchestrations/{orchestration_id}/live-preview",
        dependencies=[Depends(authorize)],
    )
    def get_deployment_live_preview(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).live_preview_snapshot()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/offline-evaluation",
        dependencies=[Depends(authorize)],
    )
    async def run_deployment_offline_evaluation(
        orchestration_id: str,
        request: DeploymentOfflineEvaluationRequest,
    ) -> dict[str, Any]:
        try:
            orchestration = deployment_orchestrations.get(orchestration_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error

        dataset_path = sandboxed(request.dataset, what="离线评测数据集路径")
        client_config = orchestration.recipe.robot.client.config or {}
        telemetry = client_config.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        action_telemetry = telemetry.get("action")
        action_telemetry = action_telemetry if isinstance(action_telemetry, dict) else {}
        configured_names = action_telemetry.get("names")
        if not isinstance(configured_names, list):
            action_config = client_config.get("action")
            action_config = action_config if isinstance(action_config, dict) else {}
            configured_names = action_config.get("joints")
        state_telemetry = telemetry.get("state")
        state_telemetry = state_telemetry if isinstance(state_telemetry, dict) else {}
        configured_state_names = state_telemetry.get("names")

        def _evaluate() -> dict[str, Any]:
            adapter = open_dataset(dataset_path)
            return evaluate_dataset_frame(
                adapter,
                episode_index=request.episodeIndex,
                frame_index=request.frameIndex,
                predictor=orchestration.infer_observations,
                prompt=request.taskPrompt,
                action_names=configured_names if isinstance(configured_names, list) else None,
                state_names=configured_state_names if isinstance(configured_state_names, list) else None,
            )

        try:
            return await run_in_threadpool(_evaluate)
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/hardware-replay",
        dependencies=[Depends(authorize)],
    )
    async def start_deployment_hardware_replay(
        orchestration_id: str,
        request: DeploymentHardwareReplayRequest,
    ) -> dict[str, Any]:
        try:
            orchestration = deployment_orchestrations.get(orchestration_id)
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        dataset_path = sandboxed(request.dataset, what="真机 Replay 数据集路径")
        client_config = orchestration.recipe.robot.client.config or {}
        telemetry = client_config.get("telemetry")
        telemetry = telemetry if isinstance(telemetry, dict) else {}
        action_telemetry = telemetry.get("action")
        action_telemetry = action_telemetry if isinstance(action_telemetry, dict) else {}
        configured_names = action_telemetry.get("names")

        def _load_replay() -> dict[str, Any]:
            return load_dataset_action_replay(
                open_dataset(dataset_path),
                episode_index=request.episodeIndex,
                start_frame=request.startFrame,
                end_frame=request.endFrame,
                action_names=configured_names if isinstance(configured_names, list) else None,
            )

        try:
            replay = await run_in_threadpool(_load_replay)
            return orchestration.request_hardware_replay(
                replay,
                move_to_start_duration_s=request.moveToStartDurationS,
            )
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error

    @app.post(
        "/api/deploy/orchestrations/{orchestration_id}/hardware-replay/stop",
        dependencies=[Depends(authorize)],
    )
    def stop_deployment_hardware_replay(orchestration_id: str) -> dict[str, Any]:
        try:
            return deployment_orchestrations.get(orchestration_id).request_stop_hardware_replay()
        except KeyError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        except ValueError as error:
            raise HTTPException(status_code=409, detail=str(error)) from error
        except Exception as error:  # noqa: BLE001
            raise HTTPException(status_code=400, detail=str(error)) from error


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
            return deployment_orchestrations.get(orchestration_id).request_disconnect_robot()
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
            return deployment_orchestrations.get(orchestration_id).request_close_model()
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
            return deployment_orchestrations.get(orchestration_id).promote_live(
                request.confirmation,
                record_video=request.recordVideo,
            )
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
            return deployment_orchestrations.get(orchestration_id).request_stop_evaluation()
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
    if not resolved.is_dir():
        raise ValueError(f"浏览根目录不存在或不是目录：{resolved}")
    return resolved


def is_inside(root: Path, candidate: Path) -> bool:
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def default_review_path(dataset: Path) -> Path:
    dataset = dataset.expanduser().resolve()
    return dataset.with_name(dataset.name + ".review.json")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--browse-root", type=Path, default=Path.cwd())
    parser.add_argument("--token", required=True)
    args = parser.parse_args()
    loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    if "EMBODIT_SANDBOX" not in os.environ and args.host not in loopback_hosts:
        settings.SANDBOX_PATHS = True
        print(
            f"path guard enabled automatically for non-loopback listener {args.host}",
            file=sys.stderr,
        )
    elif not settings.SANDBOX_PATHS and args.host not in loopback_hosts:
        print(
            "WARNING: path guard is explicitly disabled on a non-loopback listener; "
            "authenticated clients can access paths allowed by the service account",
            file=sys.stderr,
        )
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
