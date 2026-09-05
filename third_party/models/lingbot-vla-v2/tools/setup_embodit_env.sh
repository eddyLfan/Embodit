#!/usr/bin/env bash
# Independent inference environment. Never modifies another model's packages.
set -euo pipefail
cd "$(dirname "$0")/.."
UV="${UV:-/home/acting/.local/bin/uv}"
PYTHON312="${PYTHON312:-/home/acting/miniconda3/envs/sam3/bin/python}"
PYPI_INDEX="${PYPI_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
if [[ ! -x .venv/bin/python ]]; then
    "$UV" venv --python "$PYTHON312" .venv
fi
"$UV" pip install --python .venv/bin/python --index-url "$PYPI_INDEX" -r requirements-inference.txt
if ! .venv/bin/python -c 'import torch, flash_attn' >/dev/null 2>&1; then
    : "${FLASH_ATTN_ARCHIVE:?Set to the training cp312/torch2.8/cxx11abiTRUE FlashAttention archive}"
    expected=c54c393fb1a6b0d745c814af01feec4f30bae9cac0ed7ad25c8075a701c5a9ba
    actual=$(sha256sum "$FLASH_ATTN_ARCHIVE" | cut -d ' ' -f 1)
    [[ "$actual" == "$expected" ]] || { echo "FlashAttention archive checksum mismatch" >&2; exit 1; }
    tar -xzf "$FLASH_ATTN_ARCHIVE" -C .venv/lib/python3.12/site-packages
fi
.venv/bin/python -c 'import torch, flash_attn; from deploy.lingbot_vla_v2_policy import LingbotVLAv2Server; print(torch.__version__, torch.version.cuda, flash_attn.__version__)'
"$UV" pip check --python .venv/bin/python
