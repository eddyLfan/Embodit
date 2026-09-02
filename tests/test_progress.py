import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from fastapi import HTTPException
from starlette.requests import Request
from starlette.responses import Response

import app as app_module
from app import (
    LabelUpsertRequest,
    InspectRequest,
    LabelsLoadRequest,
    LabelsSaveRequest,
    ProgressLoadRequest,
    ProgressRequest,
    build_app,
    existing_root,
)


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def test_progress_round_trip_keeps_only_quarantine_reasons(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    save = _endpoint(app, "/api/progress/save")
    load = _endpoint(app, "/api/progress/load")
    progress_path = tmp_path / "dataset.review.json"

    saved = save(
        ProgressRequest(
            path=str(progress_path),
            dataset="/data/demo",
            states={"1": "quarantine", "2": "pass"},
            quarantineReasons={"1": "task_failed", "2": "sensor_data"},
        )
    )
    loaded = load(ProgressLoadRequest(path=str(progress_path)))

    assert saved["quarantineReasons"] == {"1": "task_failed"}
    assert loaded["version"] == 3
    assert loaded["quarantineReasons"] == {"1": "task_failed"}


def test_progress_load_remains_compatible_with_v2(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    load = _endpoint(app, "/api/progress/load")
    progress_path = tmp_path / "legacy.review.json"
    progress_path.write_text(
        json.dumps({"version": 2, "dataset": "/data/demo", "states": {"1": "exclude", "2": "keep"}}),
        encoding="utf-8",
    )

    loaded = load(ProgressLoadRequest(path=str(progress_path)))

    assert loaded["states"] == {"1": "quarantine", "2": "pass"}
    assert loaded["quarantineReasons"] == {}


def test_parallel_progress_saves_always_leave_valid_json(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    save = _endpoint(app, "/api/progress/save")
    progress_path = tmp_path / "parallel.review.json"

    def _save(index: int) -> None:
        save(
            ProgressRequest(
                path=str(progress_path),
                dataset="/data/demo",
                states={str(index): "pass"},
                quarantineReasons={},
            )
        )

    with ThreadPoolExecutor(max_workers=8) as executor:
        list(executor.map(_save, range(32)))

    document = json.loads(progress_path.read_text(encoding="utf-8"))
    assert document["version"] == 3
    assert len(document["states"]) == 1
    assert not list(tmp_path.glob(".parallel.review.json.tmp-*"))


def test_parallel_first_saves_cannot_claim_one_sidecar_for_two_datasets(
    tmp_path: Path,
    monkeypatch,
) -> None:
    first_dataset = tmp_path / "first"
    second_dataset = tmp_path / "second"
    first_dataset.mkdir()
    second_dataset.mkdir()
    progress_path = tmp_path / "shared.review.json"
    save = _endpoint(build_app("token", tmp_path, tmp_path), "/api/progress/save")

    # Hold the first writer at staging. Without a lock around both the
    # ownership check and publication, the second writer also reaches staging
    # and both requests incorrectly succeed.
    original_mkstemp = app_module.tempfile.mkstemp
    first_staged = threading.Event()
    second_staged = threading.Event()
    calls_lock = threading.Lock()
    calls = 0

    def synchronized_mkstemp(*args, **kwargs):
        nonlocal calls
        result = original_mkstemp(*args, **kwargs)
        with calls_lock:
            calls += 1
            position = calls
        if position == 1:
            first_staged.set()
            second_staged.wait(timeout=0.5)
        else:
            second_staged.set()
        return result

    monkeypatch.setattr(app_module.tempfile, "mkstemp", synchronized_mkstemp)

    def attempt(dataset: Path, state: str) -> int:
        try:
            save(
                ProgressRequest(
                    path=str(progress_path),
                    dataset=str(dataset),
                    states={state: "pass"},
                )
            )
        except HTTPException as error:
            return error.status_code
        return 200

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(attempt, first_dataset, "1")
        assert first_staged.wait(timeout=2)
        second = executor.submit(attempt, second_dataset, "2")
        statuses = [first.result(timeout=3), second.result(timeout=3)]

    assert sorted(statuses) == [200, 400]
    assert calls == 1
    owner = Path(json.loads(progress_path.read_text(encoding="utf-8"))["dataset"])
    assert owner in {first_dataset.resolve(), second_dataset.resolve()}
    assert not list(tmp_path.glob(".shared.review.json.tmp-*"))


def test_progress_rejects_non_sidecar_path_without_overwriting_it(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    save = _endpoint(app, "/api/progress/save")
    unrelated = tmp_path / "dataset.parquet"
    unrelated.write_bytes(b"valuable dataset bytes")

    with pytest.raises(HTTPException) as caught:
        save(ProgressRequest(path=str(unrelated), dataset="/data/demo", states={"1": "pass"}))

    assert caught.value.status_code == 400
    assert unrelated.read_bytes() == b"valuable dataset bytes"


def test_progress_save_refuses_to_replace_a_corrupt_sidecar(tmp_path: Path) -> None:
    app = build_app("token", tmp_path, tmp_path)
    save = _endpoint(app, "/api/progress/save")
    progress_path = tmp_path / "dataset.review.json"
    progress_path.write_text("{broken", encoding="utf-8")

    with pytest.raises(HTTPException) as caught:
        save(ProgressRequest(path=str(progress_path), dataset="/data/demo", states={"1": "pass"}))

    assert caught.value.status_code == 400
    assert progress_path.read_text(encoding="utf-8") == "{broken"


def test_progress_save_refuses_to_replace_another_datasets_sidecar(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    app = build_app("token", tmp_path, tmp_path)
    save = _endpoint(app, "/api/progress/save")
    progress_path = tmp_path / "shared.review.json"
    save(ProgressRequest(path=str(progress_path), dataset=str(first), states={"1": "pass"}))
    original = progress_path.read_bytes()

    with pytest.raises(HTTPException) as caught:
        save(ProgressRequest(path=str(progress_path), dataset=str(second), states={"2": "pass"}))

    assert caught.value.status_code == 400
    assert progress_path.read_bytes() == original
    assert json.loads(original)["dataset"] == str(first.resolve())


@pytest.mark.parametrize(
    "document",
    [
        "{broken",
        json.dumps([]),
        json.dumps({"version": 3, "dataset": "/data/demo", "states": []}),
        json.dumps({"version": 99, "dataset": "/data/demo", "states": {}}),
    ],
)
def test_progress_load_rejects_corrupt_or_invalid_documents(
    tmp_path: Path,
    document: str,
) -> None:
    app = build_app("token", tmp_path, tmp_path)
    load = _endpoint(app, "/api/progress/load")
    progress_path = tmp_path / "invalid.review.json"
    progress_path.write_text(document, encoding="utf-8")

    with pytest.raises(HTTPException) as caught:
        load(ProgressLoadRequest(path=str(progress_path)))

    assert caught.value.status_code == 400


@pytest.mark.parametrize(
    ("route", "request_factory"),
    [
        ("/api/labels/load", lambda dataset, path: LabelsLoadRequest(dataset=dataset, path=path)),
        (
            "/api/labels/save",
            lambda dataset, path: LabelsSaveRequest(dataset=dataset, path=path, labels=[]),
        ),
        (
            "/api/labels/upsert",
            lambda dataset, path: LabelUpsertRequest(dataset=dataset, path=path, label={}),
        ),
        (
            "/api/labels/delete",
            lambda dataset, path: LabelUpsertRequest(dataset=dataset, path=path, label={}),
        ),
    ],
)
def test_label_routes_reject_non_default_custom_paths(
    tmp_path: Path,
    route: str,
    request_factory,
) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    foreign = tmp_path / "foreign.jsonl"
    foreign.write_text("do not replace\n", encoding="utf-8")
    endpoint = _endpoint(build_app("token", tmp_path, tmp_path), route)

    with pytest.raises(HTTPException) as caught:
        endpoint(request_factory(str(dataset), str(foreign)))

    assert caught.value.status_code == 400
    assert foreign.read_text(encoding="utf-8") == "do not replace\n"


def test_label_load_accepts_the_dataset_default_sidecar(tmp_path: Path) -> None:
    dataset = tmp_path / "dataset"
    dataset.mkdir()
    expected = dataset / "labels.jsonl"
    endpoint = _endpoint(build_app("token", tmp_path, tmp_path), "/api/labels/load")

    loaded = endpoint(LabelsLoadRequest(dataset=str(dataset), path=str(expected)))

    assert loaded["path"] == str(expected.resolve())


def test_label_routes_reject_a_missing_dataset_path(tmp_path: Path) -> None:
    endpoint = _endpoint(build_app("token", tmp_path, tmp_path), "/api/labels/load")

    with pytest.raises(HTTPException) as caught:
        endpoint(LabelsLoadRequest(dataset=str(tmp_path / "typo")))

    assert caught.value.status_code == 404
    assert not (tmp_path / "typo").exists()


def test_missing_browse_root_fails_closed(tmp_path: Path) -> None:
    missing = tmp_path / "missing"
    with pytest.raises(ValueError, match="浏览根目录不存在"):
        existing_root(missing)


def test_query_token_is_exchanged_via_redirect_before_app_load(tmp_path: Path) -> None:
    app = build_app("secret", tmp_path, tmp_path)
    index = _endpoint(app, "/")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"token=secret&lang=en",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
    )

    response = index(request)

    assert response.status_code == 303
    assert response.headers["location"] == "/?lang=en"
    cookie = response.headers["set-cookie"]
    assert "embodit_token=secret" in cookie
    assert "HttpOnly" in cookie
    assert "Secure" not in cookie
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_query_token_cookie_is_secure_over_https(tmp_path: Path) -> None:
    app = build_app("secret", tmp_path, tmp_path)
    index = _endpoint(app, "/")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"token=secret",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("localhost", 443),
        }
    )

    response = index(request)

    assert "Secure" in response.headers["set-cookie"]


def test_html_response_has_security_headers(tmp_path: Path) -> None:
    (tmp_path / "index.html").write_text("<main>Embodit</main>", encoding="utf-8")
    index = _endpoint(build_app("secret", tmp_path, tmp_path), "/")
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [(b"cookie", b"embodit_token=secret")],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
    )

    response = index(request)

    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"


def test_security_middleware_defaults_api_cache_without_overriding_media(tmp_path: Path) -> None:
    app = build_app("secret", tmp_path, tmp_path)
    dispatch = app.user_middleware[0].kwargs["dispatch"]
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/api/health",
            "raw_path": b"/api/health",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
    )

    async def apply(response: Response) -> Response:
        async def call_next(_request):
            return response

        return await dispatch(request, call_next)

    plain = asyncio.run(apply(Response()))
    cached = asyncio.run(apply(Response(headers={"Cache-Control": "private, max-age=3600"})))

    assert plain.headers["cache-control"] == "no-store"
    assert plain.headers["referrer-policy"] == "no-referrer"
    assert plain.headers["x-content-type-options"] == "nosniff"
    assert plain.headers["x-frame-options"] == "DENY"
    assert cached.headers["cache-control"] == "private, max-age=3600"


def test_all_job_route_path_fields_reject_invalid_ids(tmp_path: Path) -> None:
    app = build_app("secret", tmp_path, tmp_path)
    job_routes = [
        route
        for route in app.routes
        if getattr(route, "path", "").startswith(
            ("/api/convert/", "/api/qc/scans/")
        )
        and ("{job_id}" in route.path or "{scan_id}" in route.path)
    ]

    assert len(job_routes) == 15
    for route in job_routes:
        parameter_name = "job_id" if "{job_id}" in route.path else "scan_id"
        field = next(item for item in route.dependant.path_params if item.name == parameter_name)
        value, errors = field.validate("bad.id", {}, loc=("path", parameter_name))
        assert value is None
        assert errors, route.path


def test_lifespan_stops_deployments_through_threadpool(tmp_path: Path, monkeypatch) -> None:
    calls: list[object] = []

    async def fake_run_in_threadpool(function, *args, **kwargs):
        calls.append(function)
        return function(*args, **kwargs)

    monkeypatch.setattr(app_module, "run_in_threadpool", fake_run_in_threadpool)
    app = build_app("token", tmp_path, tmp_path)

    async def run_lifespan() -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(run_lifespan())

    assert len(calls) == 1
    assert getattr(calls[0], "__name__", "") == "stop_all"


def test_inspect_serialization_runs_inside_threadpool(tmp_path: Path, monkeypatch) -> None:
    inside_threadpool = False
    calls: list[str] = []

    class View:
        def to_inspect_dict(self):
            assert inside_threadpool
            calls.append("serialize")
            return {"ok": True}

    class Adapter:
        def inspect(self):
            assert inside_threadpool
            calls.append("inspect")
            return View()

    def fake_open_dataset(_path):
        assert inside_threadpool
        calls.append("open")
        return Adapter()

    async def fake_run_in_threadpool(function, *args, **kwargs):
        nonlocal inside_threadpool
        inside_threadpool = True
        try:
            return function(*args, **kwargs)
        finally:
            inside_threadpool = False

    monkeypatch.setattr(app_module, "open_dataset", fake_open_dataset)
    monkeypatch.setattr(app_module, "run_in_threadpool", fake_run_in_threadpool)
    endpoint = _endpoint(build_app("token", tmp_path, tmp_path), "/api/inspect")

    result = asyncio.run(endpoint(InspectRequest(dataset=str(tmp_path))))

    assert result == {"ok": True}
    assert calls == ["open", "inspect", "serialize"]
