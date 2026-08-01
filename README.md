<h1 align="center">Embodit</h1>

<p align="center">
  <img src="images/Embodit_logo.png" alt="Embodit Logo" width="160" />
</p>

<p align="center">
  <b>Embodied Intelligence Toolkit</b><br />
  A local visual workspace for robotics and VLA datasets
</p>

<p align="center">
  <a href="#supported-formats"><img src="https://img.shields.io/badge/formats-LeRobot%20%7C%20HDF5%20%7C%20MCAP-0ea5e9" alt="formats" /></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="python" /></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/runtime-uv-green" alt="uv" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license" /></a>
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">中文</a>
</p>

Embodit opens local robotics datasets directly in the browser for episode inspection, manual QA, annotation, filtering, conversion, export, and visual augmentation. Data stays on the local machine by default; no third-party upload is required.

<!-- > Original sample payloads remain read-only. Reviews and labels use sidecar files; exports, conversions, and augmentations are written to new directories. -->

## Core capabilities

| Module | Capabilities |
|---|---|
| **Browse** | Format auto-detection, synchronized multi-camera playback, episode and task search |
| **Review & annotate** | Quality scores, success/failure, tags, notes, and interval labels (`B` hotkey) |
| **Automatic QC & filter** | Hard integrity validation, video/motion/manipulation findings, interval evidence, 0–100 scores, review, and filtered export |
| **Convert & export** | Four-format conversion, same-format subset export, background jobs, and fidelity reports |
| **Visual augment** | Automatic/manual brightness, SAM3 object recoloring and background replacement, preview-gated batch output |

### Automatic QC and visual review

Click “Auto QC” in the **Filter** workspace to start a detached scan. Completed results appear directly in the filtering UI instead of requiring users to read a long text report:

- the episode list shows usable duration and finding count, and the header shows the current QC summary;
- colored segments mark flagged time ranges on the main timeline; clicking one plays exactly that range and stops at its end;
- finding cards show issue code, severity, time, camera/signal, confidence, and a concise explanation;
- raw metrics and effective thresholds expand only when a finding is selected; findings can be confirmed or rejected in place;
- global results remain filterable by issue, integrity, automatic/manual decision, minimum quality, and minimum usable duration, with dataset and CSV export.

```text
Episode 12 · quality 86.4 · usable 93.1% · review
00:00  ────────── [ motion/jitter ] ───── [ visual/blur ] ─────  00:28
                     click to play              inspect evidence
```

#### Understanding the result

Automatic QC is not a single pass/fail bit. Each episode has the following independent fields:

| Field | Meaning |
|---|---|
| `integrityStatus` | `valid` means structurally usable; `invalid` means a hard integrity failure and forced export exclusion |
| `severity` | `warning` is advisory, `error` is a definite interval-quality problem, and `fatal` is severe |
| `usableRatio` | Percentage of duration not covered by `error` / `fatal` intervals; fixed to `0` for hard-invalid episodes |
| `qualityScore` | `0–100`, based on severity, confidence, and affected duration; fixed to `0` for hard-invalid episodes |
| `coverage` | Weight share of detectors that completed; missing fields that make detectors skip reduce coverage |
| `autoDecision` | `pass`, `review`, or `quarantine`; a manual episode decision can override it |

A `warning` or candidate does not mean automatic quarantine. Ordinary `error` findings default to `review`; only hard integrity failures or `fatal` findings automatically produce `quarantine`.

#### Hard unusable criteria

The following findings set `hard_invalid=true`, `integrityStatus=invalid`, `qualityScore=0`, `usableRatio=0`, and `autoDecision=quarantine`. They are forcibly excluded from QC-filtered export even if a manual episode decision says pass.

| Issue code | Default criterion |
|---|---|
| `integrity/missing_action` | `requirements.action=true` (default) but `action` is absent |
| `integrity/missing_state` | “Require state data” is enabled but `observation.state` is absent |
| `integrity/invalid_signal_shape` / `empty_signal` / `non_numeric_signal` | action/state cannot become a 2-D time series, has an empty axis, or is non-numeric |
| `integrity/non_finite_signal` | any action/state value is `NaN` or `Inf`; the report records its time span and dimensions |
| `integrity/signal_read_error` | the dataset adapter cannot read action/state for the episode |
| `integrity/signal_length_mismatch` | signal length differs from episode frames by more than `max(2 frames, episode frames × 10%)` |
| `integrity/action_state_length_mismatch` | action/state lengths differ by more than `max(2 frames, longer signal × 10%)` |
| `integrity/missing_required_camera` | camera count is below `minimumCameras=1`, or a configured `requiredCameraKeys` entry is missing |
| `integrity/missing_required_camera_pattern` | no camera key matches configured `requiredCameraPatterns` |
| `integrity/video_source_unavailable` / `video_decode_error` / `empty_video` | no frame source, a decode exception, or zero decodable frames |
| `integrity/video_frame_count_mismatch` | decoded / expected frame ratio is below `minimumDecodedRatio=0.9` |
| `integrity/video_frozen` | freeze intervals are detected by the rule below and their union reaches `50%` of episode duration |

#### Interval quality and behavior criteria

These findings appear as evidence ranges on the timeline. Unless stated otherwise they do not make the structure `invalid`; they affect usable duration, quality, and automatic decision.

| Issue code | Severity | Default criterion | Profiles |
|---|:---:|---|---|
| `visual/frozen` | error | mean absolute grayscale pixel delta between sampled frames `≤ 0.75` for `≥ 2.0s`; total freeze remains below `50%` | all |
| `visual/dark` | warning | grayscale mean `< 40` for `≥ 0.5s` | standard, deep |
| `visual/overexposed` | warning | grayscale mean `> 245` for `≥ 0.5s` | standard, deep |
| `visual/blur` | warning | grayscale Laplacian variance `< 35` for `≥ 0.5s` | standard, deep |
| `visual/camera_shake` | error | static-camera global optical-flow vector change `> 4.0` and uniform-motion pixel ratio `≥ 0.45` | standard, deep |
| `motion/jitter` | error | action by default, falling back to state only without action. Dimension P99–P1 range must be `≥ 0.001`; acceleration robust-z `> 8` and range ratio `> 0.15`; jerk robust-z `> 8` and range ratio `> 0.30`; both hit the same dimension within `±1` frame for `≥ 0.08s` | all |
| `motion/stationary` | warning | maximum per-frame absolute delta across aligned action/state is `≤ 0.0005` for `≥ 3.0s` | all |
| `motion/near_zero_episode` | error | maximum full-episode activity range across all usable action/state dimensions is `< 0.01` | all |
| `manipulation/gripper_chatter` | error | dimensions named like `gripper` / `finger` / `jaw` have `≥ 0.2` frame transitions at more than `4/s` | all |
| `manipulation/regrasp_candidate` | warning | at least three gripper transitions within `5s` with alternating close–open–close direction; enabled when `allowMultipleGrasps=false` | all |

Notes:

- Jitter combines statistical outliers, signal-relative magnitude, and joint acceleration/jerk evidence so floating-point noise in nearly static dimensions is not amplified. Measured state dynamics are not double-counted as command jitter by default.
- Camera shake only applies to configured static cameras. Without `staticCameraKeys`, keys containing `base`, `head`, `main`, `front`, or `overhead` and not containing `wrist`, `hand`, or `eef` are inferred as static.
- Gripper checks depend on feature names. Datasets with anonymous dimensions such as `command.0` should extend `gripper.namePatterns`; otherwise the detector is reported as skipped.
- Defaults are conservative cross-dataset starting points, not robot-independent physical safety limits. Calibrate with known-good and known-bad samples when units, control rate, or exposure differ substantially.

#### Scoring and automatic decision

Intervals use their union, so overlapping findings do not double-subtract usable duration:

```text
usableRatio = 100 × (episode duration - union of error/fatal intervals) / episode duration
qualityScore = 100 - duration-weighted interval penalty - episode-level penalty
```

Default severity weights are `info=0`, `warning=0.25`, `error=1.0`, and `fatal=1.0`, multiplied by confidence. Overlapping findings use the maximum active weight rather than the sum. Global findings without an interval contribute at most `5` / `25` / `100` points for `warning` / `error` / `fatal`.

| Automatic decision | Condition |
|---|---|
| `pass` | `qualityScore ≥ 80`, `usableRatio ≥ 90%`, `coverage ≥ 80%`, and no `error` findings |
| `quarantine` | any hard-invalid or `fatal` finding |
| `review` | everything else, including warnings, ordinary errors, low coverage, or a score below threshold |

Users can confirm/reject findings or override the episode decision. Raw detection, human review, and adjusted intervals remain separate for auditability.

#### Profiles, performance, and cache

| Profile | Differences |
|---|---|
| fast | full decode validation; freeze at `3 FPS / 128px`; skips visual quality and optical-flow shake; `4 × 2` workers by default |
| standard | freeze `5 FPS / 160px`, visual quality `4 FPS / 320px`, shake `2 FPS / 320px`; `2 × 2` workers |
| deep | freeze `10 FPS / 240px`, visual quality `8 FPS / 480px`, shake `4 FPS / 480px`; `2 × 2` workers |

Every profile runs signal integrity, video decodability, freeze, motion, and gripper checks. Unsampled MP4 frames need not be converted to RGB. Effective episode concurrency is reduced to fit the CPU budget, while SQLite writes remain serialized. Override `runtime.episodeWorkers` / `runtime.cameraWorkers` from the UI or config in the `1–16` range.

Each scan is an independent SQLite report under `.embodit_cache/reports/qc/<dataset-id>/` by default. Cache reuse requires both the dataset fingerprint and the complete configuration hash to match; detector threshold or configuration-version changes force a new scan. Set `EMBODIT_CACHE_DIR` to move the unified cache root. See [`config/qc.example.json`](config/qc.example.json) for the complete example.

#### Cache layout and cleanup

All job state and reproducible files live under `.embodit_cache/`:

```text
.embodit_cache/
├── jobs/{convert,augment,qc}/
├── previews/augment/
├── media/{hdf5,mcap}/
├── reusable/sam_tracks/
└── reports/qc/
```

At startup, Embodit moves content from the legacy `.convert_jobs`, `.augment_jobs`, `.augment_previews`, `.augment_cache`, and `/tmp/embody-*-video` locations into the unified cache without leaving compatibility symlinks, then applies retention once. A long-running service repeats maintenance every 24 hours by default. Defaults remove previews and playback media unused for 7 days, SAM3 cache entries unused for 30 days, and terminal jobs older than 30 days. At least 5 recent QC reports per dataset are retained; reports still referenced by jobs are protected.

```bash
bash embodit.sh clean --dry-run  # inspect default retention without writing
bash embodit.sh clean            # remove expired content
bash embodit.sh clean --cache    # clear all reproducible preview/media/SAM caches
bash embodit.sh clean --all      # also remove job history and QC reports
```

Stop the service before a real cleanup. These commands never remove dataset outputs, annotations, review sidecars, `.venv`, the service log, or the access token. Output-adjacent `.*.building-*` staging must remain on the output filesystem for atomic commits and is automatically removed after normal success, failure, or cancellation.

## Screenshots

### Dataset overview

Inspect dataset metadata, episodes, synchronized camera streams, and state/action trajectories in one workspace.

<p align="center">
  <img src="images/overview.png" alt="Embodit dataset overview" width="100%" />
</p>

<table>
  <tr>
    <td width="50%"><img src="images/annotation.png" alt="Embodit annotation workspace" /></td>
    <td width="50%"><img src="images/augmentation.png" alt="Embodit augmentation preview" /></td>
  </tr>
  <tr>
    <td align="center"><b>Review and annotation</b><br />Capture scores, tags, notes, and interval labels</td>
    <td align="center"><b>Data augmentation</b><br />Compare source and transformed results before batch output</td>
  </tr>
</table>

## Quick start

### Requirements

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/eddyLfan/Embodit.git
cd Embodit
```

### Start the service

```bash
bash embodit.sh start /path/to/your/data
```

On first run, Embodit creates `.venv` and installs the versions pinned by `pyproject.toml` and `uv.lock`. When ready, it prints a tokenized URL:

```text
http://localhost:8765/?token=<random>
```

The first request exchanges the token for an HttpOnly cookie. Subsequent visits can use `http://localhost:8765/` directly.

```bash
bash embodit.sh stop     # Stop the service
bash embodit.sh logs -f  # Follow logs
bash embodit.sh help     # Show all commands
```

## Workflow

1. Select a data path; Embodit detects the format and episodes.
2. Run automatic QC in Filter and play each flagged timeline interval to review its evidence.
3. Inspect synchronized cameras, state, and action; add scores, tags, and notes.
4. Mark episodes as `pass`, `review`, or `quarantine`.
5. Export the filtered subset or convert it to another format.
6. Optionally preview a visual transform and write an augmented dataset.

## Supported formats

| Format | Typical supported layout |
|---|---|
| **LeRobot v2.1** | Directory with parquet, video, and metadata |
| **LeRobot v3** | LeRobot v3 directory dataset |
| **HDF5** | RoboMimic-style `.hdf5` / `.h5` file or directory |
| **MCAP** | Single file, top-level files, or one shard-directory level |

Conversion matrix (rows: source; columns: target):

| → | LeRobot v2.1 | LeRobot v3 | HDF5 | MCAP |
|---|:---:|:---:|:---:|:---:|
| **LeRobot v2.1** | full | high | partial | partial |
| **LeRobot v3** | high | full | partial | partial |
| **HDF5** | partial | partial | full | partial |
| **MCAP** | partial | partial | partial | full |

- `full`: lossless same-format subset export
- `high`: primary data is preserved; layout or metadata may change
- `partial`: fields, calibration, or format-specific metadata may be lost

The UI shows known losses and records them in `conversion_report.json`. See [`config/convert.example.json`](config/convert.example.json) for field and topic mappings.

## Configuration

### Environment variables

| Variable | Purpose | Default |
|---|---|---|
| `EMBODY_ROOT` | Data browse root | Launch argument or current directory |
| `EMBODY_HOST` | Bind address | `127.0.0.1` |
| `EMBODY_PORT` | Service port | `8765` |
| `EMBODY_PUBLIC_HOST` | Hostname printed in the terminal URL | `localhost` |
| `EMBODY_TOKEN` | Access token | `config/token` or randomly generated |
| `EMBODY_PROXY` | HTTP(S) proxy used by `uv` | Empty |
| `EMBODIT_SANDBOX` | Restrict accessible paths to the browse root | Off; set to `1` |
| `EMBODIT_CACHE_DIR` | Unified cache and job-state root | `.embodit_cache` |
| `EMBODIT_PREVIEW_TTL_DAYS` | Preview job and asset retention | `7` |
| `EMBODIT_MEDIA_TTL_DAYS` | HDF5/MCAP playback cache retention | `7` |
| `EMBODIT_SAM_CACHE_TTL_DAYS` | SAM3 segmentation cache retention | `30` |
| `EMBODIT_JOB_TTL_DAYS` | Terminal job record retention | `30` |
| `EMBODIT_TEMP_TTL_DAYS` | Crash-temp and orphan-preview retention | `1` |
| `EMBODIT_QC_REPORTS_PER_DATASET` | Recent QC reports retained per dataset | `5` |
| `EMBODIT_MAINTENANCE_INTERVAL_HOURS` | Automatic maintenance interval; `0` disables periodic runs | `24` |
| `EMBODIT_HDF5_FPS` | Fallback FPS for HDF5 | `20` |
| `EMBODIT_MCAP_GAP_S` | Time gap used to split MCAP episodes, in seconds | `2` |
| `AUGMENT_PYTHON` | Python interpreter for the SAM3 worker | Current interpreter |
| `AUGMENT_SAM3_CHECKPOINT` | SAM3 checkpoint path | `checkpoints/sam3.pt` |

Legacy `LEROBOT_*` variables remain aliases for their `EMBODY_*` equivalents.

### LAN access

The service binds to localhost by default. For LAN access, use a strong fixed token and restrict the endpoint with a firewall or reverse proxy:

```bash
EMBODY_HOST=0.0.0.0 \
EMBODY_PUBLIC_HOST=<server-ip> \
EMBODY_TOKEN=<strong-random-token> \
bash embodit.sh start /path/to/your/data
```

## Data augmentation

Brightness, mask recoloring, and solid-background effects are built in. Brightness works out of the box. Color augmentation additionally requires Meta SAM3, a compatible PyTorch/CUDA environment, and an authorized checkpoint.

```bash
# In a separate Python 3.12 environment, install PyTorch for the host CUDA version, then:
git clone https://github.com/facebookresearch/sam3.git ../sam3
python -m pip install -r config/augment-worker-requirements.txt
python -m pip install -e ../sam3

export AUGMENT_PYTHON=/path/to/sam3-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/your/data
```

Follow [`facebookresearch/sam3`](https://github.com/facebookresearch/sam3) for current requirements and checkpoint access. See [`third_party/README.md`](third_party/README.md) for licensing notes. If SAM3 is unavailable, Embodit disables color augmentation and reports the missing dependency without affecting other features.

Every batch augmentation must reference a successful preview with identical transform settings. Changing brightness, prompts, color, apply mode, or GPU invalidates the preview.

### Augment output fidelity

Augmentation writes LeRobot v2.1 or v3 and preserves:

- camera videos
- `observation.state`, when available
- `action`, when available
- episode tasks

Custom tabular fields, calibration, source statistics, and format-specific metadata are not copied. Videos are re-encoded as H.264. Augment output therefore has `partial` fidelity, documented in `meta/augmentation_report.json`.

## Data writes and security

Embodit does not modify original episode payloads, but review and annotation sidecars are written next to the source:

```text
<source>/
  <name>.review.json      # manual decisions for directory datasets
  labels.jsonl            # scores and annotations

  # single-file HDF5 / MCAP:
  <file>.review.json
  <file>.labels.jsonl
```

Exports, conversions, and augmentations must use a new path outside the source dataset:

```text
<output>/
  selection_manifest.json
  conversion_report.json          # conversion fidelity and known losses
  meta/augmentation_report.json   # augmentation fidelity and known losses
```

The output path must not equal or reside inside the source dataset.

## Development and testing

```bash
uv sync --extra dev
uv run pytest -q

uv run python backend/app.py \
  --host 127.0.0.1 \
  --port 8765 \
  --browse-root /path/to/data \
  --token dev-token
```

## Project layout

```text
.
├── backend/
│   ├── app.py          # FastAPI entry point
│   ├── datasets/       # detection, adapters, frame access, and export
│   ├── labels/         # annotation schema and storage
│   ├── convert/        # conversion pipeline and background jobs
│   ├── augment/        # built-in effects and SAM3 adapter
│   └── qc/             # automatic QC detectors, scoring, SQLite reports, and jobs
├── web/                # browser UI and EN/ZH strings
├── tests/              # automated tests
├── config/             # example mappings and worker requirements
├── checkpoints/        # optional SAM3 weights (not tracked by Git)
├── embodit.sh          # service lifecycle CLI
├── pyproject.toml
└── uv.lock
```

## Version and contributing

Current version: **v0.1.0**.

Issues and pull requests are welcome. Include the affected data format, reproduction steps, and expected behavior.

## License

Embodit is released under the [MIT License](LICENSE). SAM3 and other third-party components remain subject to their respective licenses.
