# Contributing to Embodit

Thank you for improving Embodit. Contributions are welcome across dataset
adapters, QC, conversion, deployment integrations, the web UI,
tests, documentation, and security hardening.

Because this project can process valuable datasets and control robot deployment
workflows, correctness, backward compatibility, and fail-safe behavior take
priority over feature volume.

## Before You Start

- For vulnerabilities, follow [SECURITY.md](SECURITY.md); do not open a public
  issue containing exploit details.
- Search existing issues and pull requests before starting substantial work.
- Discuss new on-disk formats, public API changes, Recipe schema changes, and
  broad architectural changes before implementation.
- Keep real datasets, credentials, private infrastructure, model weights, and
  generated cache/state out of the repository.

## Development Setup

Requirements are Linux, Python 3.10 or newer, `uv`, Git, and Node.js 20 or newer
for frontend checks.

```bash
git clone https://github.com/eddyLfan/Embodit.git
cd Embodit
uv sync --frozen --extra dev
```

The application launcher installs the locked runtime environment without test
dependencies. For interactive use:

```bash
bash embodit.sh setup
bash embodit.sh start /path/to/test-datasets
```

Use synthetic or explicitly shareable fixtures. Never point destructive or
experimental work at the only copy of a dataset or a live production robot.

Optional integrations have separate trust and licensing requirements.
Model-provider submodules and checkpoints are not installed by the core
development setup; see [third_party/README.md](third_party/README.md).

## Repository Map

The main module boundaries are:

| Area | Location | Responsibility |
|---|---|---|
| API and lifecycle | `backend/app.py`, `backend/settings.py` | HTTP boundary, auth, sandbox, service configuration |
| Dataset adapters | `backend/datasets/` | Detection, inspection, normalized payloads, readers/writers |
| Conversion/export | `backend/convert/`, `backend/datasets/export.py` | Fidelity matrix, detached conversion, subset publication |
| Merge | `backend/merge/` | Same-format compatibility and merge publication |
| QC | `backend/qc/` | Detectors, reports, review decisions, queries |
| Labels | `backend/labels/` | Label schema and default JSONL sidecars |
| Deployment | `backend/deploy/` | Config/Recipe validation, transport, orchestration, robot/model clients |
| Web UI | `web/` | Browser state, media, localization, interaction |
| Service entry | `embodit.sh`, `backend/cache_manager.py` | Environment, process state, logs, cache retention |
| Tests | `tests/`, `web/frontend-regressions.test.js` | Python and frontend regression coverage |

Format behavior is documented in [docs/data/README.md](docs/data/README.md), and
deployment behavior in [docs/deployment/README.md](docs/deployment/README.md).
Keep these boundaries explicit:

- feature modules do not import `backend/app.py`; routes authenticate, validate,
  and dispatch instead of reimplementing domain logic;
- format-specific parsing and publication stay under `backend/datasets/`, while
  generic workflows consume normalized views and payloads;
- browse decisions, QC reports, and semantic labels remain independent stores;
- the browser is not the authoritative job or safety state store, and the
  robot–model action loop does not run through FastAPI.

## Implementation Guidelines

### Data and filesystem behavior

- Treat source datasets as read-only. Labels/review metadata use their defined
  sidecars; batch operations publish to new output paths.
- Resolve and validate client paths at the API boundary. Preserve sandbox
  confinement and reject traversal, unsafe identifiers, mismatched sidecars,
  and unexpected file types.
- Do not overwrite existing dataset outputs. Stage on the destination
  filesystem, validate, then publish atomically or with an explicit no-overwrite
  operation.
- Preserve episode ordering, indices/mappings, schemas, dimensions, dtypes,
  timestamps, task text, camera identity, and fidelity warnings unless the
  operation explicitly documents a transformation.
- Stream or batch large datasets. Avoid full-media decode, N+1 queries, and
  unbounded in-memory report/export construction on request threads.

### Jobs and concurrency

- Detached workers must use atomic job-state transitions. Cancellation, pause,
  terminal state, liveness refresh, and process-launch races require tests.
- Do not allow a late worker update to resurrect a cancelled, paused, failed, or
  completed job.
- Keep blocking filesystem, codec, database, subprocess, model, and deployment
  operations off the async event loop.
- Temporary files and locks must be unique per writer and cleaned on failure.

### Security and deployment

- Never log, return, commit, or place in URLs credentials that can be avoided.
  Keep validation and list responses redacted.
- Treat Recipes, provider code, custom adapters, shell commands, and checkpoints
  as trusted executable inputs; do not silently broaden what they may execute.
- Default to `dry_run`, explicit confirmation, least privilege, bounded actions,
  and fail-closed recovery for hardware-facing changes.
- Software action limits and emergency stops do not replace independent robot
  safety systems. Hardware behavior changes need a documented bench-validation
  plan without real secrets or identifying infrastructure.

### Style and compatibility

- Follow the existing Python type-hinted style and keep functions focused.
- Prefer small reusable validators/helpers over duplicated route logic.
- Keep English and Chinese UI strings synchronized in `web/i18n.js`.
- Preserve compatibility for documented formats, v2/v3 review documents,
  persisted job state, cache migrations, and Recipe/config schema versions, or
  document and test an intentional migration.
- Do not perform unrelated formatting or refactors in the same pull request.

## Validation

Run the full Python suite from the repository root:

```bash
uv run pytest -q
```

Report the actual passing/skipped/failed count in the pull request. If coverage
changes, explain why instead of copying an older result from documentation.

Use Node.js 20 or newer for frontend syntax and regression checks:

```bash
node --check web/app.js
node --check web/i18n.js
node --check web/utils.js
node --test web/frontend-regressions.test.js
```

Run the repository-level static checks:

```bash
bash -n embodit.sh
uv run python -m compileall -q backend
git diff --check
```

Also run focused tests while iterating. Changes involving real video, HDF5,
MCAP, SQLite, concurrency, cancellation, filesystem publication, authentication,
or sandboxing should include a regression that exercises the real boundary when
practical, not only a mocked happy path.

Before claiming a performance improvement, provide a reproducible workload,
before/after timing, dataset scale, and any memory or fidelity trade-off. Avoid
benchmarks that depend on private data unless a synthetic equivalent is included.

## Documentation Changes

User-visible behavior must update the relevant English and Chinese documentation
in the same pull request. This includes:

- commands, arguments, environment variables, defaults, state/cache locations,
  and cleanup behavior;
- format detection, mappings, fidelity or metadata loss;
- authentication, sandbox, credential, and network-exposure implications;
- optional dependencies, provider/checkpoint requirements, and licensing;
- Recipe/config schema or robot safety behavior.

Keep examples non-routable and secret-free. Use placeholders such as
`192.0.2.0/24`, `example.invalid`, and `/path/to/...`; never use a real robot host
or a credential-shaped value copied from an environment.

## Pull Request Checklist

A pull request should:

- state the problem, user-visible outcome, and chosen design;
- keep the patch scoped and call out generated or pre-existing changes;
- link the issue or design discussion when one exists;
- include regression tests for fixes and new behavior;
- report the exact Python, frontend, shell, compile, and diff-check commands run;
- document skipped checks and why they could not run;
- describe compatibility, migration, performance, security, and hardware-safety
  impact where relevant;
- update English/Chinese docs and UI localization where behavior changes;
- contain no credentials, private paths, private data, large checkpoints,
  runtime state, caches, logs, or unrelated lockfile changes;
- remain reviewable: separate broad refactors from behavior changes and avoid
  hiding logic changes in mechanical rewrites.

Maintainers may ask for narrower scope, additional failure-path tests, synthetic
fixtures, migration coverage, security review, or hardware validation evidence
before merging.

## 中文说明

贡献前请使用 `uv sync --frozen --extra dev` 安装开发环境，完成全量 Python
测试、Node.js 20 前端检查、Shell 语法、`compileall` 和 `git diff --check`，并报告实际结果。任何
数据读写、认证、沙箱、并发任务和真机部署变更都需要失败路径回归测试。
请勿提交真实数据、密钥、设备地址、日志、缓存或模型权重；软件急停不能替代
独立硬件安全系统。
