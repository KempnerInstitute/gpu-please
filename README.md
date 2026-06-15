# AWS GPU Instance Provisioner

CLI tool to provision GPU EC2 instances on AWS using Terraform.

## Prerequisites

- [uv](https://docs.astral.sh/uv/getting-started/installation/) — Python package/project manager

  ```bash
  # macOS / Linux
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # or on macOS
  brew install uv
  ```

- [Terraform](https://developer.hashicorp.com/terraform/install) on `$PATH`

  ```bash
  # macOS
  brew install terraform
  # other platforms: see the link above
  ```

- AWS credentials — see [AWS credentials](#aws-credentials) below.

## AWS credentials

The tool uses `boto3` and Terraform's AWS provider, so it picks up credentials from the standard AWS credential chain. Pick one of:

**Option A — `aws configure` (recommended for laptops):**

```bash
# install the AWS CLI first, e.g.:  brew install awscli
aws configure
# AWS Access Key ID:     AKIA...
# AWS Secret Access Key: ...
# Default region name:   us-east-1
# Default output format: json
```

This writes `~/.aws/credentials` and `~/.aws/config`.

**Option B — environment variables (for CI / shells):**

```bash
export AWS_ACCESS_KEY_ID=AKIA...
export AWS_SECRET_ACCESS_KEY=...
export AWS_DEFAULT_REGION=us-east-1
# If using temporary STS credentials, also set:
export AWS_SESSION_TOKEN=...
```

**Option C — IAM role** when running on an EC2 instance / EKS pod with an attached instance profile (no extra config needed).

Verify it works:

```bash
aws sts get-caller-identity
```

### Minimum IAM permissions

The principal you provision with needs:

- **EC2:** `RunInstances`, `TerminateInstances`, `DescribeInstances`, `DescribeInstanceTypeOfferings`, `DescribeAvailabilityZones`, `DescribeImages`
- **EC2 key pairs:** `CreateKeyPair`, `DeleteKeyPair`, `DescribeKeyPairs`
- **VPC + networking:** `CreateVpc`, `DeleteVpc`, `DescribeVpcs`, `CreateSubnet`, `DeleteSubnet`, `DescribeSubnets`, `CreateInternetGateway`, `AttachInternetGateway`, `DetachInternetGateway`, `DeleteInternetGateway`, `DescribeInternetGateways`, `CreateRouteTable`, `CreateRoute`, `AssociateRouteTable`, `DisassociateRouteTable`, `DeleteRouteTable`, `DescribeRouteTables`
- **Security groups:** `CreateSecurityGroup`, `AuthorizeSecurityGroupIngress`, `AuthorizeSecurityGroupEgress`, `RevokeSecurityGroupIngress`, `RevokeSecurityGroupEgress`, `DeleteSecurityGroup`, `DescribeSecurityGroups`
- **Tagging:** `CreateTags`, `DeleteTags`, `DescribeTags`
- **Pricing API:** `pricing:GetProducts` (always called in `us-east-1`, regardless of where you deploy)

For a quick sandbox, the AWS-managed `AmazonEC2FullAccess` policy plus an inline policy granting `pricing:GetProducts` is sufficient. For production, write a tight customer-managed policy with only the actions above.

## Setup

```bash
uv sync
```

### Verify setup

A 30-second smoke test before the first provision:

```bash
uv sync                                                    # install deps
terraform version                                          # terraform on PATH
aws sts get-caller-identity                                # creds work
aws ec2 describe-instance-type-offerings \
    --region us-east-1 \
    --filters Name=instance-type,Values=g5.xlarge          # EC2 read access works
```

If all four succeed, you're ready to provision.

## Usage

### Provision an instance

```bash
uv run provision.py
```

This will:
1. Load and filter GPU instances (excludes fractional/shared GPU types)
2. Verify availability in `us-east-1` via the AWS API
3. Display an interactive table for you to pick an instance
4. Create an SSH key pair, detect your public IP, and run `terraform apply`
5. Print the SSH command to connect

To use a different instance catalog, pass `--instances-file <path>`.

### SSH into a provisioned instance

After provisioning finishes, the tool prints an `ssh` command. To reconnect later:

```bash
uv run provision.py --list                # find the workspace name and public IP
cd workspaces/<workspace-name>
ssh -i <workspace-name>.pem ubuntu@<public-ip>
```

Notes:
- The login user for the Deep Learning AMI is **`ubuntu`**.
- The `.pem` is created with mode `400` automatically.
- SSH is locked to your public IP at provision time. If your IP changes (new network, VPN toggled), either edit the security group's ingress rule in the AWS console, or destroy and re-provision.

### List provisioned instances

```bash
uv run provision.py --list
```

### Destroy a provisioned instance

```bash
uv run provision.py --destroy
```

You'll be shown a list of active instances, asked to pick one, and prompted to confirm before destruction. This runs `terraform destroy`, deletes the AWS key pair, and removes the local workspace.

> [!IMPORTANT]
> A running GPU instance bills per-second (~$0.50–$30/hr depending on type). Run `--destroy` as soon as you're done. **Do not** delete a workspace directory by hand — the EC2 instance, VPC, and key pair will remain in AWS and keep accruing charges. Always use `--destroy` so the AWS resources are torn down too.

### Install software recipes on a running instance

```bash
uv run provision.py --install
```

You can also install recipes right after provisioning — you'll be prompted automatically. Note: cloud-init runs in parallel with `sshd` startup, so wait ~60 seconds after a fresh provision before installing recipes, otherwise the SSH connection will be refused.

Recipes live in the `recipes/` directory. Each recipe is a subdirectory containing a `recipe.yaml` (metadata) and an `install.sh` (installation script). Currently available:

- **NVIDIA DCGM** — GPU health monitoring, diagnostics, and telemetry

## Project Structure

```
├── provision.py              # CLI entry point
├── pyproject.toml            # Python project + dependencies (uv)
├── aws_gpu_instances_*.json  # GPU instance catalog snapshot
├── terraform/
│   ├── main.tf               # EC2, VPC, security group, AMI data source, user_data
│   ├── variables.tf          # Input variables
│   └── outputs.tf            # public_ip, instance_id, ami_id
├── recipes/                  # Post-provisioning software recipes
│   └── dcgm/
│       ├── recipe.yaml       # Recipe metadata
│       └── install.sh        # Installation script
└── workspaces/               # Auto-created (gitignored); one subdirectory per instance
    └── <instance>-<timestamp>/
        ├── *.tf              # Copied Terraform templates
        ├── terraform.tfvars.json
        ├── <name>.pem        # SSH private key
        └── metadata.json     # Instance metadata
```

## Region

The tool provisions into `us-east-1` by default. To change it, edit `DEFAULT_REGION` at the top of `provision.py`. The Pricing API is always queried in `us-east-1` regardless of where you deploy (the pricing endpoint only exists in `us-east-1` and `ap-south-1`).

Make sure your account has GPU instance quota in the chosen region — new accounts often default to 0 vCPUs for G/P-family instances, which will cause `RunInstances` to fail with `VcpuLimitExceeded`. Request increases in the AWS Service Quotas console under "Running On-Demand G and VT instances" or "Running On-Demand P instances".

## AMI

The instance uses the AWS **Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)**, looked up by name pattern in `terraform/main.tf`. The login user is `ubuntu`. If AWS retires that AMI name or it isn't published in your region, the `data.aws_ami.dlami` lookup will fail with "no matching AMI found" — find the current AMI name in the EC2 console (Images → AMI Catalog) and update the `values` filter in `terraform/main.tf`.

## Notes

- SSH access is restricted to your current public IP (detected automatically at provision time).
- Each provisioned instance gets its own isolated workspace with independent Terraform state.
- AMI architecture (x86_64 vs arm64) is auto-detected based on the instance's CPU.
- Every instance auto-installs `uv` to `/usr/local/bin` via Terraform `user_data` at first boot.

## License

MIT — see [LICENSE](LICENSE).
