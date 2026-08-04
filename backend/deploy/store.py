"""Secure, atomic persistence for recipes and reusable deployment configs."""

from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any

from .recipe import parse_deployment_config, parse_recipe


SAFE_DEPLOYMENT_ID = re.compile(r"^[A-Za-z0-9_.-]+$")


class RecipeStore:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _path(self, deployment_id: str) -> Path:
        if not SAFE_DEPLOYMENT_ID.fullmatch(deployment_id):
            raise ValueError("deployment_id 非法")
        return self.root / f"{deployment_id}.json"

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = parse_recipe(raw).model_dump(mode="json")
        path = self._path(payload["deployment_id"])
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.describe(payload)

    def get(self, deployment_id: str) -> dict[str, Any]:
        path = self._path(deployment_id)
        if not path.is_file():
            raise KeyError(f"部署 Recipe 不存在：{deployment_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return parse_recipe(raw).model_dump(mode="json")

    def list(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        paths = sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for path in paths:
            try:
                values.append(self.describe(self.get(path.stem)))
            except Exception as error:  # noqa: BLE001
                values.append({
                    "deploymentId": path.stem,
                    "name": path.stem,
                    "version": 2,
                    "valid": False,
                    "error": str(error),
                })
        return values

    def delete(self, deployment_id: str) -> bool:
        path = self._path(deployment_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    @staticmethod
    def describe(raw: dict[str, Any]) -> dict[str, Any]:
        recipe = parse_recipe(raw)
        auth = {
            name: {
                "connection": host.connection,
                "type": host.auth.type if host.auth else None,
                "configured": bool(
                    host.auth
                    and (host.auth.identity_file or host.auth.password or host.auth.environment_variable)
                ),
            }
            for name, host in recipe.hosts.items()
        }
        return {
            "deploymentId": recipe.deployment_id,
            "name": recipe.name,
            "version": 2,
            "modelHost": recipe.model.host,
            "robotHost": recipe.robot.host,
            "defaultMode": recipe.runtime.default_mode,
            "auth": auth,
            "valid": True,
        }


class DeploymentConfigStore:
    """Private persistence for independently reusable robot/model configs."""

    def __init__(self, root: Path, kind: str):
        if kind not in {"robot", "model"}:
            raise ValueError("部署配置类型必须是 robot 或 model")
        self.kind = kind
        self.root = (root / kind).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.root.chmod(0o700)

    def _path(self, config_id: str) -> Path:
        if not SAFE_DEPLOYMENT_ID.fullmatch(config_id):
            raise ValueError("config_id 非法")
        return self.root / f"{config_id}.json"

    def _parse(self, raw: dict[str, Any]):
        value = parse_deployment_config(raw)
        if value.kind != self.kind:
            raise ValueError(f"此存储仅接受 {self.kind} 配置")
        return value

    def save(self, raw: dict[str, Any]) -> dict[str, Any]:
        payload = self._parse(raw).model_dump(mode="json")
        path = self._path(payload["config_id"])
        fd, temporary = tempfile.mkstemp(prefix=f".{path.stem}.", suffix=".tmp", dir=self.root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
            path.chmod(0o600)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return self.describe(payload)

    def get(self, config_id: str) -> dict[str, Any]:
        path = self._path(config_id)
        if not path.is_file():
            raise KeyError(f"{self.kind} 配置不存在：{config_id}")
        raw = json.loads(path.read_text(encoding="utf-8"))
        return self._parse(raw).model_dump(mode="json")

    def list(self) -> list[dict[str, Any]]:
        values: list[dict[str, Any]] = []
        paths = sorted(self.root.glob("*.json"), key=lambda item: item.stat().st_mtime_ns, reverse=True)
        for path in paths:
            try:
                values.append(self.describe(self.get(path.stem)))
            except Exception as error:  # noqa: BLE001
                values.append({
                    "configId": path.stem,
                    "name": path.stem,
                    "kind": self.kind,
                    "version": 1,
                    "valid": False,
                    "error": str(error),
                })
        return values

    def delete(self, config_id: str) -> bool:
        path = self._path(config_id)
        if not path.exists():
            return False
        path.unlink()
        return True

    def describe(self, raw: dict[str, Any]) -> dict[str, Any]:
        value = self._parse(raw)
        return {
            "configId": value.config_id,
            "name": value.name,
            "kind": value.kind,
            "version": value.version,
            "host": (
                "Embodit local · " if value.host.connection == "local" else ""
            ) + f"{value.host.user}@{value.host.address}:{value.host.port}",
            "valid": True,
        }
