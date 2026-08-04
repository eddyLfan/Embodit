# Data layer guide

**English** · [中文](README.zh-CN.md)

The data layer covers dataset browsing, synchronized playback, automatic quality control (QC), human review, labels, filtering, conversion, merge, augmentation, and fidelity-aware export. Source datasets are not overwritten by default: write operations create a separate output or an explicit sidecar.

## 1. Start and select a dataset

```bash
bash embodit.sh start /path/to/datasets
```

When the path is omitted, the current directory is used as the browsing root. To prevent API clients from resolving paths outside an allowed root, enable the path sandbox:

```bash
EMBODIT_SANDBOX=1 bash embodit.sh start /allowed/root
```

Embodit currently recognizes LeRobot v2.1, LeRobot v3, RoboMimic HDF5, and MCAP. The adapters in `backend/datasets/` expose a common dataset, episode, time-series, image, and video interface. A common interface does not imply that every field is losslessly interchangeable; check the conversion capability and fidelity report before export.

## 2. Browse, replay, and label

After selecting an episode, the workspace synchronizes camera streams, state/action signals, tasks, and the primary timeline. Labels are stored in sidecars such as `labels.jsonl`, `*.labels.jsonl`, or `*.review.json`; the original media and tables remain unchanged. Generated media previews are stored under `.embodit_cache/media/` and can be regenerated.

## 3. What automatic QC does

QC is a deterministic, versioned rule pipeline rather than a learned “good/bad” classifier. Every finding records a stable issue code, detector version, severity, confidence, threshold, measured evidence, and—when applicable—a time interval. The complete effective configuration is embedded in the SQLite report.

The three profiles change scan cost, not the meaning of the output:

| Profile | Visual/freeze sampling | Parallelism default | Intended use |
|---|---|---|---|
| `fast` | Disables visual-quality and camera-shake checks; freeze sampling 3 fps at width 128 | 4 episodes / 2 cameras | First-pass structural and integrity screening |
| `standard` | Freeze 5 fps/160 px, visual 4 fps/320 px, shake 2 fps/320 px | 2 / 2 | Default daily scan |
| `deep` | Freeze 10 fps/240 px, visual 8 fps/480 px, shake 4 fps/480 px | 2 / 2 | Final audit or difficult visual cases |

Integrity decoding is retained in every profile. Actual nested episode × camera concurrency is capped by the available CPU count. The editable, complete default values are in [`../../config/qc.example.json`](../../config/qc.example.json).

## 4. Default detection standards

The following are the current `standard` defaults. They are starting points, not universal physical constants.

### 4.1 Hard integrity rules

An episode is marked `invalid` and automatically `quarantine` when any hard-invalid condition is found:

- required `action` is missing (`integrity/missing_action`; required by default), or required `state` is missing (`integrity/missing_state`; optional by default);
- action/state is empty, non-numeric, has an invalid shape, or contains NaN/Inf (`integrity/empty_signal`, `integrity/non_numeric_signal`, `integrity/invalid_signal_shape`, `integrity/non_finite_signal`); an adapter read exception is `integrity/signal_read_error`;
- signal length differs from episode length beyond `max(2 frames, 10% of expected length)`, including incompatible action/state lengths (`integrity/signal_length_mismatch`, `integrity/action_state_length_mismatch`);
- fewer than one camera is present, or a configured required camera key/pattern is absent (`integrity/missing_required_camera`, `integrity/missing_required_camera_pattern`);
- a camera cannot be opened/decoded, is empty, or has a decoded ratio below 90% (`integrity/video_source_unavailable`, `integrity/video_decode_error`, `integrity/empty_video`, `integrity/video_frame_count_mismatch`);
- frozen content occupies at least 50% of the episode (`integrity/video_frozen`).

Hard-invalid episodes are separated as `invalidEpisodes` and do not enter the normal selected export set. This prevents a score or a broad filter from silently admitting structurally unusable data.

### 4.2 Video and visual rules

| Issue code | Default standard | Severity |
|---|---|---|
| `visual/frozen` | Mean pixel delta ≤ 0.75 for at least 2 s | `error`; becomes `integrity/video_frozen`, hard `fatal`, at ≥50% of episode |
| `visual/dark` | Grayscale mean < 40 for at least 0.5 s | `warning` |
| `visual/overexposed` | Grayscale mean > 245 for at least 0.5 s | `warning` |
| `visual/blur` | Laplacian variance < 35 for at least 0.5 s | `warning` |
| `visual/camera_shake` | Optical-flow global-vector change > 4 and uniform-motion ratio ≥ 0.45 | `error` |

Adjacent visual intervals within 0.25 s are merged. Static cameras can be listed explicitly. If the list is empty, names containing `base`, `head`, `main`, `front`, or `overhead` are candidates, while `wrist`, `hand`, and `eef` cameras are excluded. This naming heuristic must be overridden when it does not match the robot.

### 4.3 Motion and gripper rules

| Issue code | Default standard | Severity |
|---|---|---|
| `motion/jitter` | In the same non-gripper dimension: acceleration robust-Z > 8 and jerk robust-Z > 8 within 1 frame, P99−P1 signal range ≥ 0.001, acceleration/range ratio > 0.15, jerk/range ratio > 0.3; persists ≥0.08 s | `error` |
| `motion/stationary` | Maximum per-frame delta ≤ 0.0005 for at least 3 s | `warning` |
| `motion/near_zero_episode` | Maximum signal range < 0.01 over the episode | `error` |
| `manipulation/gripper_chatter` | A gripper transition is delta ≥ 0.2; average rate > 4 transitions/s | `error` |
| `manipulation/regrasp_candidate` | With multiple grasps disallowed, three alternating gripper transitions occur within 5 s | `warning` |

Jitter is evaluated on `action` by default and falls back to the first usable continuous signal when action is absent. Dimensions containing `gripper` are excluded from motion jitter. Gripper rules only run on dimensions whose names contain `gripper`, `finger`, or `jaw`; otherwise that detector is marked skipped and coverage decreases.

All motion and gripper thresholds use the dataset’s native numerical units. Joint radians, normalized actions, Cartesian meters, and binary grippers therefore require different calibration. Review representative good/bad episodes before using these defaults for bulk export.

## 5. Scores and automatic decisions

Embodit exposes three independent metrics so that one aggregate number cannot hide incomplete analysis:

- `usableRatio`: 100% minus the union of all `error`/`fatal` time intervals. Warnings do not reduce it. Any hard-invalid finding forces it to 0.
- `qualityScore`: starts at 100. For each time segment, only the maximum overlapping `severity weight × confidence` is integrated, avoiding double-counting overlapping detectors. Default weights are info 0, warning 0.25, error/fatal 1. Non-temporal episode findings apply a maximum penalty of 5 (`warning`), 25 (`error`), or 100 (`fatal`), not a sum. Hard invalid forces the score to 0.
- `coverage`: completed detector weight divided by applicable detector weight. Default weights are signal integrity 3, video 3, motion 1.5, and gripper 1. Failed or skipped detectors reduce coverage instead of being treated as a clean result.

The automatic decision is evaluated in this order:

| Decision | Exact rule |
|---|---|
| `quarantine` | Any hard-invalid finding or any `fatal` finding |
| `pass` | Quality ≥80, usable ratio ≥90, coverage ≥80, and no `error` finding |
| `review` | Every remaining non-quarantined episode |

Warnings can still pass if all thresholds remain satisfied. An error always requires review even if it occupies a short interval and the numerical scores remain high.

## 6. Filtering and human review semantics

Filters support integrity status, effective decision, minimum quality/usable/coverage, issue codes, task text/index search, and sorting by episode index, scores, coverage, or finding count.

The effective decision is `manualDecision` when one exists, otherwise `autoDecision`. Reviewing an individual finding as `confirmed`, `rejected`, or `modified` records audit metadata and adjusted evidence, but currently does **not** recalculate the stored automatic scores. To change whether an episode is selected by decision, set its episode-level manual decision. Clearing that decision returns filtering to the automatic result.

Recommended operating procedure:

1. Scan a representative subset with `standard`.
2. Inspect all `quarantine` rows and a sample of `pass` rows.
3. Calibrate camera-name rules and thresholds against known good/bad episodes.
4. Run the full scan and filter by effective decision plus minimum coverage.
5. Preview the fixed episode set before export; keep the QC report with the output.

QC is designed to surface measurable anomalies. It cannot determine semantic task success, safety, or whether unusual motion is intentional; those remain human- or task-specific labels.

## 7. Report caching and reproducibility

Reports are stored under `.embodit_cache/reports/qc/` as SQLite files. A completed report is reused only when both the configuration hash and dataset fingerprint match. The fingerprint includes the resolved path, format, FPS, features, episodes, cameras, selected metadata, and media file size/mtime. A data or configuration change therefore produces a new result.

Each report preserves detector status, findings, thresholds, metrics, decisions, reviews, and an audit log. For long-lived datasets, archive the report and QC config together with an exported subset.

## 8. Filtering, conversion, and fidelity

Export first freezes the episode selection, validates source and target, renumbers required identifiers, writes a fidelity report, and atomically exposes the completed output. The target must be separate from the source.

Conversion fidelity is reported explicitly:

| Path | Fidelity class | Expected behavior |
|---|---|---|
| Same-format subset | `full` | Native lossless subset where supported |
| LeRobot v2.1 ↔ v3 | `high` | Video/action/state preserved; metadata may be rebuilt |
| LeRobot ↔ HDF5 | `partial` | Video/frame re-encoding and FPS inference may occur |
| MCAP → LeRobot/HDF5 | `partial` | Selected streams decoded; unrelated topics/calibration may be dropped |
| LeRobot/HDF5 → MCAP | `partial` | Topics and timestamps are synthesized from dataset fields |

The report records source/target formats, episode/frame counts, field mapping, known losses, and warnings. Configuration example: [`../../config/convert.example.json`](../../config/convert.example.json).

## 9. Strict merge standard

Merge accepts at least two unique, non-empty, same-format datasets. Preflight rejects mismatches in format, FPS (tolerance `1e-6`), robot type, camera keys, canonical features, HDF5 dialect, or native schema. Native checks include LeRobot Parquet columns/types, HDF5 groups/dataset dtypes and non-episode shapes, and MCAP topics/encodings/schema identity. The merge preserves native payloads and only rewrites identifiers, task references, and timestamps required for a coherent output.

This strictness is intentional: datasets that need schema or unit conversion should be normalized with an explicit conversion step before merge.

## 10. Visual augmentation

Brightness adjustment requires no external model. Object recoloring and background replacement require a separate Python/CUDA environment and a SAM3 checkpoint:

```bash
export AUGMENT_PYTHON=/path/to/augment-env/bin/python
export AUGMENT_SAM3_CHECKPOINT=/path/to/sam3.pt
bash embodit.sh start /path/to/datasets
```

See [`../../config/augment-worker-requirements.txt`](../../config/augment-worker-requirements.txt) and [`../../third_party/README.md`](../../third_party/README.md). Augmentation preserves unmodified fields, timestamps, episode boundaries, and original data types, and emits a processing manifest. Always validate a single-frame and video preview before batch output.

## 11. Cache cleanup and data safety

```bash
bash embodit.sh clean --dry-run
bash embodit.sh clean --cache
```

Do not delete `.embodit_cache/` blindly: it can contain both regenerable previews and reports worth retaining. Embodit uses separate outputs, sidecars, preflight validation, and atomic completion; errors do not make an incomplete temporary output appear as a valid dataset.

## 12. Extending the data layer

| Goal | Directory | Required contract |
|---|---|---|
| Dataset format | `backend/datasets/` | Detection, metadata, episode/frame, signal and media access |
| Conversion path | `backend/convert/` | Capability registration, field mapping, and fidelity report |
| QC detector | `backend/qc/detectors/` | Stable issue code, severity, confidence, evidence/interval, version |
| Augmentation | `backend/augment/` | Pure algorithm, preview, batch writer, fidelity checks |
| UI workflow | `web/` | Cancellable jobs, visible errors, explicit writes |

Relevant tests include `tests/test_detect.py`, `test_convert_matrix.py`, `test_qc_core.py`, `test_augment_*`, and `test_merge.py`.
