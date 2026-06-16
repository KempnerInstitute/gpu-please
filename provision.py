#!/usr/bin/env python3
"""AWS GPU Instance Provisioner — CLI tool to provision GPU EC2 instances via Terraform."""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
import yaml
from botocore.exceptions import ClientError
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_REGION = "us-east-1"
DEFAULT_PRICING_SOURCE = "vantage"
PRICING_SOURCES = ("vantage", "aws-api", "none")
VANTAGE_INSTANCES_URL = "https://instances.vantage.sh/instances.json"
SCRIPT_DIR = Path(__file__).resolve().parent
TERRAFORM_TEMPLATE_DIR = SCRIPT_DIR / "terraform"
WORKSPACES_DIR = SCRIPT_DIR / "workspaces"
RECIPES_DIR = SCRIPT_DIR / "recipes"
PRICING_CACHE_DIR = SCRIPT_DIR / ".pricing_cache"
METADATA_FILE = "metadata.json"
USER_CONFIG_PATH = Path.home() / ".config" / "aws-terraform-provisioner" / "config.json"

console = Console()


# ---------------------------------------------------------------------------
# User config (persistent across runs)
# ---------------------------------------------------------------------------


def _load_user_config() -> dict:
    """Load persistent user config; return an empty dict if absent or invalid."""
    try:
        with open(USER_CONFIG_PATH) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_user_config(config: dict) -> None:
    """Persist user config to disk, creating the parent directory if needed.

    Failures (read-only home, full disk, etc.) are logged as warnings — the
    current run continues normally, the user can pass --region next time.
    """
    try:
        USER_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(USER_CONFIG_PATH, "w") as f:
            json.dump(config, f, indent=2)
    except OSError as e:
        console.print(
            f"  [yellow]Could not persist user config to {USER_CONFIG_PATH} "
            f"({type(e).__name__}: {e}). Pass --region <r> next time to skip the prompt.[/yellow]"
        )


# Loose regex: AWS region codes are "<area>-<location>-<digit>", e.g. us-east-1,
# eu-west-2, ap-southeast-3. New regions (especially AWS Local Zones / Wavelength
# zones) sometimes break this pattern but the regex catches the common typos.
_REGION_RE = re.compile(r"^[a-z]{2,3}-[a-z]+-\d+$")


def _resolve_region(cli_region: str | None) -> str:
    """Determine the AWS region to use.

    Precedence: CLI flag > saved user default > prompt (with hardcoded fallback).
    The interactive prompt validates the shape against `_REGION_RE` and accepts
    'q'/'quit' to exit. The selected region is persisted only after passing
    validation, so a bad value can't poison future runs.
    """
    if cli_region:
        return cli_region
    config = _load_user_config()
    default = config.get("region", DEFAULT_REGION)
    while True:
        answer = console.input(
            f"[bold]AWS region[/bold] [dim](default: {default}, 'q' to quit)[/dim]: "
        ).strip().lower()
        if answer in ("q", "quit"):
            console.print("Cancelled.")
            sys.exit(0)
        region = answer or default
        if not _REGION_RE.match(region):
            console.print(
                f"  [red]'{region}' doesn't look like an AWS region.[/red] "
                "Examples: us-east-1, us-west-2, eu-west-1, ap-southeast-1."
            )
            continue
        break
    if region != config.get("region"):
        config["region"] = region
        _save_user_config(config)
        console.print(f"  [dim]Saved [bold]{region}[/bold] as your default region.[/dim]\n")
    return region


# ---------------------------------------------------------------------------
# AWS credentials & error classification
# ---------------------------------------------------------------------------


def _aws_error_message(exc: Exception, action_hint: str = "") -> str:
    """Translate a boto3/botocore/requests exception into an actionable message.

    Use this everywhere we catch AWS errors so the user sees the same shape of
    explanation regardless of which API failed.
    """
    name = type(exc).__name__
    if name in ("NoCredentialsError", "PartialCredentialsError"):
        return (
            "AWS credentials are not configured. Run `aws configure` (or "
            "`aws sso login` if you use SSO), or set AWS_ACCESS_KEY_ID + "
            "AWS_SECRET_ACCESS_KEY in your environment."
        )
    if name in ("EndpointConnectionError", "ConnectTimeoutError", "ReadTimeoutError"):
        return (
            f"Cannot reach the AWS endpoint ({exc}). Check your network/VPN, "
            "DNS, and that the region code is correct."
        )
    code = ""
    try:
        code = exc.response["Error"]["Code"]  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - exc may not be a ClientError
        pass
    if code in ("InvalidClientTokenId", "ExpiredToken", "RequestExpired", "TokenRefreshRequired"):
        return (
            f"AWS session has expired ({code}). Run `aws sso login` (or refresh "
            "your STS credentials) and retry."
        )
    if code in ("AuthFailure", "SignatureDoesNotMatch", "UnrecognizedClientException"):
        return f"AWS authentication failed ({code}). Re-check your access keys / profile."
    if code in ("AccessDenied", "AccessDeniedException", "UnauthorizedOperation"):
        action = f" ({action_hint})" if action_hint else ""
        msg = ""
        try:
            msg = exc.response["Error"].get("Message", "")  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            pass
        return (
            f"AWS denied the request{action}: {msg or code}. "
            "Add the matching IAM permission to your user/role."
        )
    if code == "OptInRequired":
        return (
            "The chosen AWS region is not enabled for this account. Enable it at "
            "https://console.aws.amazon.com/billing/home#/account, or pick a "
            "different region."
        )
    if code in ("Throttling", "ThrottlingException", "RequestLimitExceeded"):
        return f"AWS throttled the request ({code}). Retry in a few seconds."
    if code:
        return f"AWS error {code}: {exc}"
    return f"Unexpected error ({name}): {exc}"


def _verify_aws_credentials(region: str) -> dict:
    """Confirm we have working AWS credentials in the given region.

    Returns the STS GetCallerIdentity payload on success, or exits cleanly
    with an actionable message on failure. Should be called once at the top
    of every flow that touches AWS so the user gets one clear error instead
    of a deep boto3 traceback.
    """
    try:
        ident = boto3.client("sts", region_name=region).get_caller_identity()
    except Exception as e:  # noqa: BLE001 - intentionally broad; classified below
        console.print(f"[red]AWS credential check failed:[/red] {_aws_error_message(e)}")
        sys.exit(1)
    console.print(
        f"  [dim]AWS account: [cyan]{ident.get('Account')}[/cyan]  "
        f"principal: [cyan]{ident.get('Arn')}[/cyan]  region: [cyan]{region}[/cyan][/dim]"
    )
    return ident

# ---------------------------------------------------------------------------
# Instance loading & filtering
# ---------------------------------------------------------------------------


def load_instances(json_path: str) -> list[dict]:
    """Load all GPU instances from the JSON file (all_instances_flat key)."""
    with open(json_path) as f:
        data = json.load(f)
    return data.get("all_instances_flat", [])


def filter_instances(instances: list[dict]) -> list[dict]:
    """Remove fractional/shared GPU instances."""
    return [i for i in instances if not i.get("shared_or_fractional_gpu", False)]


def pick_az_for_instance(instance_type: str, region: str) -> str:
    """Return an availability zone in the region that supports the given instance type.

    Many GPU instance types are only available in a subset of AZs (e.g. g7e is
    not offered in us-east-1a). Picking the alphabetical-first AZ leads to a
    confusing terraform apply failure. This helper queries AWS for the actual
    set of supported AZs and returns the first one. Exits cleanly with an
    actionable message if no AZ supports the instance.
    """
    ec2 = boto3.client("ec2", region_name=region)
    try:
        resp = ec2.describe_instance_type_offerings(
            LocationType="availability-zone",
            Filters=[{"Name": "instance-type", "Values": [instance_type]}],
        )
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[red]Failed to check AZ support for {instance_type}:[/red] "
            f"{_aws_error_message(e, 'ec2:DescribeInstanceTypeOfferings')}"
        )
        sys.exit(1)
    azs = sorted(o["Location"] for o in resp.get("InstanceTypeOfferings", []))
    if not azs:
        console.print(
            f"[red]No availability zones in {region} support {instance_type}.[/red] "
            "Pick a different instance type or region (try `aws ec2 describe-instance-type-offerings "
            "--location-type availability-zone --filters Name=instance-type,Values=" + instance_type + "`)."
        )
        sys.exit(1)
    return azs[0]


def check_availability(instances: list[dict], region: str) -> list[dict]:
    """Keep only instance types available in the given region via AWS API."""
    ec2 = boto3.client("ec2", region_name=region)
    type_names = list({i["instance_type"] for i in instances})

    available_types: set[str] = set()
    try:
        # API only accepts 100 per call
        for start in range(0, len(type_names), 100):
            batch = type_names[start : start + 100]
            paginator = ec2.get_paginator("describe_instance_type_offerings")
            for page in paginator.paginate(
                LocationType="region",
                Filters=[{"Name": "instance-type", "Values": batch}],
            ):
                for offering in page["InstanceTypeOfferings"]:
                    available_types.add(offering["InstanceType"])
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[red]Failed to query EC2 availability in {region}:[/red] "
            f"{_aws_error_message(e, 'ec2:DescribeInstanceTypeOfferings')}"
        )
        sys.exit(1)

    available = []
    for inst in instances:
        if inst["instance_type"] in available_types:
            available.append(inst)
        else:
            console.print(
                f"  [dim]Skipping {inst['instance_type']} — not available in {region}[/dim]"
            )
    return available


# ---------------------------------------------------------------------------
# Pricing
# ---------------------------------------------------------------------------

# Map region code to the location name used by the Pricing API
REGION_NAME_MAP = {
    "us-east-1": "US East (N. Virginia)",
    "us-east-2": "US East (Ohio)",
    "us-west-1": "US West (N. California)",
    "us-west-2": "US West (Oregon)",
    "eu-west-1": "Europe (Ireland)",
    "eu-central-1": "Europe (Frankfurt)",
    "ap-southeast-1": "Asia Pacific (Singapore)",
    "ap-northeast-1": "Asia Pacific (Tokyo)",
}


def _find_latest_cache(region: str, source: str) -> tuple[Path, datetime] | None:
    """Return (path, mtime_utc) for the most recent pricing cache file for (region, source).

    Returns None if no matching cache file exists. The caller is responsible for
    deciding whether the file is fresh enough to use.
    """
    if not PRICING_CACHE_DIR.exists():
        return None
    prefix = f"pricing_{region}_{source}_"
    latest: tuple[Path, datetime] | None = None
    for f in PRICING_CACHE_DIR.iterdir():
        if not f.name.startswith(prefix) or not f.name.endswith(".json"):
            continue
        # Extract timestamp from filename: pricing_<region>_<source>_<YYYYMMDD-HHMMSS>.json
        ts_part = f.name[len(prefix):-len(".json")]
        try:
            file_time = datetime.strptime(ts_part, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if latest is None or file_time > latest[1]:
            latest = (f, file_time)
    return latest


def _prompt_yes_no(message: str, default_yes: bool = True) -> bool:
    """Prompt the user for a yes/no answer. Empty input takes the default."""
    suffix = " [Y/n]: " if default_yes else " [y/N]: "
    answer = console.input(message + suffix).strip().lower()
    if not answer:
        return default_yes
    return answer in ("y", "yes")


def _save_pricing_cache(region: str, source: str, prices: dict[str, float | None]) -> None:
    """Write pricing data to a timestamped cache file (tagged with source).

    Skips writing when every price is None — that indicates the upstream call
    failed for every instance type (e.g. IAM AccessDenied for aws-api, or a
    network error for vantage) and writing the all-null result would poison
    the 24h cache window.
    """
    if not any(v is not None for v in prices.values()):
        return
    PRICING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cache_path = PRICING_CACHE_DIR / f"pricing_{region}_{source}_{ts}.json"
    with open(cache_path, "w") as f:
        json.dump(prices, f, indent=2)


def _load_pricing_cache(cache_path: Path) -> dict[str, float | None]:
    """Load pricing data from a cache file.

    If the file is corrupted, delete it and return an empty dict so the next
    `fetch_pricing` call refreshes — one bad cache shouldn't crash the tool.
    """
    try:
        with open(cache_path) as f:
            raw = json.load(f)
        return {k: (float(v) if v is not None else None) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as e:
        console.print(
            f"  [yellow]Pricing cache {cache_path.name} unreadable ({type(e).__name__}: {e}); "
            "removing it and re-fetching.[/yellow]"
        )
        try:
            cache_path.unlink()
        except OSError:
            pass
        return {}


def fetch_pricing(
    instance_types: list[str], region: str, source: str
) -> dict[str, float | None]:
    """Dispatch on-demand pricing fetch to the chosen source.

    Sources:
      - "vantage":  public instances.json (no AWS auth required)
      - "aws-api":  AWS Pricing API via boto3 (requires pricing:GetProducts IAM permission)
      - "none":     skip pricing entirely (all values None)
    """
    if source == "none":
        return {it: None for it in instance_types}
    if source == "vantage":
        return fetch_pricing_vantage(instance_types, region)
    if source == "aws-api":
        return fetch_pricing_aws_api(instance_types, region)
    raise ValueError(
        f"Unknown pricing source {source!r}; expected one of {PRICING_SOURCES}"
    )


def fetch_pricing_vantage(
    instance_types: list[str], region: str
) -> dict[str, float | None]:
    """Fetch on-demand prices from Vantage's public instances.json.

    No AWS auth needed. Downloads ~200 MB on cache miss; cached for 24h via
    the standard cache layer.
    """
    console.print("  Downloading public pricing from Vantage (~200 MB)...")
    try:
        resp = requests.get(VANTAGE_INSTANCES_URL, timeout=120)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        console.print(
            f"  [yellow]Vantage pricing fetch failed: {type(e).__name__}: {e}[/yellow]"
        )
        return {it: None for it in instance_types}

    want = set(instance_types)
    prices: dict[str, float | None] = {it: None for it in instance_types}
    for inst in data:
        itype = inst.get("instance_type")
        if itype not in want:
            continue
        try:
            prices[itype] = float(inst["pricing"][region]["linux"]["ondemand"])
        except (KeyError, TypeError, ValueError):
            prices[itype] = None
    return prices


def fetch_pricing_aws_api(
    instance_types: list[str], region: str
) -> dict[str, float | None]:
    """Fetch on-demand hourly pricing via the AWS Pricing API.

    Requires the calling IAM principal to have ``pricing:GetProducts``.
    Returns a dict mapping instance_type -> price_per_hour (USD), or None if
    the price could not be determined.
    """
    # The Pricing API is only available in us-east-1 and ap-south-1
    pricing = boto3.client("pricing", region_name="us-east-1")
    location = REGION_NAME_MAP.get(region)
    if location is None:
        console.print(
            f"  [yellow]Region {region!r} is not in REGION_NAME_MAP; "
            "Pricing API would return wrong results. Skipping pricing for this run — "
            "consider --pricing-source=vantage which supports all regions.[/yellow]"
        )
        return {it: None for it in instance_types}

    prices: dict[str, float | None] = {}
    first_error: Exception | None = None
    for itype in instance_types:
        try:
            resp = pricing.get_products(
                ServiceCode="AmazonEC2",
                Filters=[
                    {"Type": "TERM_MATCH", "Field": "instanceType", "Value": itype},
                    {"Type": "TERM_MATCH", "Field": "location", "Value": location},
                    {"Type": "TERM_MATCH", "Field": "operatingSystem", "Value": "Linux"},
                    {"Type": "TERM_MATCH", "Field": "tenancy", "Value": "Shared"},
                    {"Type": "TERM_MATCH", "Field": "preInstalledSw", "Value": "NA"},
                    {"Type": "TERM_MATCH", "Field": "capacitystatus", "Value": "Used"},
                ],
                MaxResults=1,
            )
            if resp["PriceList"]:
                product = json.loads(resp["PriceList"][0])
                on_demand = product.get("terms", {}).get("OnDemand", {})
                for term in on_demand.values():
                    for dim in term.get("priceDimensions", {}).values():
                        usd = dim.get("pricePerUnit", {}).get("USD")
                        if usd:
                            prices[itype] = float(usd)
                            break
                    if itype in prices:
                        break
            if itype not in prices:
                prices[itype] = None
        except Exception as e:
            if first_error is None:
                first_error = e
            prices[itype] = None
    if first_error is not None:
        console.print(
            f"  [yellow]Pricing API call failed: {type(first_error).__name__}: {first_error}[/yellow]"
        )
    return prices


def _resolve_pricing(
    instance_types: list[str], region: str, source: str
) -> dict[str, float | None]:
    """Return prices for the requested instance types, using the cache when appropriate.

    Behavior by cache age:
      - No cache → fetch fresh silently.
      - Cache < 24h old → use silently; backfill any newly-requested instance types.
      - Cache ≥ 24h old → prompt the user before refreshing. If the user declines,
        the stale cache is used as-is.
    """
    latest = _find_latest_cache(region, source)
    now = datetime.now(timezone.utc)

    if latest is None:
        prices = fetch_pricing(instance_types, region, source)
        _save_pricing_cache(region, source, prices)
        return prices

    cache_path, file_time = latest
    age_hours = (now - file_time).total_seconds() / 3600

    if age_hours < 24:
        console.print(
            f"  Using cached pricing from {cache_path.name} ({age_hours:.1f}h old)"
        )
        prices = _load_pricing_cache(cache_path)
        missing = [t for t in instance_types if t not in prices]
        if missing:
            console.print(f"  Fetching {len(missing)} uncached prices...")
            fresh = fetch_pricing(missing, region, source)
            prices.update(fresh)
            _save_pricing_cache(region, source, prices)
        return prices

    # Stale cache: ask the user before refreshing.
    console.print(
        f"  [yellow]Cached pricing is {age_hours:.1f}h old "
        f"(file: {cache_path.name}).[/yellow]"
    )
    if _prompt_yes_no(f"  Refresh from '{source}' for the latest prices?", default_yes=True):
        prices = fetch_pricing(instance_types, region, source)
        _save_pricing_cache(region, source, prices)
        return prices

    console.print(f"  [yellow]Keeping stale cache.[/yellow]")
    return _load_pricing_cache(cache_path)


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

GPU_TYPE_SORT_ORDER = {
    "NVIDIA K80": 0,
    "NVIDIA M60": 1,
    "NVIDIA T4": 2,
    "NVIDIA T4g": 3,
    "AMD Radeon Pro V520": 4,
    "NVIDIA A10G": 5,
    "NVIDIA V100": 6,
    "NVIDIA L4": 7,
    "NVIDIA L40S": 8,
    "NVIDIA A100": 9,
    "NVIDIA RTX PRO Server 6000": 10,
    "NVIDIA H100": 11,
    "NVIDIA H200": 12,
    "NVIDIA B200": 13,
    "NVIDIA B300": 14,
}


def _gpu_sort_key(inst: dict) -> tuple:
    gpu_rank = GPU_TYPE_SORT_ORDER.get(inst.get("gpu_type", ""), 99)
    return (gpu_rank, inst.get("gpu_memory_total_gib", 0), inst.get("vcpus", 0))


def display_table(instances: list[dict], prices: dict[str, float | None]) -> None:
    """Print a rich table of GPU instances with pricing."""
    table = Table(title="Available GPU Instances", show_lines=False)
    table.add_column("#", justify="right", style="cyan", no_wrap=True)
    table.add_column("Instance Type", style="green")
    table.add_column("Gen", justify="center")
    table.add_column("GPU Type", style="magenta")
    table.add_column("GPUs", justify="right")
    table.add_column("GPU Mem (GiB)", justify="right")
    table.add_column("vCPUs", justify="right")
    table.add_column("RAM (GiB)", justify="right")
    table.add_column("$/hr", justify="right", style="yellow")

    last_gpu_type: str | None = None
    for idx, inst in enumerate(instances, 1):
        gpu_type = inst.get("gpu_type", "?")
        if last_gpu_type is not None and gpu_type != last_gpu_type:
            table.add_section()
        last_gpu_type = gpu_type
        gen_label = "prev" if inst.get("generation_status") == "previous" else "curr"
        price = prices.get(inst["instance_type"])
        price_str = f"{price:.2f}" if price is not None else "n/a"
        table.add_row(
            str(idx),
            inst["instance_type"],
            gen_label,
            gpu_type,
            str(inst.get("gpu_count", "?")),
            str(inst.get("gpu_memory_total_gib", "?")),
            str(inst.get("vcpus", "?")),
            str(inst.get("system_memory_gib", "?")),
            price_str,
        )

    console.print(table)


STORAGE_TYPES = ("s3", "ebs", "efs")
DEFAULT_STORAGE_TYPE = "ebs"
DEFAULT_STORAGE_SIZE_GB = 100

# Maps the user-friendly --ami choice to the actual AWS AMI Name filter pattern.
# "pytorch"  → full DLAMI with PyTorch + CUDA + cuDNN + NCCL + drivers (recommended default)
# "tensorflow" → full DLAMI with TensorFlow + CUDA + cuDNN + NCCL + drivers
# "base"     → minimal DLAMI: NVIDIA OSS drivers + CUDA + cuDNN, no frameworks
AMI_PATTERNS = {
    "pytorch": "Deep Learning OSS Nvidia Driver AMI GPU PyTorch * (Ubuntu 22.04)*",
    "tensorflow": "Deep Learning OSS Nvidia Driver AMI GPU TensorFlow * (Ubuntu 22.04)*",
    "base": "Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*",
}
DEFAULT_AMI = "pytorch"


def _probe_storage_permissions(storage_type: str, region: str) -> str | None:
    """Probe whether current AWS credentials can create the chosen storage type.

    For S3 + IAM, actually attempts CreateBucket / CreateRole (and immediately
    rolls back) since list/describe perms are not a reliable proxy for create
    perms — some accounts grant read but not write. For EBS / EFS, uses
    describe-API smoke tests because real create-and-delete probes have
    measurable cost or take noticeably longer.

    Returns None if the probe passes, or a human-readable explanation if not.
    """
    if storage_type == "none":
        return None

    import uuid

    from botocore.exceptions import ClientError

    def _is_denied(exc: ClientError) -> bool:
        code = exc.response.get("Error", {}).get("Code", "")
        return code in (
            "AccessDenied",
            "AccessDeniedException",
            "UnauthorizedOperation",
        )

    if storage_type == "s3":
        # Real CreateBucket + DeleteBucket probe (S3 buckets are free).
        # The app does not create or modify IAM — the user supplies their
        # own existing instance profile with S3 access, so we don't probe IAM.
        s3 = boto3.client("s3", region_name=region)
        test_bucket = f"provisioner-probe-{uuid.uuid4().hex[:20]}"
        created = False
        try:
            if region == "us-east-1":
                s3.create_bucket(Bucket=test_bucket)
            else:
                s3.create_bucket(
                    Bucket=test_bucket,
                    CreateBucketConfiguration={"LocationConstraint": region},
                )
            created = True
        except ClientError as e:
            if _is_denied(e):
                return (
                    "Missing s3:CreateBucket. Need S3 perms (s3:CreateBucket, "
                    "s3:DeleteBucket, s3:PutBucketTagging, s3:GetBucketLocation) "
                    "to create the workspace's storage bucket."
                )
            return _aws_error_message(e, "s3:CreateBucket")
        except Exception as e:  # noqa: BLE001 - credential/network/etc.
            return _aws_error_message(e, "s3:CreateBucket")
        if created:
            try:
                s3.delete_bucket(Bucket=test_bucket)
            except Exception as e:  # noqa: BLE001
                console.print(
                    f"  [yellow]Probe bucket {test_bucket} could not be deleted "
                    f"({type(e).__name__}). Clean up manually: "
                    f"aws s3 rb s3://{test_bucket} --region {region}[/yellow]"
                )
        return None

    if storage_type == "ebs":
        try:
            boto3.client("ec2", region_name=region).describe_volumes(MaxResults=5)
        except ClientError as e:
            if _is_denied(e):
                return (
                    "EBS access is denied. Need ec2:CreateVolume, ec2:AttachVolume, "
                    "ec2:DescribeVolumes, ec2:DeleteVolume."
                )
            return _aws_error_message(e, "ec2:DescribeVolumes")
        except Exception as e:  # noqa: BLE001
            return _aws_error_message(e, "ec2:DescribeVolumes")
        return None

    if storage_type == "efs":
        try:
            boto3.client("efs", region_name=region).describe_file_systems(MaxItems=1)
        except ClientError as e:
            if _is_denied(e):
                return (
                    "EFS access is denied. Need elasticfilesystem:CreateFileSystem, "
                    "CreateMountTarget, DescribeFileSystems, DescribeMountTargets, "
                    "DeleteFileSystem, DeleteMountTarget."
                )
            return _aws_error_message(e, "elasticfilesystem:DescribeFileSystems")
        except Exception as e:  # noqa: BLE001
            return _aws_error_message(e, "elasticfilesystem:DescribeFileSystems")
        return None

    return None


def prompt_storage_options(region: str = DEFAULT_REGION) -> tuple[str, int]:
    """Ask the user for storage type and size.

    Probes permissions for every type upfront, then shows availability next to
    each option in the prompt and defaults to the first available type
    (preferring s3 > ebs > efs > none). The user can still pick an unavailable
    type and will see the specific error before re-prompting.

    This function does NOT ask about IAM — that is the user's responsibility.
    If you want an IAM instance profile attached (e.g. for mountpoint-s3 to
    authenticate when storage_type=s3), pass it via the --iam-instance-profile
    CLI flag.

    Returns (storage_type, storage_size_gb). storage_size_gb is honored for
    EBS data volumes; for S3 and EFS it is informational.
    """
    console.print()
    console.print("  Checking AWS permissions for each storage type...")
    availability: dict[str, str | None] = {}
    for t in STORAGE_TYPES:
        availability[t] = _probe_storage_permissions(t, region)

    # Default to the first available type in preference order. The default
    # storage type comes first; if it's unavailable we fall through to the
    # other supported types.
    preference = (DEFAULT_STORAGE_TYPE,) + tuple(t for t in STORAGE_TYPES if t != DEFAULT_STORAGE_TYPE)
    default_type = next(
        (t for t in preference if availability.get(t) is None), DEFAULT_STORAGE_TYPE
    )

    # Format option list with availability markers.
    option_strs = []
    for t in STORAGE_TYPES:
        if availability[t] is None:
            option_strs.append(f"[green]{t}[/green]")
        else:
            option_strs.append(f"[red]{t}[/red] (no perms)")

    while True:
        console.print()
        raw_type = console.input(
            f"[bold]Storage type[/bold] ({', '.join(option_strs)}) "
            f"[dim]default: {default_type}, 'q' to quit[/dim]: "
        ).strip().lower()
        if raw_type in ("q", "quit"):
            console.print("Cancelled.")
            sys.exit(0)
        storage_type = raw_type or default_type
        if storage_type not in STORAGE_TYPES:
            console.print(
                f"  [yellow]Unknown storage type {raw_type!r}; pick one of "
                f"{', '.join(STORAGE_TYPES)}.[/yellow]"
            )
            continue
        if availability[storage_type] is not None:
            console.print(f"  [red]Permission check failed:[/red] {availability[storage_type]}")
            console.print(
                "  [yellow]Pick a different storage type.[/yellow]"
            )
            continue
        break

    while True:
        raw_size = console.input(
            f"[bold]Storage size in GB[/bold] "
            f"[dim]default: {DEFAULT_STORAGE_SIZE_GB}[/dim]: "
        ).strip()
        if not raw_size:
            size_gb = DEFAULT_STORAGE_SIZE_GB
            break
        try:
            size_gb = int(raw_size)
        except ValueError:
            console.print(
                f"  [yellow]Invalid size {raw_size!r} — enter a positive integer "
                f"(or press Enter for {DEFAULT_STORAGE_SIZE_GB} GB).[/yellow]"
            )
            continue
        if size_gb <= 0:
            console.print("  [yellow]Size must be positive.[/yellow]")
            continue
        if size_gb > 16000:
            console.print(
                f"  [yellow]{size_gb} GB exceeds the gp3 maximum (16 TB). "
                "Pick a smaller value.[/yellow]"
            )
            continue
        break

    console.print(f"  Selected: [cyan]{storage_type}[/cyan], [cyan]{size_gb} GB[/cyan]\n")
    return storage_type, size_gb


def get_user_selection(instances: list[dict]) -> dict:
    """Prompt the user to pick an instance by number."""
    while True:
        try:
            choice = console.input(
                "\n[bold]Enter instance number (or 'q' to quit): [/bold]"
            )
            if choice.strip().lower() == "q":
                console.print("Cancelled.")
                sys.exit(0)
            idx = int(choice)
            if 1 <= idx <= len(instances):
                return instances[idx - 1]
            console.print(f"[red]Please enter a number between 1 and {len(instances)}[/red]")
        except ValueError:
            console.print("[red]Invalid input — enter a number.[/red]")


# ---------------------------------------------------------------------------
# Workspace helpers
# ---------------------------------------------------------------------------


def _normalize_instance_type(instance_type: str) -> str:
    return instance_type.replace(".", "-")


def create_workspace(instance_type: str) -> Path:
    """Create a new workspace directory for a provisioned instance."""
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    name = f"{_normalize_instance_type(instance_type)}-{ts}"
    ws = WORKSPACES_DIR / name
    ws.mkdir(parents=True, exist_ok=True)
    # Copy terraform templates into workspace (.tf configs + any .tpl files
    # referenced by templatefile() calls).
    for pattern in ("*.tf", "*.tpl"):
        for src in TERRAFORM_TEMPLATE_DIR.glob(pattern):
            shutil.copy2(src, ws / src.name)
    return ws


def create_key_pair(workspace_name: str, workspace_dir: Path, region: str) -> str:
    """Create an AWS key pair and save the .pem file. Returns the key pair name.

    On failure, surfaces a clear error message and exits — the caller does not
    have to worry about cleanup since the .pem file is only written after the
    API call succeeds.
    """
    ec2 = boto3.client("ec2", region_name=region)
    key_name = workspace_name
    try:
        response = ec2.create_key_pair(KeyName=key_name, KeyType="rsa", KeyFormat="pem")
    except Exception as e:  # noqa: BLE001
        console.print(
            f"[red]Failed to create AWS key pair '{key_name}':[/red] "
            f"{_aws_error_message(e, 'ec2:CreateKeyPair')}"
        )
        sys.exit(1)
    pem_path = workspace_dir / f"{key_name}.pem"
    try:
        # Atomic create at mode 0o400 so a Ctrl+C between write and chmod
        # can never leave the .pem with default-umask permissions.
        fd = os.open(str(pem_path), os.O_CREAT | os.O_WRONLY | os.O_EXCL, 0o400)
        try:
            os.write(fd, response["KeyMaterial"].encode())
        finally:
            os.close(fd)
    except OSError as e:
        # File system error AFTER AWS already created the key pair — try to
        # roll back the AWS side so we don't orphan an unusable key pair.
        console.print(f"[red]Failed to write {pem_path} ({e}); rolling back AWS key pair.[/red]")
        try:
            ec2.delete_key_pair(KeyName=key_name)
        except Exception:  # noqa: BLE001
            console.print(
                f"[yellow]Could not clean up AWS key pair '{key_name}' — delete manually: "
                f"aws ec2 delete-key-pair --key-name {key_name} --region {region}[/yellow]"
            )
        sys.exit(1)
    return key_name


def delete_key_pair(key_name: str, region: str) -> None:
    """Delete an AWS key pair. Idempotent on NotFound; warns loudly on permission errors."""
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ec2.delete_key_pair(KeyName=key_name)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "InvalidKeyPair.NotFound":
            return  # already gone
        if code in ("AccessDenied", "UnauthorizedOperation"):
            console.print(
                f"[red]Could not delete key pair '{key_name}' (missing "
                f"ec2:DeleteKeyPair). Delete manually: "
                f"aws ec2 delete-key-pair --key-name {key_name} --region {region}[/red]"
            )
            return
        console.print(
            f"[yellow]Warning: could not delete key pair '{key_name}' "
            f"({code or type(e).__name__}: {e})[/yellow]"
        )
    except Exception as e:  # noqa: BLE001
        console.print(f"[yellow]Warning: could not delete key pair '{key_name}': {e}[/yellow]")


def get_my_public_ip() -> str:
    """Fetch the caller's public IP, with a manual fallback prompt on failure.

    Tries checkip.amazonaws.com first, then ifconfig.me as a backup, then asks
    the user to enter their public IP / CIDR. Returns the IP as a plain string
    (no /32 suffix — the caller appends that). Validates the result is a real
    IPv4 address so a captive-portal HTML response can't leak into the SG rule.
    """
    import ipaddress

    candidates = ("https://checkip.amazonaws.com", "https://ifconfig.me/ip")
    last_err = ""
    for url in candidates:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            candidate = resp.text.strip()
            ipaddress.IPv4Address(candidate)
            return candidate
        except Exception as e:  # noqa: BLE001
            last_err = f"{type(e).__name__}: {e}"
            continue

    console.print(
        f"  [yellow]Could not auto-detect your public IP ({last_err}). "
        "Enter it manually below (e.g. 1.2.3.4) or type 'q' to cancel.[/yellow]"
    )
    while True:
        answer = console.input("[bold]Your public IP[/bold]: ").strip()
        if answer.lower() in ("q", "quit"):
            console.print("Cancelled.")
            sys.exit(0)
        # Accept either bare IP or CIDR — strip the netmask for validation.
        ip_only = answer.split("/")[0]
        try:
            ipaddress.IPv4Address(ip_only)
            return ip_only
        except ValueError:
            console.print(f"  [red]'{answer}' is not a valid IPv4 address — try again.[/red]")


def _ami_arch_for_instance(instance: dict) -> str:
    """Return 'arm64' or 'x86_64' based on the instance's CPU architecture."""
    arch = instance.get("cpu_architecture", "")
    if "arm64" in arch.lower() or "graviton" in arch.lower() or "grace" in arch.lower():
        return "arm64"
    return "x86_64"


def write_tfvars(workspace_dir: Path, variables: dict) -> None:
    """Write a terraform.tfvars.json file."""
    tfvars_path = workspace_dir / "terraform.tfvars.json"
    with open(tfvars_path, "w") as f:
        json.dump(variables, f, indent=2)


# ---------------------------------------------------------------------------
# Terraform execution
# ---------------------------------------------------------------------------


def _check_terraform() -> str:
    """Verify terraform is available and return its path."""
    tf = shutil.which("terraform")
    if not tf:
        console.print(
            "[red]Error: 'terraform' not found on PATH. "
            "Install it from https://developer.hashicorp.com/terraform/install[/red]"
        )
        sys.exit(1)
    return tf


def _check_ssh_tools() -> None:
    """Verify ssh + scp are on PATH. Exit cleanly with an actionable message if not.

    Required for the recipe-install path (_wait_for_ssh, _wait_for_cloud_init,
    install_recipe_on_instance). Without this check, subprocess.run raises a
    bare FileNotFoundError on hosts without OpenSSH (some minimal containers /
    Windows without OpenSSH client).
    """
    missing = [tool for tool in ("ssh", "scp") if shutil.which(tool) is None]
    if missing:
        console.print(
            f"[red]Error: {', '.join(missing)} not found on PATH.[/red] "
            "Install an OpenSSH client (macOS: built-in; Ubuntu/Debian: "
            "`sudo apt install openssh-client`; Windows: `winget install OpenSSH.Client` "
            "or use WSL)."
        )
        sys.exit(1)


_INCOMPLETE_LOCK_WARNING_RE = re.compile(
    r"╷\s*\n(?:│[^\n]*\n)*?│\s*Warning: Incomplete lock file information"
    r"(?:[^╵])*?╵\s*\n?",
    re.DOTALL,
)


def _filter_terraform_init_output(text: str) -> str:
    """Strip the cosmetic 'Incomplete lock file information' warning emitted by

    terraform init when using our filesystem_mirror (the mirror only ships the
    darwin_arm64 binary, so terraform helpfully warns the lock file is
    platform-incomplete — expected and not actionable for end users here).
    """
    return _INCOMPLETE_LOCK_WARNING_RE.sub("", text)


def run_terraform(
    workspace_dir: Path, *args: str, check: bool = True
) -> subprocess.CompletedProcess:
    """Run a terraform command in the given workspace directory.

    By default exits the process on non-zero return code (the historical
    behavior). Callers that want to handle the failure themselves — notably
    destroy_flow, where we want to still delete the key pair even if
    `terraform destroy` partially failed — can pass `check=False`.
    """
    tf = _check_terraform()
    cmd = [tf] + list(args)
    console.print(f"[dim]Running: {' '.join(cmd)} (in {workspace_dir})[/dim]")
    # For `init`, capture + filter the cosmetic Incomplete-lock warning.
    # Other commands stream live so the user sees progress in real time.
    if args and args[0] == "init":
        result = subprocess.run(
            cmd, cwd=workspace_dir, capture_output=True, text=True
        )
        sys.stdout.write(_filter_terraform_init_output(result.stdout))
        sys.stdout.flush()
        if result.stderr:
            sys.stderr.write(result.stderr)
            sys.stderr.flush()
    else:
        result = subprocess.run(cmd, cwd=workspace_dir)
    if result.returncode != 0:
        console.print(f"[red]Terraform command failed (exit {result.returncode})[/red]")
        if check:
            console.print(
                f"  [yellow]AWS resources may have been partially created in "
                f"{workspace_dir.name}. Run [bold]uv run provision.py --destroy[/bold] "
                f"and pick this workspace to clean up.[/yellow]"
            )
            sys.exit(result.returncode)
    return result


def get_terraform_outputs(workspace_dir: Path) -> dict:
    """Parse terraform output as JSON. Returns {} on any failure (the caller decides)."""
    tf = _check_terraform()
    try:
        result = subprocess.run(
            [tf, "output", "-json"],
            cwd=workspace_dir,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.TimeoutExpired:
        console.print("[red]terraform output -json timed out after 60s.[/red]")
        return {}
    if result.returncode != 0:
        console.print(f"[red]Failed to read terraform outputs: {result.stderr}[/red]")
        return {}
    try:
        raw = json.loads(result.stdout or "{}")
    except json.JSONDecodeError as e:
        console.print(f"[red]terraform output returned malformed JSON: {e}[/red]")
        return {}
    return {k: v.get("value") for k, v in raw.items()}


# ---------------------------------------------------------------------------
# Metadata
# ---------------------------------------------------------------------------


def save_metadata(workspace_dir: Path, metadata: dict) -> None:
    meta_path = workspace_dir / METADATA_FILE
    with open(meta_path, "w") as f:
        json.dump(metadata, f, indent=2)


def load_all_workspaces(include_incomplete: bool = False) -> list[dict]:
    """Scan workspaces/ for provisioned instances.

    If include_incomplete is True, also returns workspaces that have Terraform
    state but no metadata.json (e.g. cancelled mid-provision). Corrupt JSON in
    any single workspace just causes that one to be skipped with a warning —
    it does not break --list / --destroy for the other workspaces.
    """
    results = []
    if not WORKSPACES_DIR.exists():
        return results
    for ws in sorted(WORKSPACES_DIR.iterdir()):
        if not ws.is_dir():
            continue
        meta_path = ws / METADATA_FILE
        if meta_path.exists():
            try:
                with open(meta_path) as f:
                    meta = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                console.print(
                    f"  [yellow]Skipping {ws.name}: metadata.json is unreadable ({e}).[/yellow]"
                )
                continue
            meta["workspace_dir"] = str(ws)
            meta["incomplete"] = False
            results.append(meta)
        elif include_incomplete and (ws / "terraform.tfvars.json").exists():
            # Incomplete workspace — read what we can from tfvars
            tfvars_path = ws / "terraform.tfvars.json"
            try:
                with open(tfvars_path) as f:
                    tfvars = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                console.print(
                    f"  [yellow]Skipping {ws.name}: terraform.tfvars.json is unreadable ({e}).[/yellow]"
                )
                continue
            results.append({
                "workspace_name": ws.name,
                "workspace_dir": str(ws),
                "instance_type": tfvars.get("instance_type", "?"),
                "region": tfvars.get("region", DEFAULT_REGION),
                "key_pair_name": tfvars.get("key_pair_name"),
                "incomplete": True,
            })
    return results


# ---------------------------------------------------------------------------
# Recipes
# ---------------------------------------------------------------------------


def load_recipes() -> list[dict]:
    """Scan recipes/ for subdirectories containing recipe.yaml.

    Malformed recipe.yaml files are skipped with a warning so one bad recipe
    can't break the install flow.
    """
    recipes = []
    if not RECIPES_DIR.exists():
        return recipes
    for d in sorted(RECIPES_DIR.iterdir()):
        if not d.is_dir():
            continue
        recipe_file = d / "recipe.yaml"
        if recipe_file.exists():
            try:
                with open(recipe_file) as f:
                    recipe = yaml.safe_load(f)
            except (OSError, yaml.YAMLError) as e:
                console.print(f"  [yellow]Skipping recipe {d.name}: {e}[/yellow]")
                continue
            if not isinstance(recipe, dict):
                console.print(
                    f"  [yellow]Skipping recipe {d.name}: recipe.yaml must be a YAML mapping.[/yellow]"
                )
                continue
            recipe["_dir"] = d
            recipe["_install_script"] = d / "install.sh"
            recipes.append(recipe)
    return recipes


def _parse_recipe_selection(selection: str, count: int) -> tuple[list[int], list[str]]:
    """Parse a comma-separated recipe selection like '1, 3' into (indices, rejected).

    indices are 0-based and in [0, count). rejected is the list of tokens we
    couldn't make sense of, so the caller can surface them to the user.
    """
    indices: list[int] = []
    rejected: list[str] = []
    for raw in selection.split(","):
        part = raw.strip()
        if not part:
            continue
        if not part.isdigit():
            rejected.append(part)
            continue
        n = int(part)
        if 1 <= n <= count:
            indices.append(n - 1)
        else:
            rejected.append(part)
    return indices, rejected


def get_compatible_recipes(gpu_vendor: str | None) -> list[dict]:
    """Return recipes compatible with the given GPU vendor."""
    recipes = load_recipes()
    if not gpu_vendor:
        return recipes
    return [
        r for r in recipes
        if not r.get("gpu_vendors") or gpu_vendor in r["gpu_vendors"]
    ]


def display_recipes(recipes: list[dict]) -> None:
    """Print a table of available recipes."""
    table = Table(title="Available Recipes")
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Name", style="green")
    table.add_column("Description")
    table.add_column("Version", justify="center")
    for idx, r in enumerate(recipes, 1):
        table.add_row(
            str(idx),
            r.get("name", "?"),
            r.get("description", ""),
            str(r.get("version", "")),
        )
    console.print(table)


def _wait_for_ssh(
    pem_path: Path, public_ip: str, timeout: int = 300
) -> bool:
    """Poll SSH on the instance until it accepts a connection or timeout elapses."""
    _check_ssh_tools()
    console.print(f"  Waiting for SSH on [cyan]{public_ip}[/cyan] (up to {timeout}s)...")
    ssh_opts = [
        "-i", str(pem_path),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=5",
        "-o", "BatchMode=yes",
    ]
    deadline = time.time() + timeout
    attempt = 0
    while time.time() < deadline:
        attempt += 1
        result = subprocess.run(
            ["ssh", *ssh_opts, f"ubuntu@{public_ip}", "true"],
            capture_output=True,
        )
        if result.returncode == 0:
            console.print(f"  [green]SSH ready (after {attempt} attempt(s)).[/green]")
            return True
        time.sleep(5)
    console.print(f"[red]  SSH did not come up within {timeout}s.[/red]")
    return False


def _wait_for_cloud_init(
    pem_path: Path, public_ip: str, timeout: int = 600
) -> bool:
    """Wait for cloud-init to finish on the instance so user_data has completed."""
    console.print("  Waiting for cloud-init to finish (uv install + storage setup)...")
    ssh_opts = [
        "-i", str(pem_path),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=20",
    ]
    try:
        result = subprocess.run(
            ["ssh", *ssh_opts, f"ubuntu@{public_ip}", "sudo cloud-init status --wait"],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        console.print(f"[yellow]  cloud-init still running after {timeout}s; continuing anyway.[/yellow]")
        return False
    lines = (result.stdout or "").strip().splitlines()
    status_line = lines[-1] if lines else ""
    if "done" in (result.stdout or ""):
        console.print("  [green]cloud-init done.[/green]")
    else:
        console.print(
            f"[yellow]  cloud-init {status_line or 'status unknown'}; continuing anyway.[/yellow]"
        )
    return result.returncode == 0


def install_recipe_on_instance(
    pem_path: Path, public_ip: str, recipe: dict
) -> bool:
    """SCP the install script to the instance and run it via SSH.

    Uses BatchMode + ConnectTimeout + ServerAliveInterval so transient network
    issues fail fast with a real error instead of hanging. The SCP step is
    retried a few times to ride out the occasional sshd-restarting-during-
    cloud-init blip; the install command itself is run live (no retry) and
    capped at one hour.
    """
    script = recipe["_install_script"]
    if not script.exists():
        console.print(f"[red]Install script not found: {script}[/red]")
        return False

    remote_script = f"/tmp/{script.name}"
    ssh_opts = [
        "-i", str(pem_path),
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
        "-o", "ConnectTimeout=15",
        "-o", "BatchMode=yes",
        "-o", "ServerAliveInterval=30",
        "-o", "ServerAliveCountMax=20",
    ]

    # SCP the script, retrying briefly on transient failures. 5-min per-attempt
    # cap prevents a stalled transfer from blocking for the full ServerAlive window.
    scp_cmd = ["scp", *ssh_opts, str(script), f"ubuntu@{public_ip}:{remote_script}"]
    for attempt in range(1, 4):
        try:
            result = subprocess.run(scp_cmd, timeout=300)
        except subprocess.TimeoutExpired:
            console.print(
                f"  [yellow]scp attempt {attempt}/3 timed out after 5 min; "
                "retrying in 10s...[/yellow]"
            )
            result = subprocess.CompletedProcess(scp_cmd, returncode=124)
        if result.returncode == 0:
            break
        if attempt < 3:
            console.print(
                f"  [yellow]scp attempt {attempt}/3 failed (exit {result.returncode}); "
                "retrying in 10s...[/yellow]"
            )
            time.sleep(10)
    if result.returncode != 0:
        console.print(
            f"[red]Failed to copy install script to {public_ip} after 3 attempts. "
            "Re-run `--install` once cloud-init / sshd is fully ready, or "
            "ssh manually to inspect the issue.[/red]"
        )
        return False

    # Run the install script. Cap at 1 hour so a hanging apt prompt eventually
    # surfaces instead of blocking forever.
    ssh_cmd = (
        ["ssh", *ssh_opts]
        + [f"ubuntu@{public_ip}", f"chmod +x {remote_script} && sudo bash {remote_script}"]
    )
    try:
        result = subprocess.run(ssh_cmd, timeout=3600)
    except subprocess.TimeoutExpired:
        console.print(
            f"[red]Recipe '{recipe.get('name')}' did not finish within 1h. "
            f"SSH in manually and check {remote_script} progress.[/red]"
        )
        return False
    if result.returncode != 0:
        console.print(
            f"[red]Recipe '{recipe.get('name')}' failed (exit {result.returncode}). "
            "See the ssh output above for the underlying error.[/red]"
        )
        return False

    return True


def prompt_and_install_recipes(
    pem_path: Path, public_ip: str, gpu_vendor: str | None
) -> None:
    """After provisioning, offer to install compatible recipes."""
    recipes = get_compatible_recipes(gpu_vendor)
    if not recipes:
        return

    answer = console.input(
        "\n[bold]Install software recipes on the instance? (y/n): [/bold]"
    )
    if answer.strip().lower() not in ("y", "yes"):
        return

    display_recipes(recipes)
    selection = console.input(
        "\n[bold]Enter recipe numbers (comma-separated, Enter or 'q' to skip): [/bold]"
    )
    stripped = selection.strip().lower()
    if stripped in ("", "q"):
        return

    indices, rejected = _parse_recipe_selection(selection, len(recipes))
    if rejected:
        console.print(
            f"  [yellow]Ignored: {', '.join(repr(r) for r in rejected)} — "
            "not valid recipe numbers.[/yellow]"
        )

    if not indices:
        console.print("[yellow]No valid recipes selected.[/yellow]")
        return

    # Wait until SSH is up and cloud-init has finished, so the recipe install
    # doesn't race the uv install / storage mount in user_data.
    if not _wait_for_ssh(pem_path, public_ip):
        console.print("[red]Cannot install recipes — SSH never came up.[/red]")
        return
    _wait_for_cloud_init(pem_path, public_ip)

    for i in indices:
        recipe = recipes[i]
        console.print(f"\n[bold]Installing {recipe.get('name')}...[/bold]")
        ok = install_recipe_on_instance(pem_path, public_ip, recipe)
        if ok:
            console.print(f"[green]{recipe.get('name')} installed successfully.[/green]")


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def provision_flow(
    json_path: str,
    pricing_source: str = DEFAULT_PRICING_SOURCE,
    region: str = DEFAULT_REGION,
    cli_iam_profile: str | None = None,
    ami_choice: str = DEFAULT_AMI,
) -> None:
    """Main provisioning flow: select instance → terraform apply → print SSH command."""
    # Preflight credentials so a missing/expired identity surfaces as a single
    # clear message instead of a deep boto3 traceback later.
    _verify_aws_credentials(region)

    console.print("[bold]Loading GPU instance catalog...[/bold]")
    instances = load_instances(json_path)
    instances = filter_instances(instances)
    console.print(f"  {len(instances)} non-fractional instances loaded")

    console.print(f"[bold]Checking availability in {region}...[/bold]")
    instances = check_availability(instances, region)
    if not instances:
        console.print(f"[red]No GPU instances available in {region}.[/red]")
        sys.exit(1)
    console.print(f"  {len(instances)} instances available\n")

    console.print(
        f"[bold]Fetching on-demand pricing[/bold] (source: {pricing_source})..."
    )
    type_names = [i["instance_type"] for i in instances]
    if pricing_source == "none":
        prices = {t: None for t in type_names}
    else:
        prices = _resolve_pricing(type_names, region, pricing_source)
    priced = sum(1 for t in type_names if prices.get(t) is not None)
    console.print(f"  {priced}/{len(type_names)} prices found\n")

    instances.sort(key=_gpu_sort_key)
    display_table(instances, prices)
    selected = get_user_selection(instances)
    storage_type, storage_size_gb = prompt_storage_options(region)
    # IAM instance profile is opt-in only via CLI flag; never prompted.
    iam_instance_profile_name = cli_iam_profile or ""

    console.print(f"\n[bold]Provisioning {selected['instance_type']}...[/bold]\n")

    # Pick an AZ that actually supports this instance type. Some GPU types
    # (e.g. g7e) are only in a subset of AZs in a region — without this,
    # terraform apply fails halfway with an opaque "Unsupported AZ" error.
    console.print(f"  Finding an AZ in {region} that supports {selected['instance_type']}...")
    az = pick_az_for_instance(selected["instance_type"], region)
    console.print(f"  Using AZ: [cyan]{az}[/cyan]")

    # Detect the user's public IP FIRST — if it fails (or they Ctrl+C the
    # manual-entry prompt), we haven't created any AWS resources yet, so
    # there's nothing to clean up.
    console.print("  Detecting your public IP...")
    my_ip = get_my_public_ip()
    console.print(f"  Your IP: {my_ip}")

    # Create workspace dir + key pair after the IP is known.
    ws = create_workspace(selected["instance_type"])
    workspace_name = ws.name
    console.print(f"  Workspace: [cyan]{workspace_name}[/cyan]")

    console.print("  Creating SSH key pair...")
    key_name = create_key_pair(workspace_name, ws, region)

    # Determine AMI architecture
    ami_arch = _ami_arch_for_instance(selected)

    # Write tfvars
    ami_name_pattern = AMI_PATTERNS[ami_choice]
    console.print(
        f"  AMI flavor: [cyan]{ami_choice}[/cyan]  "
        f"[dim](filter: {ami_name_pattern})[/dim]"
    )
    write_tfvars(ws, {
        "region": region,
        "availability_zone": az,
        "instance_type": selected["instance_type"],
        "key_pair_name": key_name,
        "workspace_name": workspace_name,
        "allowed_ssh_cidr": f"{my_ip}/32",
        "ami_architecture": ami_arch,
        "ami_name_pattern": ami_name_pattern,
        "storage_type": storage_type,
        "storage_size_gb": storage_size_gb,
        "iam_instance_profile_name": iam_instance_profile_name,
    })

    # Terraform init + apply
    console.print("\n[bold]Running terraform init...[/bold]")
    run_terraform(ws, "init")

    console.print("\n[bold]Running terraform apply...[/bold]")
    run_terraform(ws, "apply", "-auto-approve")

    # Get outputs
    outputs = get_terraform_outputs(ws)
    public_ip = outputs.get("public_ip", "<unknown>")
    instance_id = outputs.get("instance_id", "<unknown>")
    ami_id = outputs.get("ami_id", "<unknown>")
    storage_mount_point = outputs.get("storage_mount_point", "") or ""
    s3_bucket_name = outputs.get("s3_bucket_name", "") or ""
    efs_dns_name = outputs.get("efs_dns_name", "") or ""

    # Save metadata
    save_metadata(ws, {
        "workspace_name": workspace_name,
        "instance_type": selected["instance_type"],
        "gpu_type": selected.get("gpu_type"),
        "region": region,
        "public_ip": public_ip,
        "instance_id": instance_id,
        "ami_id": ami_id,
        "key_pair_name": key_name,
        "storage_type": storage_type,
        "storage_size_gb": storage_size_gb,
        "storage_mount_point": storage_mount_point,
        "s3_bucket_name": s3_bucket_name,
        "efs_dns_name": efs_dns_name,
        "iam_instance_profile_name": iam_instance_profile_name,
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
    })

    # Print results
    pem_path = ws / f"{key_name}.pem"
    console.print("\n" + "=" * 60)
    console.print("[bold green]Instance provisioned successfully![/bold green]\n")
    _print_connection_info({
        "workspace_name": workspace_name,
        "workspace_dir": str(ws),
        "key_pair_name": key_name,
        "public_ip": public_ip,
        "instance_id": instance_id,
        "instance_type": selected["instance_type"],
        "gpu_type": selected.get("gpu_type"),
        "ami_id": ami_id,
        "storage_type": storage_type,
        "storage_size_gb": storage_size_gb,
        "storage_mount_point": storage_mount_point,
        "s3_bucket_name": s3_bucket_name,
        "efs_dns_name": efs_dns_name,
    })
    console.print(f"\n[bold]To destroy this instance:[/bold]")
    console.print(f"  [yellow]uv run {Path(__file__).name} --destroy[/yellow]")
    console.print("=" * 60 + "\n")

    # Offer recipe installation
    gpu_vendor = selected.get("gpu_vendor")
    prompt_and_install_recipes(pem_path, public_ip, gpu_vendor)


def _print_connection_info(meta: dict) -> None:
    """Print instance details + storage info + SSH command + VS Code Remote-SSH setup block."""
    workspace_name = meta.get("workspace_name", "?")
    workspace_dir = Path(meta["workspace_dir"])
    key_pair_name = meta.get("key_pair_name") or workspace_name
    pem_path = workspace_dir / f"{key_pair_name}.pem"
    public_ip = meta.get("public_ip", "?")

    console.print(f"  Instance ID : {meta.get('instance_id', '?')}")
    console.print(f"  Public IP   : {public_ip}")
    console.print(f"  Instance    : {meta.get('instance_type', '?')}")
    console.print(f"  GPU         : {meta.get('gpu_type', '?')}")
    if meta.get("ami_id"):
        console.print(f"  AMI         : {meta['ami_id']}")

    storage_type = meta.get("storage_type") or "none"
    mount_point = meta.get("storage_mount_point") or ""
    if storage_type != "none" and mount_point:
        console.print(f"  Storage     : {storage_type} -> {mount_point} (symlinked at ~/storage)")
        if storage_type == "s3" and meta.get("s3_bucket_name"):
            console.print(f"  S3 bucket   : {meta['s3_bucket_name']}")
        if storage_type == "efs" and meta.get("efs_dns_name"):
            console.print(f"  EFS DNS     : {meta['efs_dns_name']}")
        if storage_type == "ebs" and meta.get("storage_size_gb"):
            console.print(f"  EBS size    : {meta['storage_size_gb']} GB")

    console.print(f"\n[bold]SSH command:[/bold]")
    console.print(f"  [cyan]ssh -i {pem_path} ubuntu@{public_ip}[/cyan]")

    console.print(f"\n[bold]VS Code Remote-SSH setup:[/bold]")
    console.print(f"  Add this block to your [cyan]~/.ssh/config[/cyan]:\n")
    console.print(f"    Host {workspace_name}")
    console.print(f"        HostName {public_ip}")
    console.print(f"        User ubuntu")
    console.print(f"        IdentityFile {pem_path}")
    console.print(f"        StrictHostKeyChecking accept-new")
    console.print(f"\n  Then in VS Code:")
    console.print(f"    1. Install the [cyan]Remote - SSH[/cyan] extension if you don't have it.")
    console.print(f"    2. Open the Command Palette ([cyan]Cmd+Shift+P[/cyan] on Mac, [cyan]Ctrl+Shift+P[/cyan] on Windows/Linux).")
    console.print(f"    3. Run [cyan]Remote-SSH: Open SSH Configuration File...[/cyan] and pick [cyan]~/.ssh/config[/cyan].")
    console.print(f"    4. Paste the block above, save, and close.")
    console.print(f"    5. Run [cyan]Remote-SSH: Connect to Host...[/cyan] and pick [cyan]{workspace_name}[/cyan].")


def connect_flow() -> None:
    """Pick a provisioned instance and print its SSH/VS Code connection info."""
    workspaces = load_all_workspaces(include_incomplete=False)
    if not workspaces:
        console.print("No provisioned instances to connect to.")
        return
    _display_workspace_table(workspaces, title="Provisioned Instances")
    while True:
        try:
            choice = console.input(
                "\n[bold]Enter instance number to connect to (or 'q' to quit): [/bold]"
            )
            if choice.strip().lower() == "q":
                return
            idx = int(choice)
            if 1 <= idx <= len(workspaces):
                console.print("\n" + "=" * 60)
                console.print(
                    f"[bold]Connection info for {workspaces[idx - 1].get('workspace_name')}[/bold]\n"
                )
                _print_connection_info(workspaces[idx - 1])
                console.print("=" * 60 + "\n")
                return
            console.print(f"[red]Enter a number between 1 and {len(workspaces)}[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")


def entry_menu(existing: list[dict]) -> str:
    """Show existing instances and ask what to do.

    Returns one of: 'new', 'connect', 'destroy', 'quit'.
    """
    console.print()
    _display_workspace_table(existing, title="Existing Instances")
    console.print(
        "\n[bold]What would you like to do?[/bold]\n"
        "  [cyan]n[/cyan]) Start a [bold]n[/bold]ew instance\n"
        "  [cyan]c[/cyan]) [bold]C[/bold]onnect to an existing instance (show SSH / VS Code config)\n"
        "  [cyan]d[/cyan]) [bold]D[/bold]estroy an existing instance\n"
        "  [cyan]q[/cyan]) [bold]Q[/bold]uit"
    )
    while True:
        answer = console.input("\nChoice [n/c/d/q]: ").strip().lower()
        if answer in ("n", "new"):
            return "new"
        if answer in ("c", "connect"):
            return "connect"
        if answer in ("d", "destroy"):
            return "destroy"
        if answer in ("q", "quit"):
            return "quit"
        console.print("[red]Please enter n, c, d, or q.[/red]")


def _display_workspace_table(
    workspaces: list[dict], title: str = "Provisioned Instances"
) -> None:
    """Display a table of workspaces (complete and incomplete)."""
    table = Table(title=title)
    table.add_column("#", justify="right", style="cyan")
    table.add_column("Workspace", style="green")
    table.add_column("Status", justify="center")
    table.add_column("Instance Type")
    table.add_column("GPU")
    table.add_column("Public IP", style="magenta")
    table.add_column("Instance ID")
    table.add_column("Provisioned At")

    for idx, meta in enumerate(workspaces, 1):
        status = "[red]INCOMPLETE[/red]" if meta.get("incomplete") else "[green]active[/green]"
        table.add_row(
            str(idx),
            meta.get("workspace_name", "?"),
            status,
            meta.get("instance_type", "?"),
            meta.get("gpu_type", "?") if not meta.get("incomplete") else "-",
            meta.get("public_ip", "?") if not meta.get("incomplete") else "-",
            meta.get("instance_id", "?") if not meta.get("incomplete") else "-",
            meta.get("provisioned_at", "?") if not meta.get("incomplete") else "-",
        )

    console.print(table)


def list_flow() -> None:
    """List all provisioned instances."""
    workspaces = load_all_workspaces(include_incomplete=True)
    if not workspaces:
        console.print("No provisioned instances found.")
        return

    _display_workspace_table(workspaces)


def destroy_flow() -> None:
    """Destroy a provisioned instance: pick from list → terraform destroy → cleanup."""
    # We'll be running boto3 (delete_key_pair) and terraform; verify creds upfront.
    _verify_aws_credentials(_load_user_config().get("region", DEFAULT_REGION))
    workspaces = load_all_workspaces(include_incomplete=True)
    if not workspaces:
        console.print("No provisioned instances to destroy.")
        return

    _display_workspace_table(workspaces, title="Instances Available for Destruction")

    while True:
        try:
            choice = console.input(
                "\n[bold]Enter instance number to destroy (or 'q' to quit): [/bold]"
            )
            if choice.strip().lower() == "q":
                console.print("Cancelled.")
                return
            idx = int(choice)
            if 1 <= idx <= len(workspaces):
                break
            console.print(f"[red]Enter a number between 1 and {len(workspaces)}[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")

    meta = workspaces[idx - 1]
    ws = Path(meta["workspace_dir"])
    workspace_name = meta.get("workspace_name", ws.name)
    is_incomplete = meta.get("incomplete", False)

    status_label = " (INCOMPLETE)" if is_incomplete else ""
    console.print(
        f"\n[bold red]Destroying {meta.get('instance_type', '?')} "
        f"({workspace_name}){status_label}...[/bold red]\n"
    )

    confirm = console.input(
        "[bold]Type 'yes' (or 'y') to confirm destruction: [/bold]"
    ).strip().lower()
    if confirm not in ("y", "yes"):
        if confirm:
            console.print(
                f"  [yellow]Got '{confirm}' — expected 'yes' or 'y'. Cancelled.[/yellow]"
            )
        else:
            console.print("Cancelled.")
        return

    # Run terraform destroy. If state exists but .terraform dir was cleaned,
    # re-init first. Use check=False so a partial-destroy failure still lets
    # us try to delete the key pair below.
    tf_destroy_ok = True
    if (ws / ".terraform").exists():
        result = run_terraform(ws, "destroy", "-auto-approve", check=False)
        tf_destroy_ok = result.returncode == 0
    elif (ws / "terraform.tfstate").exists():
        console.print("  Re-initializing terraform...")
        # check=False so a flaky registry/init failure doesn't bypass the
        # key-pair cleanup that runs after this block.
        init_result = run_terraform(ws, "init", check=False)
        if init_result.returncode != 0:
            console.print(
                "  [yellow]terraform init failed; skipping terraform destroy "
                "but still attempting key-pair cleanup below.[/yellow]"
            )
            tf_destroy_ok = False
        else:
            result = run_terraform(ws, "destroy", "-auto-approve", check=False)
            tf_destroy_ok = result.returncode == 0
    else:
        console.print(
            "  [yellow]No Terraform state found — skipping terraform destroy.[/yellow]"
        )
        console.print(
            "  [yellow]Note: if provision was interrupted partway through `terraform apply`, "
            "some AWS resources may still exist. Check the AWS console for VPCs/instances "
            f"tagged with Name={workspace_name}.[/yellow]"
        )

    # Delete key pair from AWS (best-effort; warns loudly on AccessDenied).
    key_name = meta.get("key_pair_name")
    if key_name:
        console.print(f"  Deleting key pair '{key_name}'...")
        delete_key_pair(key_name, meta.get("region", DEFAULT_REGION))

    if not tf_destroy_ok:
        console.print(
            f"\n[red]terraform destroy did not complete cleanly.[/red] "
            f"Keeping workspace [cyan]{ws.name}[/cyan] on disk so you can inspect "
            f"the state and retry. Run `cd {ws} && terraform destroy` once the "
            f"underlying issue is resolved.\n"
        )
        return

    # Remove workspace directory only on a fully successful destroy
    console.print("  Removing workspace directory...")
    shutil.rmtree(ws, ignore_errors=True)

    console.print("\n[bold green]Instance destroyed and workspace cleaned up.[/bold green]\n")


def install_flow() -> None:
    """Install recipes on an already-provisioned instance."""
    workspaces = load_all_workspaces(include_incomplete=False)
    if not workspaces:
        console.print("No provisioned instances found.")
        return

    _display_workspace_table(workspaces)

    while True:
        try:
            choice = console.input(
                "\n[bold]Enter instance number (or 'q' to quit): [/bold]"
            )
            if choice.strip().lower() == "q":
                console.print("Cancelled.")
                return
            idx = int(choice)
            if 1 <= idx <= len(workspaces):
                break
            console.print(f"[red]Enter a number between 1 and {len(workspaces)}[/red]")
        except ValueError:
            console.print("[red]Invalid input.[/red]")

    meta = workspaces[idx - 1]
    ws = Path(meta["workspace_dir"])
    pem_path = ws / f"{meta['key_pair_name']}.pem"
    public_ip = meta.get("public_ip", "")
    gpu_vendor = meta.get("gpu_type", "").split()[0] if meta.get("gpu_type") else None

    if not public_ip or public_ip == "<unknown>":
        console.print("[red]No public IP found for this instance.[/red]")
        return

    recipes = get_compatible_recipes(gpu_vendor)
    if not recipes:
        console.print("[yellow]No compatible recipes found.[/yellow]")
        return

    display_recipes(recipes)
    selection = console.input(
        "\n[bold]Enter recipe numbers (comma-separated, Enter or 'q' to skip): [/bold]"
    )
    stripped = selection.strip().lower()
    if stripped in ("", "q"):
        return

    indices, rejected = _parse_recipe_selection(selection, len(recipes))
    if rejected:
        console.print(
            f"  [yellow]Ignored: {', '.join(repr(r) for r in rejected)} — "
            "not valid recipe numbers.[/yellow]"
        )

    if not indices:
        console.print("[yellow]No valid recipes selected.[/yellow]")
        return

    # Wait until SSH is up and cloud-init has finished before SCP/SSH.
    if not _wait_for_ssh(pem_path, public_ip):
        console.print("[red]Cannot install recipes — SSH never came up.[/red]")
        return
    _wait_for_cloud_init(pem_path, public_ip)

    for i in indices:
        recipe = recipes[i]
        console.print(f"\n[bold]Installing {recipe.get('name')}...[/bold]")
        ok = install_recipe_on_instance(pem_path, public_ip, recipe)
        if ok:
            console.print(f"[green]{recipe.get('name')} installed successfully.[/green]")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Provision AWS GPU instances via Terraform",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples:\n"
            "  python provision.py --instances-file gpu_instances.json   # provision\n"
            "  python provision.py --list                                # list active\n"
            "  python provision.py --destroy                             # destroy one\n"
            "  python provision.py --install                             # install recipes\n"
        ),
    )
    parser.add_argument(
        "--instances-file",
        default=str(SCRIPT_DIR / "aws_gpu_instances.json"),
        help="Path to the GPU instances JSON file (default: aws_gpu_instances.json)",
    )
    parser.add_argument(
        "--pricing-source",
        choices=PRICING_SOURCES,
        default=DEFAULT_PRICING_SOURCE,
        help=(
            f"Where to fetch on-demand pricing from (default: {DEFAULT_PRICING_SOURCE}). "
            "'vantage' is public and needs no AWS auth (~200 MB download, cached 24h). "
            "'aws-api' uses boto3 + AWS Pricing API (requires pricing:GetProducts IAM permission). "
            "'none' skips pricing entirely."
        ),
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="List all provisioned instances",
    )
    parser.add_argument(
        "--destroy",
        action="store_true",
        help="Destroy a provisioned instance",
    )
    parser.add_argument(
        "--install",
        action="store_true",
        help="Install software recipes on a provisioned instance",
    )
    parser.add_argument(
        "--connect",
        action="store_true",
        help="Show SSH / VS Code connection info for an existing instance",
    )
    parser.add_argument(
        "--iam-instance-profile",
        default=None,
        help=(
            "Name of an existing IAM instance profile to attach to the EC2 instance. "
            "Required for storage_type=s3 so mountpoint-s3 can authenticate. "
            "This app does not create or modify IAM resources — you must create the "
            "profile (and the underlying role and policy) yourself."
        ),
    )
    parser.add_argument(
        "--region",
        default=None,
        help=(
            "AWS region to provision in. If omitted, you are prompted at startup "
            "(defaulting to your last saved choice, or us-east-1 on first run). "
            "The selected region is persisted to ~/.config/aws-terraform-provisioner/config.json."
        ),
    )
    parser.add_argument(
        "--ami",
        choices=tuple(AMI_PATTERNS.keys()),
        default=DEFAULT_AMI,
        help=(
            f"Which AWS Deep Learning AMI to use (default: {DEFAULT_AMI}). "
            "'pytorch' = full DLAMI with PyTorch + CUDA + cuDNN + NCCL + drivers. "
            "'tensorflow' = full DLAMI with TensorFlow instead of PyTorch. "
            "'base' = minimal DLAMI with only NVIDIA OSS drivers + CUDA + cuDNN "
            "(no frameworks pre-installed)."
        ),
    )

    args = parser.parse_args()

    if args.list:
        list_flow()
    elif args.destroy:
        destroy_flow()
    elif args.install:
        install_flow()
    elif args.connect:
        connect_flow()
    else:
        # Default: if there are existing instances, show the entry menu first.
        existing = load_all_workspaces(include_incomplete=True)
        if existing:
            action = entry_menu(existing)
            if action == "quit":
                return
            if action == "connect":
                connect_flow()
                return
            if action == "destroy":
                destroy_flow()
                return
            # action == "new" → fall through to provisioning
        if not Path(args.instances_file).exists():
            console.print(
                f"[red]Instances file not found: {args.instances_file}\n"
                f"Use --instances-file to specify the path.[/red]"
            )
            sys.exit(1)
        region = _resolve_region(args.region)
        provision_flow(
            args.instances_file,
            args.pricing_source,
            region,
            cli_iam_profile=args.iam_instance_profile,
            ami_choice=args.ami,
        )


def _graceful_exit() -> None:
    """Print a friendly Ctrl+C message and flag any half-finished workspaces."""
    console.print("\n[yellow]Cancelled by user (Ctrl+C).[/yellow]")
    if WORKSPACES_DIR.exists():
        incomplete = [
            d
            for d in sorted(WORKSPACES_DIR.iterdir())
            if d.is_dir() and not (d / METADATA_FILE).exists()
        ]
        if incomplete:
            console.print(
                f"[yellow]{len(incomplete)} workspace(s) without metadata may have "
                f"partial AWS resources:[/yellow]"
            )
            for d in incomplete:
                console.print(f"  - {d.name}")
            console.print(
                "[yellow]Run [bold]uv run provision.py --destroy[/bold] to clean them up.[/yellow]"
            )
    sys.exit(130)  # standard exit code for SIGINT


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        _graceful_exit()
    except EOFError:
        # Stdin closed (e.g. piped input ended) — treat as a graceful quit.
        console.print("\n[yellow]Input closed; exiting.[/yellow]")
        sys.exit(0)
