<div align="center">
  <img src="images/Embodit_logo.png" alt="Embodit" width="180">
  <h1>Embodit</h1>
  <p>An integrated toolkit for embodied-AI data workflows and real-robot deployment</p>
  <p><strong>English</strong> · <a href="README.zh-CN.md">中文</a></p>
</div>

## Why we built Embodit

Embodied-AI projects repeatedly lose time to two kinds of infrastructure work. The first is browsing, validating, annotating, converting, merging, and augmenting datasets. The second is real-robot bring-up: logging into the inference server and robot, starting services, creating SSH tunnels, checking ROS, powering on, and launching the inference client.

These procedures often live in temporary scripts, shell history, and individual experience. They are difficult to reproduce, unstable across sessions, and costly to adapt to the next robot. Embodit turns them into inspectable, reusable, safety-first workflows: a data loop from issue discovery to clean export, and a deployment loop from configuration through Dry Run, Live operation, and fault rollback.

## What the toolkit provides

Embodit has two clearly separated layers:

| Layer | Purpose | Detailed guide |
|---|---|---|
| Data layer | Multi-format browsing, synchronized playback, automatic QC, human review, labels, filtering, conversion, merge, visual augmentation, and fidelity-aware export | [Data layer guide](docs/data/README.md) |
| Real-robot deployment layer | Dual-host SSH, model loading, tunneling, ROS bring-up, power-on, initial pose, inference client, Dry Run, Live confirmation, logs, and rollback | [Deployment layer guide](docs/deployment/README.md) |

The deployment layer now has one supported architecture: Recipe v2. For the default Python model provider, users supply a remote Python `entrypoint` and checkpoint, while their model class implements only `load()` and `predict()`. Embodit generates the internal HTTP service, health checks, and process supervision.

Automatic QC is deterministic and configuration-versioned: the detailed guide documents its hard-invalid rules, default visual/motion thresholds, score formulas, pass/review/quarantine decisions, and manual-override behavior. The deployment guide likewise defines exact ROS readiness and robot-side action-safety checks instead of treating startup as a fixed delay.

## UI preview

### Dataset overview and synchronized playback

![Embodit dataset overview](images/overview.png)

### Annotation and interval review

![Embodit annotation workspace](images/annotation.png)

### Data augmentation

![Embodit data augmentation](images/augmentation.png)

The real-robot deployment workspace is available from the top-right “Robot deployment” entry. It provides Recipe editing, Dry Run, component status, logs, Live confirmation, and emergency stop.

## Quick start

Requirements: Linux, Python 3.10+, and [uv](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/eddyLfan/Embodit.git
cd Embodit

# Synchronize the environment and start the service.
# Without DATA_ROOT, Embodit browses the current directory.
bash embodit.sh start /path/to/datasets
```

The terminal prints an access URL containing a temporary token. Common commands:

```bash
bash embodit.sh status
bash embodit.sh logs -f
bash embodit.sh stop

# Validate and start a real-robot deployment Recipe
bash embodit.sh recipe-validate config/deployment.recipe-v2.example.json
bash embodit.sh recipe-run config/deployment.recipe-v2.example.json --mode dry_run
```

For LAN access:

```bash
EMBODY_HOST=0.0.0.0 EMBODY_PUBLIC_HOST=<workstation-ip> \
  bash embodit.sh start /path/to/datasets
```

Read the [data layer guide](docs/data/README.md) before processing datasets and the complete [deployment layer guide](docs/deployment/README.md) before connecting a physical robot.

## Changelog

### 2026-08-04

- Consolidated real-robot deployment on Recipe v2 and removed Profile v1, the legacy Gateway, Mock/SDK Policy flows, and Session APIs.
- Added an Embodit-managed Python model provider; regular users no longer implement `/health` or `/infer`.
- Completed orchestration, monitoring, and reverse rollback for the model, SSH tunnel, ROS, initial pose, and robot client.
- Consolidated deployment persistence and CLI entry points, and moved remotely uploaded runtime assets into `backend/deploy/assets/`.
- Reorganized documentation into the main README, data layer guide, deployment layer guide, and architecture guide.
- Documented the exact QC/filtering, conversion fidelity, merge compatibility, deployment readiness, and action-safety standards in both English and Chinese.

### 2026-08-03

- Added dataset merge, automatic QC, interval evidence, review filtering, and augmentation-preview workflows.
- Added unified browsing and conversion support for LeRobot, RoboMimic HDF5, and MCAP datasets.

## Development and contributing

See the [architecture guide](docs/architecture.md) for module boundaries and extension points. Preferred extension paths:

- New dataset format: implement and register the shared dataset interface in `backend/datasets/`.
- New QC rule: add a detector under `backend/qc/detectors/` while keeping the finding schema stable.
- New augmentation: add the pure algorithm and worker integration in `backend/augment/`, preserving preview and output-fidelity checks.
- New model: implement `load(checkpoint)` and `predict(observations)` and use the Python provider.
- New robot SDK: prefer a thin ROS bridge exposing standard topics, services, and actions instead of changing the deployment control plane.

Before submitting changes:

```bash
UV_CACHE_DIR=/tmp/embodit-uv-cache uv run pytest -q
python3 -m compileall -q backend
bash -n embodit.sh
git diff --check
```

Keep implementation, tests, and documentation in the same change. Any feature capable of issuing physical actions must preserve Dry Run, explicit Live confirmation, limits, watchdog behavior, hold/stop operations, and failure rollback.

## License

[MIT](LICENSE)
