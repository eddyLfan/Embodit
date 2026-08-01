import json
from pathlib import Path

from app import ProgressLoadRequest, ProgressRequest, build_app


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
