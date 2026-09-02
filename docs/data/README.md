# Data Workspace Guide

**English** · [中文](README.zh-CN.md)

Embodit provides a local workspace for inspecting, reviewing, labeling,
quality-checking, converting, and merging robot datasets. Source
datasets are treated as read-only; derived datasets are written to a new path.

## 1. Start and path scope

```bash
bash embodit.sh start /path/to/datasets
```

The path is the workspace's initial directory. On localhost it is not a
security boundary: an authenticated client may request other absolute paths
available to the service account. A non-loopback listener automatically enables
`EMBODIT_SANDBOX=1`, which confines client-supplied data paths to this root.
Internal state and cache directories remain separate.

The first start synchronizes the locked **core** environment. CUDA,
provider-specific environments, checkpoints, and system tools are not installed
by this step.

| Variable | Purpose |
|---|---|
| `EMBODY_HOST` / `EMBODY_PORT` | Bind address and port; defaults `127.0.0.1:8765` |
| `EMBODY_PUBLIC_HOST` | Host printed in the browser URL; does not configure networking |
| `EMBODIT_SANDBOX=1` | Confine client-supplied paths to the data root |
| `EMBODIT_PYPI_MIRROR` | `tsinghua`, `official`, or a trusted Simple Index URL |
| `EMBODIT_CACHE_DIR` | Cache, reports, and detached-job state |
| `EMBODIT_REVIEW_CONFIG` | Custom manual quarantine reasons |
| `EMBODIT_HDF5_FPS` | Fallback when recognized HDF5 data has no FPS |
| `EMBODIT_MCAP_GAP_S` | Episode split gap for a single MCAP file |

The built-in service uses bearer-token authentication over plain HTTP. Keep it
on localhost or a trusted private network; read [SECURITY.md](../../SECURITY.md)
before LAN use.

## 2. Supported scope

| Format | Detection and browsing | Native-format subset |
|---|---|---|
| LeRobot v2.1 | `meta/info.json`; accepts compatible `v2.0`/`v2.1` layouts | Rebuilds selected Parquet and metadata; copies or hardlinks video |
| LeRobot v3 | Compatible `v3`/`v3.0` metadata and sharded data/video | Rebuilds selected shards and metadata; copies or hardlinks video |
| HDF5 | Recognized RoboMimic/Astribot-compatible episode layouts | Rebuilds one HDF5 file containing selected episodes |
| MCAP | One file, a top-level directory, or one nested directory level | Rewrites schemas, channels, and messages in selected episode windows |

Browsing exposes episodes, tasks, cameras, FPS, frames, and recognized
state/action series on a common playback timeline. This timeline alignment does
not prove that source sensors were physically synchronized.

MCAP camera, joint, numeric, and pose discovery depends on supported schemas
and topic-name heuristics. Arbitrary HDF5 layouts, ROS schemas, calibration
records, attachments, and custom columns are not automatically normalized.

## 3. Decisions, QC, and labels are separate

Embodit intentionally keeps three kinds of state independent:

| State | Storage | Effect |
|---|---|---|
| Manual browse decision | `*.review.json` (`pass`, `review`, `quarantine`) | Drives normal filtered export unless explicit episode IDs are supplied |
| QC decision | Scan-specific SQLite report under `.embodit_cache/reports/qc/` | Stores `autoDecision`, optional `manualDecision`, findings, and audit history |
| Labels | Dataset's fixed JSONL sidecar | Adds episode/frame/interval annotations; does not change either decision |

Review documents must be v2 or v3, end in `.review.json`, and identify the same
dataset when overwritten. Quarantine reasons come from
[`../../config/data/review.json`](../../config/data/review.json); do not rename a
reason ID after use—disable it with `enabled: false`.

The label sidecar location is fixed:

- directory dataset: `<dataset>/labels.jsonl`;
- single-file dataset: `<file>.labels.jsonl`.

The web UI creates episode and interval labels. The backend schema additionally
accepts frame labels. An interval label is metadata only: exports still operate
on complete episodes and do not trim the interval into a clip.

When export is opened from a QC selection, the scan query supplies a fixed set
of episode IDs. Saving a QC manual decision does not silently rewrite a browse
review file, and labels do not silently alter either selection.

## 4. Automatic quality control

| Profile | Current behavior | Typical use |
|---|---|---|
| `fast` | Integrity, low-cost freeze sampling, motion, and gripper checks; exposure/blur visual quality and camera shake disabled | Large first pass |
| `standard` | Integrity, freeze, exposure, blur, camera shake, motion, and gripper checks | Routine scan |
| `deep` | Higher sampling rate and resolution | Final audit |

Reports contain the effective configuration, a lightweight dataset fingerprint,
detector versions, findings, evidence intervals, thresholds, coverage, and
review audit. The fingerprint uses path/size/mtime and structural information;
it is not a cryptographic content hash.

| Field | Meaning |
|---|---|
| `integrityStatus` | Structural validity; hard-invalid episodes are quarantined |
| `usableRatio` | Duration left after the union of `error`/`fatal` intervals |
| `qualityScore` | Severity/confidence/duration score |
| `coverage` | Completed detector weight divided by applicable detector weight |
| `autoDecision` | Automatic `pass`, `review`, or `quarantine` |
| `manualDecision` | Scan-local human override, when present |

Default decisions are conservative: hard-invalid or fatal becomes
`quarantine`; score ≥80, usable ratio ≥90%, coverage ≥80%, and no error becomes
`pass`; other episodes become `review`. Reviewing a finding changes its audit
state but does not recompute the stored score. Set the episode-level QC decision
when a human override is required.

Current detectors primarily cover structural integrity, visual/signal quality,
and selected motion/cross-modal checks. Full sensor synchronization, device
physics limits, task success, duplicate detection, drift, and train/eval leakage
remain profile-specific work.

There is no universal quality threshold for robot data. Treat the bundled
defaults as conservative evidence-generation starting points, not certification.
Maintain a labeled calibration set for each robot, control mode, and camera
layout; prioritize precision for automatic quarantine and send ambiguous cases
to review. Detector and configuration versions are stored with each report so
rule changes do not silently reuse incompatible results.

## 5. Subset export and fidelity

All subset writers require a new output path. Keep the source until the output
has been validated. LeRobot, HDF5, and MCAP writers use staging
and no-overwrite publication in their supported write paths, so a failed or
cancelled write does not publish its partial staging product as the target.

“Native-format subset” means a usable subset in the same dataset family, not a
byte-for-byte or container-lossless copy:

| Format | Preserved | Rebuilt or not guaranteed |
|---|---|---|
| LeRobot v2.1/v3 | Selected episode samples, standard features, tasks, media | Episode/frame indices, shards, and metadata/statistics are rebuilt |
| HDF5 | Recognized episode arrays/images and supported dtypes | Root objects/attributes, multi-file layout, and unknown dialect fields |
| MCAP | Supported schemas/channels/messages inside selected time windows | Chunk/index/compression layout, attachments, metadata records, unrelated messages |

`hardlink` saves space for LeRobot media but requires filesystem support and
keeps the output dependent on the same underlying media inode. Use `copy` for
an independent media copy.

## 6. Cross-format conversion

| Path | Fidelity | Main limitations |
|---|---|---|
| LeRobot v2.1 ↔ v3 | `high` | Standard samples/media retained; metadata and shards rebuilt |
| LeRobot ↔ HDF5 | `partial` | Media may be transcoded; timestamps/FPS and metadata may be reconstructed |
| MCAP → LeRobot/HDF5 | `partial` | Only selected cameras and standard/explicit numeric series are mapped |
| LeRobot/HDF5 → MCAP | `partial` | Synthesized state/action topics, JPEG cameras, FPS-derived timestamps |

Conversion works through recognized tasks, cameras, and standard or explicitly
mapped state/action series. It does not preserve arbitrary columns, topics,
calibration, original ROS schemas, or irregular timestamp structure. Each job
writes a report with episode/frame counts, mappings, warnings, and known losses.
Directory outputs use `<output>/conversion_report.json`; single-file HDF5 or
MCAP outputs use `<output-file>.conversion_report.json`.

| Mapping field | Meaning |
|---|---|
| `fps` | Required when the source has no usable FPS |
| `state_key` / `action_key` | Explicit source-series selection |
| `media_mode` | `hardlink` or `copy` for compatible same-format media |
| `on_error` | `fail` or `skip`; `skip` may reduce output episodes |
| `allow_camera_loss` | Permit a failed/unextractable camera to be omitted |
| `state_topic` / `action_topic` | Numeric MCAP output topics |
| `camera_topics` | `{camera_key: "/topic/name"}` for MCAP output |
| `mcap_image_quality` | MCAP JPEG quality `1..100`; default `90` |

See [`../../config/data/convert.example.json`](../../config/data/convert.example.json).

## 7. Strict merge

Merge accepts at least two distinct, non-empty datasets in the same format.
Preflight compares FPS (`1e-6` tolerance), robot type, camera/features,
LeRobot schemas, HDF5 dialect/group/dtype/non-episode shapes, or MCAP
topic/encoding/schema identity. Incompatible inputs must be converted or
normalized explicitly first.

Source order determines output episode order and the output must not exist.
Labels are remapped when requested. Directory datasets store manifests/labels
inside the output; single-file formats use adjacent sidecars. `hardlink` versus
`copy` mainly affects LeRobot video media.

## 8. Jobs, cache, and cleanup

QC, conversion, and merge use detached workers; closing the
browser does not stop them.

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --expired
bash embodit.sh clean --cache
bash embodit.sh clean --all
```

`--cache` removes reproducible media caches. `--all` also removes job
history and QC reports under the cache root. It does not remove datasets,
derived outputs, labels, review files, deployment state, the Python environment,
or the service token/log. Archive important reports before cleanup.

Retention is applied once at service startup and then periodically while the
service is running. Configure it before starting Embodit:

| Variable | Default | Purpose |
|---|---:|---|
| `EMBODIT_MEDIA_TTL_DAYS` | `7` | Reproducible playback-media cache files |
| `EMBODIT_JOB_TTL_DAYS` | `30` | Terminal export, conversion, merge, and QC job records/logs |
| `EMBODIT_TEMP_TTL_DAYS` | `1` | Temporary/staging artifacts |
| `EMBODIT_QC_REPORTS_PER_DATASET` | `5` | Latest reports retained per dataset; older reports remain while referenced by a job |
| `EMBODIT_MAINTENANCE_INTERVAL_HOURS` | `24` | Periodic cleanup interval; positive values are clamped to at least one hour, and `0` disables only periodic cleanup |

A zero TTL or report count makes matching unprotected items immediately eligible
for cleanup. Startup maintenance still runs when the periodic interval is `0`.

## 9. Troubleshooting and extension

| Problem | Check |
|---|---|
| Dataset not detected | Version marker, recognized HDF5 layout, or MCAP scan depth |
| Media unavailable | Codec/FFmpeg, file permissions, and service log |
| Low QC coverage | Skipped detectors, camera mapping, and state/action dimensions |
| Missing FPS | Set `fps` in conversion mapping or `EMBODIT_HDF5_FPS` |
| Hardlink failure | Use `copy` or keep source/output on one filesystem |
| Path rejected | Start with an appropriate data root; keep sandboxing enabled on LAN |

Extension boundaries: dataset adapters live in `backend/datasets/`, QC detectors
in `backend/qc/detectors/`, conversion in `backend/convert/`. Contribution boundaries are summarized in
[CONTRIBUTING.md](../../CONTRIBUTING.md).
