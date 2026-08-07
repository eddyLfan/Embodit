# Data Layer Guide

**English** · [中文](README.zh-CN.md)

The data layer browses, quality-checks, reviews, labels, filters, converts, merges, augments, and exports real-robot data. Sources remain read-only by default; labels use sidecars and batch operations write to new paths.

## 1. Start and path scope

```bash
bash embodit.sh start /path/to/datasets
```

`/path/to/datasets` is the initial directory. Localhost mode allows other absolute paths accessible to the service account. Non-loopback listeners automatically set `EMBODIT_SANDBOX=1`, restricting every read and write to this root.

The first start installs every dependency synchronously and starts the service only after the environment is ready. Later starts skip synchronization when the environment fingerprint is unchanged. For a slow default route:

```bash
EMBODIT_PYPI_MIRROR=tsinghua bash embodit.sh setup
```

| Variable | Purpose |
|---|---|
| `EMBODY_PORT` | Web port; default `8765` |
| `EMBODY_HOST` | Bind address; default `127.0.0.1` |
| `EMBODY_PUBLIC_HOST` | Workstation address printed for the browser |
| `EMBODIT_SANDBOX=1` | Confine client paths to the data root |
| `EMBODIT_PYPI_MIRROR` | `tsinghua`, `official`, or a trusted Simple Index URL |
| `EMBODIT_CACHE_DIR` | Cache, report, and background-job root |
| `EMBODIT_REVIEW_CONFIG` | Custom human quarantine reasons |
| `EMBODIT_HDF5_FPS` | Fallback when HDF5 contains no FPS |
| `EMBODIT_MCAP_GAP_S` | Episode split gap for a single MCAP file |

## 2. Supported formats

| Format | Detection | Browsed content | Same-format subset |
|---|---|---|---|
| LeRobot v2.1 | `meta/info.json`, `codebase_version=v2.1` | Parquet, MP4, tasks, state/action | Native copy or hardlink |
| LeRobot v3 | `codebase_version=v3.0` | Parquet, sharded video, tasks, state/action | Native copy or hardlink |
| RoboMimic HDF5 | `.h5/.hdf5` and episode groups | Arrays, embedded images, state/action | Preserve HDF5 dtype/schema |
| MCAP | `.mcap` file or directory | Topics, CompressedImage, numeric/pose series | Preserve native schemas/channels/messages |

The workspace shows episodes, tasks, cameras, FPS, frames, and available time-series fields. Cameras share one timeline; state/action dimensions are plotted together.

## 3. Review and labels

Episodes use `pass`, `review`, or `quarantine`. Progress can be saved to and loaded from `*.review.json`. Quarantine reasons come from [`../../config/data/review.json`](../../config/data/review.json):

```json
{
  "quarantineReasons": [
    {"id": "task_failed", "label": {"zh": "任务未完成", "en": "Task not completed"}, "enabled": true}
  ]
}
```

Do not change an `id` after use; disable it with `enabled: false`. Labels may target an episode or time range and are stored in `labels.jsonl`, `*.labels.jsonl`, or another sidecar without modifying media or tables.

## 4. Automatic QC

| Profile | Behavior | Use |
|---|---|---|
| `fast` | Integrity plus low-cost freeze sampling; visual quality and shake disabled | Large first pass |
| `standard` | Integrity, freeze, exposure, blur, camera shake, motion, and gripper | Normal scan |
| `deep` | Higher sample rate and resolution | Final audit |

The template is [`../../config/data/qc.example.json`](../../config/data/qc.example.json). Reports under `.embodit_cache/reports/qc/` contain the effective config, dataset fingerprint, detector versions, findings, intervals, thresholds, evidence, and review audit.

| Field | Meaning |
|---|---|
| `integrityStatus` | Structural validity; hard-invalid episodes are quarantined |
| `usableRatio` | Duration remaining after the union of `error/fatal` intervals |
| `qualityScore` | Severity/confidence/duration quality score |
| `coverage` | Completed detector weight / applicable detector weight |
| `autoDecision` | Automatic `pass/review/quarantine` |
| `manualDecision` | Human override, when present |

Default decisions: hard-invalid or fatal → `quarantine`; score ≥80, usable ≥90%, coverage ≥80%, and no error → `pass`; everything else → `review`.

A finding review (`confirmed/rejected/modified`) changes presentation and audit records but does not recompute the saved score. Set an episode-level manual decision to change selection. Exact issue codes, thresholds, and calibration are in [`QC_STANDARD.zh-CN.md`](QC_STANDARD.zh-CN.md). Thresholds use source-native units and must be calibrated with known good/bad robot data.

## 5. Export and conversion

Same-format subsets use native lossless exports. `hardlink` saves space but requires filesystem support; `copy` creates independent media. Outputs must not exist and are published atomically after staging succeeds.

| Path | Fidelity | Behavior |
|---|---|---|
| Same format | `full` | Native payload and schema retained |
| LeRobot v2.1 ↔ v3 | `high` | Video/state/action retained; some metadata rebuilt |
| LeRobot ↔ HDF5 | `partial` | Video/frame transcode; FPS may be inferred |
| MCAP → LeRobot/HDF5 | `partial` | Selected cameras and numeric topics; unrelated topics/calibration may be dropped |
| LeRobot/HDF5 → MCAP | `partial` | Synthesized state/action topics; cameras stored as JPEG `foxglove.CompressedImage`; timestamps rebuilt from FPS |

Cross-format jobs write `conversion_report.json` with paths, episode/frame counts, mappings, warnings, and known losses.

### Mapping fields

| Field | Meaning |
|---|---|
| `fps` | Required when the source has no FPS |
| `state_key` / `action_key` | Explicit source series selection |
| `media_mode` | `hardlink` or `copy` for same-format media |
| `on_error` | `fail` or `skip` on series read errors |
| `allow_camera_loss` | Permit an unextractable source camera to be skipped |
| `state_topic` / `action_topic` | Numeric MCAP output topics |
| `camera_topics` | `{camera_key: "/topic/name"}` for MCAP output |
| `mcap_image_quality` | MCAP JPEG quality `1..100`; default `90` |

```json
{
  "fps": 30,
  "state_key": "observation.state",
  "action_key": "action",
  "state_topic": "/robot/state",
  "action_topic": "/policy/action",
  "camera_topics": {
    "head": "/camera/head/compressed",
    "left_wrist": "/camera/left_wrist/compressed"
  },
  "mcap_image_quality": 92
}
```

See [`../../config/data/convert.example.json`](../../config/data/convert.example.json).

## 6. Strict merge

Merge requires at least two distinct, non-empty datasets in the same format. Preflight compares FPS (`1e-6` tolerance), robot type, camera keys, normalized features, LeRobot Parquet schemas, HDF5 dialect/group/dtype/non-episode shapes, or MCAP topic/encoding/schema identity. Incompatible data must be explicitly converted/normalized first. Successful outputs include source-to-episode mapping and a merge manifest.

## 7. Visual augmentation

Augmentation requires a successful preview before batch output. Brightness supports automatic multi-camera gain/gamma estimation or manual values. Object recolor and background replacement require a separate CUDA/SAM3 environment:

```bash
export AUGMENT_PYTHON=/path/to/augment-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/datasets
```

See [`../../third_party/README.md`](../../third_party/README.md) for SAM3 setup and licensing. Augmentation preserves untouched fields, episode boundaries, timestamps, and numeric dtypes, and writes a processing manifest.

## 8. Jobs and cleanup

QC, conversion, merge, and augmentation use detached workers. Closing the browser does not stop them.

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --expired
bash embodit.sh clean --cache
bash embodit.sh clean --all
```

`--cache` removes reproducible previews/media; `--all` also removes job history and QC reports. Archive important reports with training data.

## 9. Troubleshooting

| Problem | Check |
|---|---|
| Dataset not detected | Path, format marker, HDF5 groups, or MCAP files |
| Video unavailable | Codec, ffmpeg, permissions, and service logs |
| Low QC coverage | Skipped detectors, camera names, and gripper dimensions |
| Missing FPS | Set `fps` in mapping |
| Hardlink failure | Use `copy` or keep source/output on one filesystem |
| Path outside root | Start with a wider data root; keep confinement enabled for LAN access |

## 10. Extension points

| Goal | Directory |
|---|---|
| Dataset format | `backend/datasets/` |
| QC detector | `backend/qc/detectors/` |
| Conversion | `backend/convert/` |
| Augmentation | `backend/augment/` |

See [`../architecture.md`](../architecture.md).
