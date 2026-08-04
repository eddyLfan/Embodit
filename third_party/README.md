# Optional third-party components

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
