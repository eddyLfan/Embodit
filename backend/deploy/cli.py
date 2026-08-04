#!/usr/bin/env python3
"""Rescue/CI entry point for Recipe deployments."""

from __future__ import annotations

import argparse
import json
import signal
import sys
import time
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

import settings
from deploy.orchestrator import DeploymentOrchestration, OrchestrationState
from deploy.recipe import ModelConfig, RobotConfig, compose_recipe, load_deployment_config, load_recipe, redact_recipe


def print_json(value: object) -> None:
    print(json.dumps(value, ensure_ascii=False, indent=2), flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(prog="embodit-recipe")
    subparsers = parser.add_subparsers(dest="command", required=True)
    validate = subparsers.add_parser("validate", help="校验 Recipe")
    validate.add_argument("recipe")
    compose = subparsers.add_parser("compose", help="组合本体与模型配置并生成 Recipe")
    compose.add_argument("robot_config")
    compose.add_argument("model_config")
    compose.add_argument("--deployment-id", default=None)
    compose.add_argument("--name", default=None)
    compose.add_argument("--output", default=None, help="写入 JSON；省略时输出到 stdout")
    run = subparsers.add_parser("run", help="启动并监控部署")
    run.add_argument("recipe")
    run.add_argument("--mode", choices=("dry_run", "live"), default=None)
    run.add_argument("--no-follow", action="store_true", help="部署就绪后退出，远端 systemd 服务继续运行")
    stop = subparsers.add_parser("stop", help="按 Recipe 逆序停止远端服务")
    stop.add_argument("recipe")
    stop.add_argument("--emergency", action="store_true")
    args = parser.parse_args()

    try:
        if args.command == "compose":
            robot = load_deployment_config(args.robot_config)
            model = load_deployment_config(args.model_config)
            if not isinstance(robot, RobotConfig):
                raise ValueError("第一个文件必须是 kind=robot 的本体配置")
            if not isinstance(model, ModelConfig):
                raise ValueError("第二个文件必须是 kind=model 的模型配置")
            recipe = compose_recipe(
                robot,
                model,
                deployment_id=args.deployment_id,
                name=args.name,
            )
            payload = recipe.model_dump(mode="json")
            if args.output:
                output = Path(args.output).expanduser().resolve()
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                output.chmod(0o600)
                print_json({"written": str(output), "deploymentId": recipe.deployment_id})
            else:
                print_json(payload)
            return

        recipe = load_recipe(args.recipe)
        if args.command == "validate":
            print_json({"valid": True, "recipe": redact_recipe(recipe.model_dump(mode="json"))})
            return

        if getattr(args, "mode", None):
            recipe.runtime.default_mode = args.mode
        item = DeploymentOrchestration(
            recipe,
            settings.CACHE_DIR / "deploy" / "orchestrations" / recipe.deployment_id,
        )
        if args.command == "stop":
            item.state = OrchestrationState.RUNNING
            for component in ("model", "tunnel", "ros", "client"):
                item.components[component] = {
                    "active": True,
                    "unit": None,
                    "host": item.model_host_name if component == "model" else item.robot_host_name,
                }
            item.components["power"] = {"active": True, "unit": None, "host": item.robot_host_name}
            print_json(item.stop(emergency=args.emergency))
            return

        stopping = False

        def request_stop(*_: object) -> None:
            nonlocal stopping
            stopping = True
            item.stop()

        signal.signal(signal.SIGINT, request_stop)
        signal.signal(signal.SIGTERM, request_stop)
        item.start()
        last_signature = None
        while True:
            snapshot = item.snapshot()
            signature = (snapshot["state"], snapshot["currentStep"], len(snapshot["steps"]))
            if signature != last_signature:
                print_json(snapshot)
                last_signature = signature
            if snapshot["state"] == "fault":
                raise SystemExit(1)
            if snapshot["state"] in {"dry_run", "running"} and args.no_follow:
                return
            if snapshot["state"] == "stopped" or stopping:
                return
            time.sleep(0.5)
    except SystemExit:
        raise
    except Exception as error:  # noqa: BLE001
        print(f"deployment recipe error: {error}", file=sys.stderr)
        raise SystemExit(2) from error


if __name__ == "__main__":
    main()
