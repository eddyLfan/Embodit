# Configuration Guide

**English** · [中文](README.zh-CN.md)

```text
config/
├── data/
│   ├── review.json                 # Default manual quarantine reasons
│   ├── qc.example.json             # Automatic-QC parameter template
│   └── convert.example.json        # Cross-format mapping template
├── deployment/
│   ├── recipe.example.json         # Full Recipe v2 reference
│   ├── robot.example.json          # Reusable Robot Config
│   └── models/
│       ├── python.example.json     # Custom Python model
│       ├── openpi.example.json     # OpenPI checkpoint provider
│       ├── lerobot.example.json    # LeRobot checkpoint provider
│       └── starvla.example.json    # StarVLA checkpoint provider
└── local/                          # Private local configs; create as needed
```

Committed `*.example.json` files use documentation-only addresses and
`/path/to/...` placeholders. They are schema references, not runnable hardware
configurations. Copy Robot and Model Configs directly into `config/local/`,
replace every placeholder, and run read-only preflight before use:

```bash
mkdir -p config/local
chmod 700 config/local
cp config/deployment/robot.example.json config/local/my-robot.json
cp config/deployment/models/python.example.json config/local/my-model.json
chmod 600 config/local/my-robot.json config/local/my-model.json
```

Project discovery is intentionally non-recursive and reads only
`config/local/*.json`; a file under `config/local/models/` will not appear in the
web selector. Valid Robot/Model Configs saved through the web UI are stored
privately under `.embodit_cache/deploy/configs/` and take precedence over a
discovered project config with the same `config_id`. Saved Recipes live under
`.embodit_cache/deploy/recipes/`.

`config/local/`, `.embodit/`, and `.embodit_cache/` are ignored by Git. They may
contain credentials, private hosts, paths, prompts, and deployment state; do not
publish or attach them to bug reports. Prefer SSH keys or `password_env` over
inline passwords. Git ignore rules are not access control: keep `config/local/`
at mode `0700` and its private JSON files at mode `0600`.

`data/review.json` is the versioned default loaded by the application and may be
customized in a fork. Set `EMBODIT_REVIEW_CONFIG` to keep local review reasons
outside the repository. See the [deployment guide](../docs/deployment/README.md)
and [security policy](../SECURITY.md).
