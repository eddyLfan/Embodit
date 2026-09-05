#!/usr/bin/env python3
"""Generate validated, task-specific Embodit model/recipe files without starting services."""
import argparse
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from lingbot_vla_embodit import TASKS, task_assets, validate_checkpoint, validate_training_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--task", choices=list(TASKS))
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--checkpoint", type=Path, help="Exact downloaded hf_ckpt or global_step_N directory")
    parser.add_argument("--embodit-root", type=Path, default=ROOT.parents[2])
    parser.add_argument("--allow-missing", action="store_true", help="Prepare config before weights arrive")
    parser.add_argument("--force", action="store_true", help="Explicitly overwrite existing generated config files")
    args = parser.parse_args()
    if args.all == bool(args.task) or (args.all and args.checkpoint):
        parser.error("Choose --task NAME [--checkpoint PATH] or --all")
    embody = args.embodit_root.resolve()
    tasks = list(TASKS) if args.all else [args.task]
    out = embody / "config/local"
    for task in tasks:
        config, norm, tokenizer = task_assets(task)
        validate_training_config(config, task)
        checkpoint = args.checkpoint or embody / "checkpoints/test/lingbot-vla-v2" / task / "hf_ckpt"
        checkpoint = checkpoint.expanduser().resolve()
        if not args.allow_missing:
            checkpoint = validate_checkpoint(checkpoint)
        slug = "lingbot-vla-v2-" + task.replace("_", "-")
        model_file, recipe_file = out / f"{slug}.model.json", out / f"{slug}.recipe.json"
        if not args.force and (model_file.exists() or recipe_file.exists()):
            raise FileExistsError(f"Existing configuration: {slug}; use --force only to replace it")
        model = {
            "version": 1, "kind": "model", "config_id": slug, "name": f"LingBot VLA v2 · {task}",
            "host": {"connection": "local", "address": "192.168.0.100", "port": 22,
                     "user": "acting", "service_manager": "user"},
            "model": {"workdir": str(ROOT), "source_path": str(ROOT),
                      "provider": "python", "entrypoint": "lingbot_vla_embodit:LingBotVLAAdapter",
                      "checkpoint": str(checkpoint), "python_executable": str(ROOT / ".venv/bin/python"),
                      "environment": {"CUDA_VISIBLE_DEVICES": "0", "TOKENIZERS_PARALLELISM": "false",
                                      "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"},
                      "load_kwargs": {"task": task, "default_prompt": TASKS[task],
                                      "training_config_path": str(config), "norm_stats_path": str(norm),
                                      "tokenizer_path": str(tokenizer), "action_horizon": 50, "use_compile": False},
                      "predict_kwargs": {}, "startup_timeout_s": 900, "restart": "no",
                      "action_horizon": 50, "maximum_request_bytes": 50000000},
            "endpoint": {"bind": "127.0.0.1", "port": 18002},
        }
        model_file.write_text(json.dumps(model, indent=2, ensure_ascii=False) + "\n")
        result = subprocess.run(["bash", "embodit.sh", "recipe-compose",
                                 "config/local/astribot-s1.robot.json", str(model_file),
                                 "--deployment-id", slug, "--name", model["name"]],
                                cwd=embody, capture_output=True, text=True)
        if result.returncode:
            raise RuntimeError(f"Embodit composition failed for {task}; inspect robot/model config locally")
        recipe = json.loads(result.stdout)
        client = recipe["robot"]["client"]["config"]
        client["default_prompt"] = TASKS[task]
        client["task_prompts"] = [TASKS[task]]
        client["action"]["horizon"] = 50
        # Conservative first-test gate; reject large jumps, do not clip predictions.
        limits = [0.08] * 16
        limits[7] = limits[15] = 10.0
        for field in ("max_step", "initial_max_step"):
            prior = client["action"].get(field, limits)
            client["action"][field] = [min(a, b) for a, b in zip(prior, limits)]
        client["control"]["action_steps"] = 10  # recede after 10 of the 50 predicted steps
        recipe["runtime"]["default_mode"] = "dry_run"
        recipe_file.write_text(json.dumps(recipe, indent=2, ensure_ascii=False) + "\n")
        recipe_file.chmod(0o600)  # local recipe inherits robot connection data
        result = subprocess.run(["bash", "embodit.sh", "recipe-validate", str(recipe_file)],
                                cwd=embody, capture_output=True, text=True)
        if result.returncode or not json.loads(result.stdout).get("valid"):
            raise RuntimeError(f"Embodit recipe validation failed for {task}")
        print(json.dumps({"task": task, "model": str(model_file), "recipe": str(recipe_file),
                          "checkpoint_present": checkpoint.is_dir(), "schema_valid": True}))


if __name__ == "__main__":
    main()
