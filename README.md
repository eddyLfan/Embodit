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
| **Filter** | `pass` / `review` / `quarantine` decisions, bulk actions, and status-based export |
| **Convert & export** | Four-format conversion, same-format subset export, background jobs, and fidelity reports |
| **Visual augment** | Automatic/manual brightness, SAM3 object recoloring and background replacement, preview-gated batch output |

The automatic filtering interface is reserved; the rule engine is not yet available.

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
bash start.sh /path/to/your/data
```

On first run, Embodit creates `.venv` and installs the versions pinned by `pyproject.toml` and `uv.lock`. When ready, it prints a tokenized URL:

```text
http://localhost:8765/?token=<random>
```

The first request exchanges the token for an HttpOnly cookie. Subsequent visits can use `http://localhost:8765/` directly.

```bash
bash stop.sh          # Stop the service
tail -f service.log   # Follow logs
bash start.sh --help  # Show launch options
```

## Workflow

1. Select a data path; Embodit detects the format and episodes.
2. Inspect synchronized cameras, state, and action; add scores, tags, and notes.
3. Mark episodes as `pass`, `review`, or `quarantine`.
4. Export the filtered subset or convert it to another format.
5. Optionally preview a visual transform and write an augmented dataset.

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
bash start.sh /path/to/your/data
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
bash start.sh /path/to/your/data
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
│   └── qc/             # reserved automatic filtering module
├── web/                # browser UI and EN/ZH strings
├── tests/              # automated tests
├── config/             # example mappings and worker requirements
├── checkpoints/        # optional SAM3 weights (not tracked by Git)
├── start.sh / stop.sh  # service lifecycle
├── pyproject.toml
└── uv.lock
```

## Version and contributing

Current version: **v0.1.0**.

Issues and pull requests are welcome. Include the affected data format, reproduction steps, and expected behavior.

## License

Embodit is released under the [MIT License](LICENSE). SAM3 and other third-party components remain subject to their respective licenses.
