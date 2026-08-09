# Changelog

All notable user-visible changes to Embodit are documented here. The project
follows [Semantic Versioning](https://semver.org/); `0.x` denotes initial development.

## [Unreleased]

First open-source release candidate.

### Added

- Local workspace for LeRobot v2.1/v3, recognized HDF5 layouts, and MCAP data.
- Manual review decisions, fixed JSONL label sidecars, automatic QC reports,
  native-format subsets, cross-format conversion, strict merge, and visual
  augmentation with optional SAM3-assisted effects.
- Composable Robot/Model Configs, Recipe v2 validation, SSH/systemd
  orchestration, ROS readiness, Dry Run, offline single-frame evaluation, Live
  mode, monitoring, rollback, and software emergency-stop integration.
- Public Python and dependency-free frontend regression suites with GitHub
  Actions checks.
- English/Chinese data and deployment guides, plus contribution, security, and
  third-party documentation.

### Changed

- Refactored persistent worker state, cancellation, no-overwrite publication,
  path validation, media handling, dataset payloads, and deployment lifecycle
  logic for clearer boundaries and lower request-thread overhead.
- Rebuilt the static Web UI as a compact, responsive light interface with
  system-native typography and restrained visual hierarchy.
- Recomputed LeRobot v2.1 subset indices and statistics from emitted samples.

### Fixed

- Prevented stale workers from reviving terminal jobs and tightened cleanup of
  incomplete staging products.
- Prevented path traversal and unsafe output/sidecar targeting across data APIs.
- Allowed CPU-only brightness augmentation without color-augmentation CUDA/GPU
  validation.
- Hardened offline evaluation concurrency, deployment teardown, action
  validation, and error recovery.

### Security

- Documented the bearer-token/plain-HTTP trust model, localhost versus LAN path
  confinement, sensitive state locations, trusted executable configuration,
  third-party checkpoint boundaries, and independent hardware-safety needs.
