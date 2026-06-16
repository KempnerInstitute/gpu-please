# Contributing

Thanks for your interest in this project. Bug reports, fixes, new software recipes, and documentation improvements are all welcome.

## Development setup

```bash
git clone https://github.com/KempnerInstitute/aws-gpu-instance-provisioner.git
cd aws-gpu-instance-provisioner
uv sync
```

Make changes, then run a fast local check before opening a PR:

```bash
# Python syntax + import sanity
uv run python -c "import provision"

# Terraform syntax + plan (no AWS calls, no state mutation)
( cd terraform && terraform fmt -check )

# Shell script lint for recipe install scripts (requires shellcheck)
shellcheck recipes/*/install.sh terraform/*.tpl
```

The interactive flows still need a real AWS account to test end-to-end. Use the smallest viable instance (`g4dn.xlarge`, ~$0.50/hr) and always run `--destroy` immediately afterwards.

## Adding a software recipe

A recipe is a subdirectory under `recipes/` containing two files:

```
recipes/<recipe-name>/
├── recipe.yaml    # metadata
└── install.sh     # idempotent installation script
```

### `recipe.yaml` schema

```yaml
name: NVIDIA DCGM                # Required. Display name in the recipe picker.
description: GPU monitoring...   # Required. One-line tagline shown in the picker.
version: "4"                     # Optional. Free-form version label.
url: https://docs...             # Optional. Reference link, displayed for context.
gpu_vendors:                     # Optional. Filters which GPU vendors this applies to.
  - NVIDIA                       # Valid values: NVIDIA, AMD. Omit to apply to all.
verify_command: dcgmi discovery -l   # Optional. Sanity-check command shown to the user.
```

### `install.sh` conventions

- Start with `#!/usr/bin/env bash` + `set -euo pipefail`.
- Idempotent — running it twice in a row must succeed.
- No interactive prompts; the recipe is run non-interactively over SSH.
- Use `apt-get install -y --no-install-recommends` for package installs to avoid prompts.
- The script runs as a normal user; use `sudo` explicitly when root is needed.
- Write user-facing progress to stdout with clear section markers (the provisioner streams ssh output live so the user sees them).
- Exit non-zero on failure — the provisioner reports the exit code back to the user.

See `recipes/dcgm/` as the canonical example.

## Pull request checklist

Before opening a PR, please confirm:

- [ ] `uv run python -c "import provision"` passes
- [ ] `terraform fmt -check` in `terraform/` passes
- [ ] If you added or changed a recipe, you actually ran it end-to-end against a real instance.
- [ ] If you added a new CLI flag, you updated `README.md`.
- [ ] No secrets, .pem files, .tfstate files, or AWS account IDs in the diff.

## Reporting bugs

Open an issue at <https://github.com/KempnerInstitute/aws-gpu-instance-provisioner/issues> with:

- What command you ran (e.g. `uv run provision.py --ami pytorch`).
- The full error output (redact any account IDs / IPs if sharing publicly).
- AWS region, instance type chosen, storage type chosen.
- Output of `aws sts get-caller-identity` (with the Account ID redacted is fine).

Most failures are AWS-account-side (quotas, IAM, capacity) rather than tool bugs — the provisioner already classifies these into actionable messages, so include the message verbatim.

## Reporting security issues

Please do not file public issues for suspected security problems. Email the maintainers at the address listed on the [Kempner Institute GitHub org](https://github.com/KempnerInstitute) page.
