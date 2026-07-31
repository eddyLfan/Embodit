<h1 align="center">Embodit</h1>

<p align="center">
  <img src="images/Embodit_logo.png" alt="Embodit Logo" width="160" />
</p>

<p align="center">
  <b>Embodied Intelligence Toolkit</b><br />
  A local visual workspace for robotics and VLA datasets
</p>

<p align="center">
  <a href="#features"><img src="https://img.shields.io/badge/formats-LeRobot%20%7C%20HDF5%20%7C%20MCAP-0ea5e9" alt="formats" /></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/python-%3E%3D3.10-blue" alt="python" /></a>
  <a href="#quick-start"><img src="https://img.shields.io/badge/runtime-uv-green" alt="uv" /></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/license-MIT-yellow" alt="license" /></a>
</p>

<p align="center">
  <b>English</b> | <a href="README.zh-CN.md">中文</a>
</p>

Embodit opens local datasets directly in your browser for episode inspection, manual QA, annotation, filtering, format conversion, and visual augmentation. **Original sample content stays read-only**: reviews and labels are stored in sidecar files, while exports, conversions, and augmentations are written to new directories.

> Data stays on your machine by default. No third-party upload is required.

**Documentation:** [Features](#features) · [Typical workflow](#typical-workflow) · [Supported formats](#supported-formats) · [Quick start](#quick-start) · [Configuration](#configuration) · [Data write policy](#data-write-policy) · [Updates](#updates)

## Features

| Module | Description |
|---|---|
| **Browse** | Auto-detect formats; multi-camera synced timeline preview; search by episode / task |
| **Annotate** | Episode quality score, success/fail, tags and notes; multi-breakpoint interval labels (hotkey `B`) |
| **Filter** | Manual decisions: `pass` / `review` / `quarantine`; bulk update and export-time filtering |
| **Convert** | Convert among four formats in background jobs; emits `conversion_report.json` and fidelity hints |
| **Augment** | Brightness auto/manual; SAM3-based object recolor and background replace (preview, then batch write) |
| **Export** | Default same-format subset export (hardlink/copy); optional convert-on-export |

Auto-filter is reserved; the rule engine ships after the QC standard is finalized.

## Typical workflow

1. **Open a dataset** — point Embodit at a local directory; it detects the format and episodes.
2. **Inspect and review** — view synchronized cameras, states, and actions; record scores and notes.
3. **Annotate and filter** — add interval labels and mark episodes as `pass`, `review`, or `quarantine`.
4. **Export or convert** — export a filtered subset, optionally converting it to another format.
5. **Augment (optional)** — preview brightness, recoloring, or background replacement, then write the result to a new directory.

## Supported formats

| Format | Typical layout |
|---|---|
| **LeRobot v2.1** | Directory dataset (parquet + video / metadata) |
| **LeRobot v3** | Directory dataset |
| **HDF5** | Single file or directory (RoboMimic-style `.hdf5` / `.h5`) |
| **MCAP** | Single file, top-level multi-file, or one nested shard level |

Conversion capability matrix (rows are source formats; columns are target formats):

| → | LeRobot v2.1 | LeRobot v3 | HDF5 | MCAP |
|---|:---:|:---:|:---:|:---:|
| **LeRobot v2.1** | full | high | partial | partial |
| **LeRobot v3** | high | full | partial | partial |
| **HDF5** | partial | partial | full | partial |
| **MCAP** | partial | partial | partial | full |

Fidelity levels: `full` means a lossless same-format subset export, `high` preserves the primary data, and `partial` may lose fields, calibration, or metadata. Exact losses are shown in the UI and recorded in `conversion_report.json`.

## Quick start

### 1. Prerequisites

You only need:

- Python 3.10 or newer
- [uv](https://docs.astral.sh/uv/getting-started/installation/)

```bash
# Install uv if needed
curl -LsSf https://astral.sh/uv/install.sh | sh

git clone https://github.com/eddyLfan/Embodit.git
cd Embodit
```

### 2. Start

```bash
bash start.sh /path/to/your/data
```

On first run, Embodit creates `.venv` and installs the versions pinned by `pyproject.toml` and `uv.lock`. When the server is ready, the terminal prints a tokenized URL:

```text
http://localhost:8765/?token=<random>
```

Open this URL in your browser. On first visit, the token is exchanged for an HttpOnly cookie; afterwards, `http://localhost:8765/` works without it.

### 3. Manage the service

```bash
bash stop.sh          # Stop the service
tail -f service.log   # Follow logs
bash start.sh --help  # Show launch options
```

Browse / annotate / filter / convert work out of the box. **Augment** (brightness / SAM3 recolor) is optional — see [Optional: augment](#optional-augment) below.

## Configuration

### Dependency environments

| Layer | What |
|---|---|
| **Core runtime** | Declared in `pyproject.toml`, locked in `uv.lock`. `start.sh` runs `uv sync` then `uv run`. |
| **Dev extras** | `uv sync --extra dev` (optional tooling). |
| **Augment (optional)** | External algorithm tree + optional Torch/CUDA env — not part of the core install. |

### Environment variables

| Variable | Meaning | Default |
|---|---|---|
| `EMBODY_ROOT` | Browse root | Launch arg, else current directory |
| `EMBODY_HOST` | Bind address | `127.0.0.1` |
| `EMBODY_PORT` | Port | `8765` |
| `EMBODY_PUBLIC_HOST` | Hostname printed in the URL | `localhost` |
| `EMBODY_TOKEN` | Access token | Persisted in `config/token`; else randomly generated |
| `EMBODY_PROXY` | HTTP(S) proxy for `uv` downloads | Off (empty) |
| `EMBODIT_SANDBOX` | Restrict paths to the browse root | Off; set `1` to enable |
| `EMBODIT_HDF5_FPS` | Default HDF5 FPS | `20` |
| `EMBODIT_MCAP_GAP_S` | Gap (seconds) used to split MCAP into episodes | `2` |

Legacy aliases: `LEROBOT_*` mirrors the corresponding `EMBODY_*` variables.

The server binds to `127.0.0.1` by default. For LAN access, set `EMBODY_HOST=0.0.0.0`, use a strong fixed `EMBODY_TOKEN`, and restrict access with a firewall or reverse proxy:

```bash
EMBODY_HOST=0.0.0.0 \
EMBODY_PUBLIC_HOST=<server-ip> \
EMBODY_TOKEN=<strong-random-token> \
bash start.sh /path/to/your/data
```

### Optional: augment

Color / brightness augmentation loads algorithms from a separate package. See [`third_party/README.md`](third_party/README.md) for the expected layout:

```bash
export EMBODIT_AUGMENT_ROOT=/path/to/data_strengthen   # must contain augment/
export AUGMENT_PYTHON=/path/to/torch-env/bin/python    # needed for SAM3 color
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt        # or place at checkpoints/sam3.pt
bash start.sh /path/to/your/data
```

Without this setup, browsing, annotation, filtering, and conversion still work; only the Augment tab is unavailable.

## Data write policy

Embodit never rewrites episode payloads, but it does write review and annotation sidecars next to the source:

```text
<source>/
  <name>.review.json      # manual review decisions (directory datasets)
  labels.jsonl            # scores and annotations
  # single-file HDF5 / MCAP:
  #   <file>.review.json
  #   <file>.labels.jsonl
```

Export / convert / augment results go to a **new path**, e.g.:

```text
export_or_converted/
  selection_manifest.json
  conversion_report.json   # on convert: mappings, known losses, etc.
```

For cross-format field and topic mapping, see [`config/convert.example.json`](config/convert.example.json). Keep output paths outside the source dataset to avoid mixing generated and original data.

## Project layout

```text
.
├── start.sh / stop.sh     # one-command start/stop (uv sync + uv run)
├── pyproject.toml         # core dependencies
├── uv.lock                # locked versions
├── web/                   # browser UI (EN/ZH toggle)
├── backend/
│   ├── app.py             # FastAPI entry
│   ├── settings.py        # env-overridable settings
│   ├── datasets/          # format detection, adapters, frames, export
│   ├── labels/            # label schema and JSONL store
│   ├── convert/           # conversion pipeline and background jobs
│   ├── augment/           # visual augment (optional external algos)
│   └── qc/                # auto-filter placeholder
├── config/                # token (gitignored), convert example
├── checkpoints/           # optional SAM3 weights
├── third_party/           # optional augment algorithm checkout
├── images/                # logo and documentation assets
└── LICENSE
```

## Development

```bash
uv sync
uv run python backend/app.py --host 127.0.0.1 --port 8765 \
  --browse-root /path/to/data --token=dev-token
```

## Design principles

1. **Source data is read-only** — review and labels land as sidecars; convert / augment write new datasets.
2. **Explicit fidelity** — cross-format conversion surfaces `full` / `high` / `partial` and known losses in the UI.
3. **Long jobs outlive the browser** — convert and augment run as detached processes; closing the tab does not cancel them.
4. **Lightweight startup** — core deps come from `uv sync`; SAM3 / Torch load only when Augment is configured.

## Updates

Current release: **v0.1.0** (first public open-source release).

| Version | Date | Highlights |
|---|---|---|
| **v0.1.0** | 2026-07 | Browse / annotate / filter / convert / export for LeRobot v2.1 & v3, HDF5, and MCAP; optional brightness and SAM3 visual augment; EN/ZH UI and docs; one-command startup via `uv` (`start.sh`) |

Later changes will be recorded in this section (and in GitHub Releases when available).

## Contributing

Issues and pull requests are welcome. Please describe the formats and repro paths involved.

## License

Released under the [MIT License](LICENSE).
