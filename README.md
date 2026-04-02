# AWS GPU Instance Provisioner

CLI tool to provision GPU EC2 instances on AWS using Terraform.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- [Terraform](https://developer.hashicorp.com/terraform/install) on `$PATH`
- AWS credentials configured (`~/.aws/credentials`, environment variables, or IAM role)

## Setup

```bash
uv sync
```

## Usage

### Provision an instance

```bash
uv run provision.py --instances-file aws_gpu_instances_2026-04-01.json
```

This will:
1. Load and filter GPU instances (excludes fractional/shared GPU types)
2. Verify availability in `us-east-1` via the AWS API
3. Display an interactive table for you to pick an instance
4. Create an SSH key pair, detect your public IP, and run `terraform apply`
5. Print the SSH command to connect

### List provisioned instances

```bash
uv run provision.py --list
```

### Destroy a provisioned instance

```bash
uv run provision.py --destroy
```

You'll be shown a list of active instances, asked to pick one, and prompted to confirm before destruction. This runs `terraform destroy`, deletes the AWS key pair, and removes the local workspace.

### Install software recipes on a running instance

```bash
uv run provision.py --install
```

You can also install recipes right after provisioning — you'll be prompted automatically.

Recipes live in the `recipes/` directory. Each recipe is a subdirectory containing a `recipe.yaml` (metadata) and an `install.sh` (installation script). Currently available:

- **NVIDIA DCGM** — GPU health monitoring, diagnostics, and telemetry

## Project Structure

```
├── provision.py              # CLI entry point
├── requirements.txt
├── terraform/
│   ├── main.tf               # EC2, security group, AMI data source
│   ├── variables.tf          # Input variables
│   └── outputs.tf            # public_ip, instance_id, ami_id
├── recipes/                  # Post-provisioning software recipes
│   └── dcgm/
│       ├── recipe.yaml       # Recipe metadata
│       └── install.sh        # Installation script
└── workspaces/               # Auto-created; one subdirectory per instance
    └── <instance>-<timestamp>/
        ├── *.tf              # Copied Terraform templates
        ├── terraform.tfvars.json
        ├── <name>.pem        # SSH private key
        └── metadata.json     # Instance metadata
```

## Notes

- SSH access is restricted to your current public IP (detected automatically).
- Each provisioned instance gets its own isolated workspace with independent Terraform state.
- The default region is `us-east-1` (configurable in `provision.py` via the `DEFAULT_REGION` constant).
- AMI architecture (x86_64 vs arm64) is auto-detected based on the instance's CPU.
