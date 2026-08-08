# Architecture and Extension Boundaries

**English** · [中文](architecture.zh-CN.md)

Embodit is a local FastAPI service with a dependency-free static web client,
format adapters, detached data workers, and a separate robot-deployment control
plane. This document describes ownership and dependency direction; user-facing
behavior belongs in the data and deployment guides.

## Repository map

```text
Embodit/
├── backend/
│   ├── app.py                  # HTTP/auth/path boundary and route composition
│   ├── settings.py             # Environment-derived service settings
│   ├── jobs_common.py          # Shared atomic detached-job state
│   ├── cache_manager.py        # Retention, cleanup, and legacy migration
│   ├── datasets/               # Detection, normalized views, readers, writers
│   ├── qc/                     # Detectors, scoring, SQLite reports, worker
│   ├── convert/                # Fidelity registry, pipeline, report, worker
│   ├── merge/                  # Strict preflight and same-format merge
│   ├── augment/                # Preview, effects, SAM3 bridge, output, worker
│   ├── labels/                 # Label schema and fixed JSONL sidecars
│   └── deploy/                 # Config/Recipe validation and orchestration
│       └── assets/             # Standalone runtimes uploaded to managed hosts
├── web/                        # Static HTML/CSS/JS client and regressions
├── config/                     # Versioned data/deployment templates
├── examples/deployment/        # Copyable model and robot integration examples
├── docs/                       # English/Chinese user and architecture guides
├── tests/                      # Synthetic Python regression suite
├── third_party/models/         # Pinned upstream gitlinks; no model weights
└── embodit.sh                  # Supported service/deployment entry point
```

Runtime state is deliberately outside the source model:

- `.embodit/`: PID, URL, bearer token, environment fingerprint, and service log;
- `.embodit_cache/`: media, previews, jobs, QC reports, and deployment state;
- `config/local/*.json`: non-recursively discovered private Robot/Model Configs.

All three paths are ignored by Git and may contain sensitive information.

## Dependency direction

```text
web → backend/app.py → feature modules

qc / convert / merge / augment → datasets
feature workers → jobs_common + their feature pipeline

deploy/orchestrator → recipe + store + transport + deploy/assets
deploy/assets → remote Python/ROS/provider environments
```

- Feature modules must not import `app.py`.
- Format-specific parsing and publication belong in `datasets/`; generic
  workflows consume normalized views/payloads.
- `app.py` authenticates, confines client paths, validates request-level input,
  and dispatches. It must not become a second implementation of domain logic.
- The browser is a view/controller, not the authoritative job, QC, or hardware
  safety state store.
- Uploaded deployment assets remain standalone and must not rely on the
  workstation's Python import path.
- Provider integrations refer to pinned upstream submodules; Embodit does not
  copy their implementations or bundle checkpoints.

## Data and state model

Dataset adapters expose a normalized `DatasetView`, episode payloads, media, and
format-native export hooks. Cross-format conversion writes only recognized or
explicitly mapped fields; it must report fidelity limits instead of implying
container-lossless output.

Three user states remain independent:

| State | Owner | Persistence |
|---|---|---|
| Browse decisions | review API/web workspace | Dataset-bound `*.review.json` |
| QC decisions/findings | `qc/store.py` | Scan-specific SQLite report |
| Semantic labels | `labels/` | Fixed dataset JSONL sidecar |

An explicit workflow may use one state as an input selection, but modules must
not silently synchronize or overwrite the others.

## Work and publication model

Conversion, QC, merge, and augmentation batch work runs in detached workers.
`jobs_common.py` supplies locked, atomic state updates so late workers cannot
resurrect a cancelled or terminal job. Request-level inspection and media
materialization may still perform bounded filesystem or codec work and should
be moved off the async event loop when blocking.

Writers must reject existing outputs, validate path components, use unique
job-scoped staging where supported, and publish only after validation. Each
format documents its actual atomicity boundary; callers must not assume every
container writer is transactional.

## Deployment boundary

Deployment is a control plane. Embodit composes reusable Robot and Model Configs
into a Recipe, performs preflight, manages SSH/systemd/tunnels, and records
orchestration state. During Dry Run or Live, the real-time observation/inference/
action loop runs between the robot-side client and model runner rather than
through FastAPI.

Offline evaluation is intentionally out-of-band: it sends one recorded dataset
frame to an already prepared model and compares the result with recorded action
data without connecting robot control. It shares model single-flight protection
with the orchestration.

Recipe/config/provider/adapter inputs are trusted executable control material.
Software validation, limits, and emergency stop orchestration complement—but do
not replace—an independent hardware safety chain.

## Adding functionality

| Goal | Primary location | Required companion work |
|---|---|---|
| Dataset format | `backend/datasets/` | Detection, view/payload, fidelity docs, real-format tests |
| QC detector | `backend/qc/detectors/` | Versioned config, evidence schema, calibration tests/docs |
| Conversion pair | `backend/convert/` | Capability registry, loss report, failure/publication tests |
| Augmentation | `backend/augment/` | Preview parity, manifest, resource and fidelity documentation |
| Robot/model integration | `backend/deploy/` or `examples/deployment/` | Config validation, fake-runner tests, safety/commissioning guide |
| Web workflow | `web/` | English/Chinese strings, keyboard states, frontend regression |

Keep route handlers thin, preserve documented persisted schemas, and add a
migration when compatibility cannot be maintained.

## Validation

```bash
uv sync --frozen --extra dev
uv run --no-sync pytest -q
python3 -m compileall -q backend
bash -n embodit.sh

node --check web/app.js
node --check web/i18n.js
node --check web/utils.js
node --test web/frontend-regressions.test.js

git diff --check
```

Hardware-facing regressions use fake runners by default: automated tests must
not connect to a real host, power a robot, or send an action. See
[CONTRIBUTING.md](../CONTRIBUTING.md) and the repository CI workflow.
