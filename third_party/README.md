# Third-party components

Embodit-owned integration code lives in `backend/deploy/assets/model_adapters.py`.
The projects below remain independent upstream works: their source is referenced
through pinned Git submodules, their packages are installed into separate model
environments, and their model weights are not redistributed by Embodit.

## Checkpoint model providers

| Provider | Submodule | Pinned revision | Upstream code license | Embodit contract |
| --- | --- | --- | --- | --- |
| OpenPI | `third_party/models/openpi` | `15a9616a00943ada6c20a0f158e3adb39df2ccac` | Apache-2.0; Gemma-derived components and checkpoints may carry additional terms | `openpi.policies.policy_config.create_trained_policy` |
| LeRobot | `third_party/models/lerobot` | `f66e5128ecb2456e8c54a63d15404fa59c16aebc` | Apache-2.0 | checkpoint `config.json`, `PreTrainedConfig.from_pretrained`, policy `from_pretrained`, pre/post-processors |
| StarVLA | `third_party/models/starvla` | `312fac890ab75b7651d2bc4f8f8c8dbb5e055184` | MIT | `deployment.model_server.policy_wrapper.PolicyServerWrapper` |

Upstream sources:

- OpenPI: <https://github.com/Physical-Intelligence/openpi>
- LeRobot: <https://github.com/huggingface/lerobot>
- StarVLA: <https://github.com/starVLA/starVLA>

The commit hashes above are the compatibility boundary, not merely examples.
Do not replace them with a floating branch in deployment automation.

### Initialize sources

On a fresh Embodit checkout, initialize all pinned sources once:

```bash
GIT_LFS_SKIP_SMUDGE=1 git submodule update --init --recursive
git submodule status --recursive
```

`GIT_LFS_SKIP_SMUDGE=1` prevents an incidental source initialization from
downloading large upstream LFS artifacts. Checkpoints are managed separately
and supplied through the model config's `checkpoint` field.

Install each provider by following the README at its pinned revision. Keep one
environment per provider, for example `openpi`, `lerobot`, and `starvla`; their
PyTorch/CUDA and transitive dependency pins are not merged into Embodit's web
environment. The model host must contain the same Embodit checkout (the default
examples use `/root/Embodit`) and the selected environment before first use.
After that one-time provisioning, a deployment only needs the provider and a
checkpoint.

### Checkpoint expectations

- **OpenPI:** official checkpoints can normally infer their training config
  from the checkpoint directory. For a renamed or custom training checkpoint,
  set `model.load_kwargs.config_name` to the upstream OpenPI config name.
- **LeRobot:** the local directory or Hub repository must be a complete
  `save_pretrained` checkpoint, including its config and processor metadata.
- **StarVLA:** point to the checkpoint directory expected by the pinned
  `PolicyServerWrapper`; keep its model config and normalization statistics
  beside the weights. Set `model.load_kwargs.unnorm_key` when the checkpoint
  contains more than one normalization domain.

These are checkpoint-format requirements, not extra Embodit entrypoints. The
model `entrypoint`, HTTP `/health`, and `/infer` service are generated and
managed by Embodit for all three providers. Common camera/state/task names are
mapped automatically; use `model.load_kwargs.observation_map` (target feature
name to robot observation name) when a checkpoint uses a special feature
schema.

### Review and update workflow

Update one provider deliberately, never with an unattended `--remote` update:

```bash
git -C third_party/models/lerobot fetch origin
git -C third_party/models/lerobot checkout <reviewed-commit>
git add third_party/models/lerobot
```

Before accepting the new gitlink, review the upstream changelog and diff,
license changes, checkpoint-loading API, security advisories, and dependency
requirements; then run Embodit's adapter and deployment tests with a real
checkpoint. Update this table and `backend/app.py`'s model catalog in the same
change. Do not patch vendored source silently: contribute fixes upstream or
record any temporary fork, commit, rationale, and license here.

## Optional augmentation components

Embodit contains its own brightness, mask recoloring, and solid-background
algorithms under `backend/augment/`.

Color augmentation additionally uses **SAM3** from Meta:

- Source: <https://github.com/facebookresearch/sam3>
- License: the `SAM License` distributed in that repository
- Checkpoint access: follow the upstream repository and its linked Hugging Face page

SAM3 source code and model weights are not redistributed by Embodit. Install
SAM3 into a separate environment, select it with `AUGMENT_PYTHON`, and point
`AUGMENT_SAM3_CHECKPOINT` at a checkpoint you are authorized to use. By using
SAM3 you accept its upstream license and usage restrictions.

The readiness check verifies required package metadata and imports PyTorch only
to confirm CUDA availability and GPU count. Model weights are loaded only by
the detached color worker when a preview or batch job starts.
