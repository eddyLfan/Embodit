import json
from pathlib import Path

import settings
from app import build_app
from review_config import load_review_config, review_config_payload


def _endpoint(app, path: str):
    return next(route.endpoint for route in app.routes if getattr(route, "path", None) == path)


def test_review_config_supports_order_labels_and_disabled_items(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text(
        json.dumps({
            "quarantineReasons": [
                {"id": "custom", "label": {"zh": "自定义", "en": "Custom"}, "enabled": True},
                {"id": "old", "label": {"zh": "旧原因", "en": "Old reason"}, "enabled": False},
            ]
        }),
        encoding="utf-8",
    )

    loaded = load_review_config(path)

    assert [item["id"] for item in loaded["quarantineReasons"]] == ["custom", "old"]
    assert loaded["quarantineReasons"][1]["enabled"] is False


def test_invalid_review_config_returns_defaults_and_visible_error(tmp_path: Path) -> None:
    path = tmp_path / "review.json"
    path.write_text('{"quarantineReasons": [{"id": "bad id"}]}', encoding="utf-8")

    payload = review_config_payload(path)

    assert payload["configError"]
    assert payload["quarantineReasons"][0]["id"] == "task_failed"


def test_health_reads_review_config_without_server_restart(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "review.json"
    monkeypatch.setattr(settings, "REVIEW_CONFIG_PATH", path)
    app = build_app("token", tmp_path, tmp_path)
    health = _endpoint(app, "/api/health")

    def write(label: str) -> None:
        path.write_text(
            json.dumps({
                "quarantineReasons": [
                    {"id": "custom", "label": {"zh": label, "en": "Custom"}, "enabled": True}
                ]
            }),
            encoding="utf-8",
        )

    write("第一次")
    assert health()["review"]["quarantineReasons"][0]["label"]["zh"] == "第一次"
    write("第二次")
    assert health()["review"]["quarantineReasons"][0]["label"]["zh"] == "第二次"
