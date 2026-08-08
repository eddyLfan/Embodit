# SAM3 checkpoint location

This legacy directory does not hold checkpoints. Put optional SAM3 weights at
the repository path `checkpoints/sam3.pt` (that is, `../checkpoints/sam3.pt`
from this directory), or set `AUGMENT_SAM3_CHECKPOINT` to an absolute path.

Core browsing, review, labeling, QC, export, conversion, and brightness-only
augmentation do not require this checkpoint.
