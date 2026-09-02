# Security Policy

Embodit handles robot datasets, model checkpoints, remote deployment credentials,
and commands that may ultimately move physical hardware. Treat an Embodit host as
an engineering control system, not as a public web application.

## Supported Versions

Embodit is currently pre-1.0. Security fixes are made on the latest `main`
revision. Older commits, forks, and locally modified deployments are not covered
by a backport guarantee.

| Version | Security updates |
|---|---|
| Latest `main` | Supported |
| Older revisions | Not guaranteed |

When reporting a problem, include the commit SHA and whether the deployment has
local changes.

## Reporting a Vulnerability

Please do not disclose an exploitable vulnerability in a public issue, pull
request, discussion, log excerpt, or chat transcript.

Use this repository's GitHub **Security** tab and choose **Report a
vulnerability** to open a private security advisory. If that option is not
available, open a public issue containing no vulnerability details and ask the
maintainers to provide or enable a private reporting channel. Do not attach a
proof of concept, credentials, private paths, robot addresses, or sensitive
datasets to that issue.

A useful private report includes:

- the affected commit and deployment mode;
- impact and the trust boundary that was crossed;
- minimal reproduction steps or a narrowly scoped proof of concept;
- relevant configuration with secrets and private infrastructure redacted;
- whether a robot, dataset, credential, or remote host may already be affected;
- any suggested mitigation or embargo constraints.

The project does not currently publish a security bounty or a guaranteed
response timeline. Maintainers should acknowledge reports privately, coordinate
validation and remediation, and agree on disclosure before details are made
public.

## Threat Model

### Localhost mode

The default listener is `127.0.0.1:8765`. In localhost mode, path confinement is
disabled unless `EMBODIT_SANDBOX=1` is set. The selected data root is therefore
an initial browsing location, not a security boundary: an authenticated client
may request other absolute paths available to the service account.

Run Embodit under a dedicated, least-privileged operating-system account when
the workstation, browser profile, or local users are not fully trusted. Do not
run it as root.

### LAN mode and path confinement

When `EMBODY_HOST` is non-loopback and `EMBODIT_SANDBOX` is unset,
`embodit.sh` automatically enables path confinement. Client-supplied dataset,
input, output, and sidecar paths are then restricted to the selected data root.
This does not move internal service state, caches, QC reports, or deployment
configuration into that root; those remain under their configured private
directories.

Do not disable confinement on a non-loopback listener unless every authenticated
user is trusted with the service account's filesystem permissions. Symlinks and
mounts inside a data root should also be treated as part of the trusted data
layout.

### Authentication and transport security

Embodit uses a bearer token. The browser exchanges the first `?token=...` URL
for a 30-day HttpOnly, SameSite=Lax cookie. API clients may use the
`X-LeRobot-Token` header. Possession of the token authorizes both data operations
and deployment-control APIs.

The built-in server provides HTTP, not TLS. Do not expose it directly to the
public Internet. For access beyond a trusted private network:

- place it behind a correctly configured TLS reverse proxy or private VPN;
- restrict source networks with a firewall and additional access control;
- keep path confinement enabled;
- ensure the application sees HTTPS when Secure-cookie behavior is expected;
- prevent query-string tokens from entering proxy, browser-history, analytics,
  screenshot, and support-bundle logs;
- rotate the token after suspected disclosure by stopping the service and
  replacing the configured/state token before restart.

`EMBODY_PUBLIC_HOST` only changes the URL printed by the launcher. It does not
configure DNS, TLS, proxy trust, or firewall policy.

## Credentials and Sensitive State

The default service state directory is `.embodit/`. It contains the access
token, URL, PID, environment fingerprint, and service log. The unified cache is
`.embodit_cache/`; it may contain job parameters, local paths, previews, decoded
media, QC evidence, reports, deployment configuration, recipes, manifests, and
orchestration logs. Both locations may be changed with environment variables,
but they remain sensitive.

- Keep these directories private and exclude them from source control, shared
  archives, telemetry, and support bundles.
- Prefer SSH agents, restricted identity files, or Recipe-referenced environment
  variables over embedded passwords.
- Never commit tokens, passwords, private keys, production hostnames, dataset
  samples, task prompts, or signed artifact URLs.
- Review backups and filesystem permissions. File mode `0600` is useful but does
  not protect against the same account, root, compromised processes, or copied
  backups.
- Redact secrets before sharing validation output, manifests, screenshots, or
  logs.

The cleanup command intentionally does not erase the service token, service log,
Python environment, datasets, labels, review files, or deployment state. Remove
sensitive retained state explicitly and carefully when decommissioning a host.

## Trusted Configurations, Code, and Checkpoints

Treat deployment Recipes and robot/model configurations as executable control
material. They can select local or remote Python environments, shell setup,
SSH/systemd operations, ROS interfaces, model servers, tunnels, lifecycle
commands, and custom adapters. Only load configurations from trusted authors and
review every command, host, path, environment variable, observation mapping,
action mapping, and limit before use.

Model repositories, Python providers, custom robot adapters, and checkpoints
execute or influence trusted computation. A malicious or
incompatible artifact can execute code, exhaust resources, return unsafe
actions, or corrupt derived datasets. Pin reviewed revisions, verify provenance
and hashes where available, isolate provider environments, and avoid loaders
that execute untrusted serialized code. Git submodule references and catalog
entries do not attest to downloaded checkpoint contents.

Dataset files are also untrusted input. Parse unknown HDF5, MCAP, Parquet, image,
and video files in a least-privileged environment, keep codecs and dependencies
updated, and retain original data until derived output has been verified.

## Robot Hardware Safety

Embodit's `dry_run`, readiness checks, action limits, manual arm confirmation,
software stop, and emergency-stop orchestration are engineering safeguards, not
a certified safety system. Network loss, process failure, stale telemetry,
mapping errors, incorrect units, model defects, or a compromised host can bypass
software assumptions.

Before any live run:

- validate observation/action names, dimensions, units, coordinate frames,
  timing, limits, and sign conventions against the actual robot;
- begin at reduced speed/force in a cleared workspace with a trained operator;
- use independent physical stops, guarding, interlocks, and a tested hardware
  emergency stop that does not depend on Embodit or the same network;
- verify loss-of-communication and process-crash behavior at the robot
  controller;
- keep people outside the reachable workspace and follow the robot vendor's
  safety procedures;
- re-run validation after every model, checkpoint, adapter, configuration,
  firmware, network, or mechanical change.

Never use example addresses, commands, mappings, or limits unchanged on real
hardware.

## Security-relevant Changes

Changes affecting authentication, path handling, file publication, subprocesses,
SSH, remote commands, configuration parsing, checkpoint loading, robot actions,
or cleanup require focused regression tests and explicit security review. Preserve
fail-closed behavior, atomic/no-overwrite publication, least privilege, secret
redaction, and auditable stop/recovery paths.

## 中文摘要

Embodit 默认使用 HTTP Bearer Token，不应直接暴露在公网。局域网使用时请
保持路径沙箱开启，并通过 TLS 反向代理、VPN、防火墙和最小权限限制访问。
配置、Recipe、模型代码和 Checkpoint 都应视为可执行的可信资产。软件急停不能替代
独立的硬件急停与设备安全流程。漏洞请通过 GitHub Security 页面私密报告，不要在
公开 Issue 中提供利用细节、密钥或真实设备信息。
