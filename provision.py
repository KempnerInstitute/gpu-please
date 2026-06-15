#!/usr/bin/env python3
"""AWS GPU Instance Provisioner — CLI tool to provision GPU EC2 instances via Terraform."""

import argparse
import json
import os
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests
import yaml
from rich.console import Console
from rich.table import Table

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
DEFAULT_REGION = "us-east-1"
SCRIPT_DIR = Path(__file__).resolve().parent
TERRAFORM_TEMPLATE_DIR = SCRIPT_DIR / "terraform"
WORKSPACES_DIR = SCRIPT_DIR / "workspaces"
RECIPES_DIR = SCRIPT_DIR / "recipes"
PRICING_CACHE_DIR = SCRIPT_DIR / ".pricing_cache"
METADATA_FILE = "metadata.json"

console = Console()

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


def check_availability(instances: list[dict], region: str) -> list[dict]:
    """Keep only instance types available in the given region via AWS API."""
    ec2 = boto3.client("ec2", region_name=region)
    type_names = list({i["instance_type"] for i in instances})

    available_types: set[str] = set()
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


def _find_valid_cache(region: str) -> Path | None:
    """Return the path to a pricing cache file created within the last 24 hours, or None."""
    if not PRICING_CACHE_DIR.exists():
        return None
    now = datetime.now(timezone.utc)
    prefix = f"pricing_{region}_"
    for f in sorted(PRICING_CACHE_DIR.iterdir(), reverse=True):
        if not f.name.startswith(prefix) or not f.name.endswith(".json"):
            continue
        # Extract timestamp from filename: pricing_<region>_<YYYYMMDD-HHMMSS>.json
        ts_part = f.name[len(prefix):-len(".json")]
        try:
            file_time = datetime.strptime(ts_part, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)
        except ValueError:
            continue
        if (now - file_time).total_seconds() < 86400:
            return f
    return None


def _save_pricing_cache(region: str, prices: dict[str, float | None]) -> None:
    """Write pricing data to a timestamped cache file."""
    PRICING_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    cache_path = PRICING_CACHE_DIR / f"pricing_{region}_{ts}.json"
    with open(cache_path, "w") as f:
        json.dump(prices, f, indent=2)


def _load_pricing_cache(cache_path: Path) -> dict[str, float | None]:
    """Load pricing data from a cache file."""
    with open(cache_path) as f:
        raw = json.load(f)
    return {k: (float(v) if v is not None else None) for k, v in raw.items()}


def fetch_pricing(instance_types: list[str], region: str) -> dict[str, float | None]:
    """Fetch on-demand hourly pricing for a list of instance types.

    Returns a dict mapping instance_type -> price_per_hour (USD), or None if
    the price could not be determined.
    """
    # The Pricing API is only available in us-east-1 and ap-south-1
    pricing = boto3.client("pricing", region_name="us-east-1")
    location = REGION_NAME_MAP.get(region, "US East (N. Virginia)")

    prices: dict[str, float | None] = {}
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
        except Exception:
            prices[itype] = None
    return prices


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

    for idx, inst in enumerate(instances, 1):
        gen_label = "prev" if inst.get("generation_status") == "previous" else "curr"
        price = prices.get(inst["instance_type"])
        price_str = f"{price:.2f}" if price is not None else "n/a"
        table.add_row(
            str(idx),
            inst["instance_type"],
            gen_label,
            inst.get("gpu_type", "?"),
            str(inst.get("gpu_count", "?")),
            str(inst.get("gpu_memory_total_gib", "?")),
            str(inst.get("vcpus", "?")),
            str(inst.get("system_memory_gib", "?")),
            price_str,
        )

    console.print(table)


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
    # Copy terraform templates into workspace
    for tf_file in TERRAFORM_TEMPLATE_DIR.glob("*.tf"):
        shutil.copy2(tf_file, ws / tf_file.name)
    return ws


def create_key_pair(workspace_name: str, workspace_dir: Path, region: str) -> str:
    """Create an AWS key pair and save the .pem file. Returns the key pair name."""
    ec2 = boto3.client("ec2", region_name=region)
    key_name = workspace_name
    response = ec2.create_key_pair(KeyName=key_name, KeyType="rsa", KeyFormat="pem")
    pem_path = workspace_dir / f"{key_name}.pem"
    pem_path.write_text(response["KeyMaterial"])
    pem_path.chmod(stat.S_IRUSR)  # chmod 400
    return key_name


def delete_key_pair(key_name: str, region: str) -> None:
    """Delete an AWS key pair."""
    ec2 = boto3.client("ec2", region_name=region)
    try:
        ec2.delete_key_pair(KeyName=key_name)
    except Exception as e:
        console.print(f"[yellow]Warning: could not delete key pair '{key_name}': {e}[/yellow]")


def get_my_public_ip() -> str:
    """Fetch the caller's public IP address."""
    resp = requests.get("https://checkip.amazonaws.com", timeout=10)
    resp.raise_for_status()
    return resp.text.strip()


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


def run_terraform(workspace_dir: Path, *args: str) -> subprocess.CompletedProcess:
    """Run a terraform command in the given workspace directory."""
    tf = _check_terraform()
    cmd = [tf] + list(args)
    console.print(f"[dim]Running: {' '.join(cmd)} (in {workspace_dir})[/dim]")
    result = subprocess.run(cmd, cwd=workspace_dir)
    if result.returncode != 0:
        console.print(f"[red]Terraform command failed (exit {result.returncode})[/red]")
        sys.exit(result.returncode)
    return result


def get_terraform_outputs(workspace_dir: Path) -> dict:
    """Parse terraform output as JSON."""
    tf = _check_terraform()
    result = subprocess.run(
        [tf, "output", "-json"],
        cwd=workspace_dir,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print(f"[red]Failed to read terraform outputs: {result.stderr}[/red]")
        return {}
    raw = json.loads(result.stdout)
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
    state but no metadata.json (e.g. cancelled mid-provision).
    """
    results = []
    if not WORKSPACES_DIR.exists():
        return results
    for ws in sorted(WORKSPACES_DIR.iterdir()):
        if not ws.is_dir():
            continue
        meta_path = ws / METADATA_FILE
        if meta_path.exists():
            with open(meta_path) as f:
                meta = json.load(f)
            meta["workspace_dir"] = str(ws)
            meta["incomplete"] = False
            results.append(meta)
        elif include_incomplete and (ws / "terraform.tfvars.json").exists():
            # Incomplete workspace — read what we can from tfvars
            tfvars_path = ws / "terraform.tfvars.json"
            with open(tfvars_path) as f:
                tfvars = json.load(f)
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
    """Scan recipes/ for subdirectories containing recipe.yaml."""
    recipes = []
    if not RECIPES_DIR.exists():
        return recipes
    for d in sorted(RECIPES_DIR.iterdir()):
        if not d.is_dir():
            continue
        recipe_file = d / "recipe.yaml"
        if recipe_file.exists():
            with open(recipe_file) as f:
                recipe = yaml.safe_load(f)
            recipe["_dir"] = d
            recipe["_install_script"] = d / "install.sh"
            recipes.append(recipe)
    return recipes


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


def install_recipe_on_instance(
    pem_path: Path, public_ip: str, recipe: dict
) -> bool:
    """SCP the install script to the instance and run it via SSH."""
    script = recipe["_install_script"]
    if not script.exists():
        console.print(f"[red]Install script not found: {script}[/red]")
        return False

    remote_script = f"/tmp/{script.name}"
    ssh_opts = [
        "-i", str(pem_path),
        "-o", "StrictHostKeyChecking=no",
        "-o", "UserKnownHostsFile=/dev/null",
        "-o", "LogLevel=ERROR",
    ]

    # SCP the script
    scp_cmd = ["scp"] + ssh_opts + [str(script), f"ubuntu@{public_ip}:{remote_script}"]
    result = subprocess.run(scp_cmd)
    if result.returncode != 0:
        console.print(f"[red]Failed to copy install script to instance.[/red]")
        return False

    # SSH and run
    ssh_cmd = (
        ["ssh"] + ssh_opts
        + [f"ubuntu@{public_ip}", f"chmod +x {remote_script} && sudo bash {remote_script}"]
    )
    result = subprocess.run(ssh_cmd)
    if result.returncode != 0:
        console.print(f"[red]Recipe '{recipe.get('name')}' failed (exit {result.returncode}).[/red]")
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
        "\n[bold]Enter recipe numbers (comma-separated, or 'q' to skip): [/bold]"
    )
    if selection.strip().lower() == "q":
        return

    indices = []
    for part in selection.split(","):
        part = part.strip()
        if part.isdigit():
            idx = int(part)
            if 1 <= idx <= len(recipes):
                indices.append(idx - 1)

    if not indices:
        console.print("[yellow]No valid recipes selected.[/yellow]")
        return

    for i in indices:
        recipe = recipes[i]
        console.print(f"\n[bold]Installing {recipe.get('name')}...[/bold]")
        ok = install_recipe_on_instance(pem_path, public_ip, recipe)
        if ok:
            console.print(f"[green]{recipe.get('name')} installed successfully.[/green]")


# ---------------------------------------------------------------------------
# Flows
# ---------------------------------------------------------------------------


def provision_flow(json_path: str) -> None:
    """Main provisioning flow: select instance → terraform apply → print SSH command."""
    console.print("[bold]Loading GPU instance catalog...[/bold]")
    instances = load_instances(json_path)
    instances = filter_instances(instances)
    console.print(f"  {len(instances)} non-fractional instances loaded")

    console.print(f"[bold]Checking availability in {DEFAULT_REGION}...[/bold]")
    instances = check_availability(instances, DEFAULT_REGION)
    if not instances:
        console.print("[red]No GPU instances available in this region.[/red]")
        sys.exit(1)
    console.print(f"  {len(instances)} instances available\n")

    console.print("[bold]Fetching on-demand pricing...[/bold]")
    type_names = [i["instance_type"] for i in instances]
    cache_path = _find_valid_cache(DEFAULT_REGION)
    if cache_path:
        console.print(f"  Using cached pricing from {cache_path.name}")
        prices = _load_pricing_cache(cache_path)
        # Fetch any instance types not in the cache
        missing = [t for t in type_names if t not in prices]
        if missing:
            console.print(f"  Fetching {len(missing)} uncached prices...")
            fresh = fetch_pricing(missing, DEFAULT_REGION)
            prices.update(fresh)
            _save_pricing_cache(DEFAULT_REGION, prices)
    else:
        prices = fetch_pricing(type_names, DEFAULT_REGION)
        _save_pricing_cache(DEFAULT_REGION, prices)
    priced = sum(1 for t in type_names if prices.get(t) is not None)
    console.print(f"  {priced}/{len(type_names)} prices found\n")

    instances.sort(key=_gpu_sort_key)
    display_table(instances, prices)
    selected = get_user_selection(instances)

    console.print(f"\n[bold]Provisioning {selected['instance_type']}...[/bold]\n")

    # Create workspace
    ws = create_workspace(selected["instance_type"])
    workspace_name = ws.name
    console.print(f"  Workspace: [cyan]{workspace_name}[/cyan]")

    # Create key pair
    console.print("  Creating SSH key pair...")
    key_name = create_key_pair(workspace_name, ws, DEFAULT_REGION)

    # Get caller IP
    console.print("  Detecting your public IP...")
    my_ip = get_my_public_ip()
    console.print(f"  Your IP: {my_ip}")

    # Determine AMI architecture
    ami_arch = _ami_arch_for_instance(selected)

    # Write tfvars
    write_tfvars(ws, {
        "region": DEFAULT_REGION,
        "instance_type": selected["instance_type"],
        "key_pair_name": key_name,
        "workspace_name": workspace_name,
        "allowed_ssh_cidr": f"{my_ip}/32",
        "ami_architecture": ami_arch,
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

    # Save metadata
    save_metadata(ws, {
        "workspace_name": workspace_name,
        "instance_type": selected["instance_type"],
        "gpu_type": selected.get("gpu_type"),
        "region": DEFAULT_REGION,
        "public_ip": public_ip,
        "instance_id": instance_id,
        "ami_id": ami_id,
        "key_pair_name": key_name,
        "provisioned_at": datetime.now(timezone.utc).isoformat(),
    })

    # Print results
    pem_path = ws / f"{key_name}.pem"
    console.print("\n" + "=" * 60)
    console.print("[bold green]Instance provisioned successfully![/bold green]\n")
    console.print(f"  Instance ID : {instance_id}")
    console.print(f"  Public IP   : {public_ip}")
    console.print(f"  Instance    : {selected['instance_type']}")
    console.print(f"  GPU         : {selected.get('gpu_type', '?')}")
    console.print(f"  AMI         : {ami_id}")
    console.print(f"\n[bold]SSH command:[/bold]")
    console.print(f"  [cyan]ssh -i {pem_path} ubuntu@{public_ip}[/cyan]")
    console.print(f"\n[bold]To destroy this instance:[/bold]")
    console.print(f"  [yellow]python {Path(__file__).name} --destroy[/yellow]")
    console.print("=" * 60 + "\n")

    # Offer recipe installation
    gpu_vendor = selected.get("gpu_vendor")
    prompt_and_install_recipes(pem_path, public_ip, gpu_vendor)


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

    confirm = console.input("[bold]Type 'yes' to confirm destruction: [/bold]")
    if confirm.strip().lower() != "yes":
        console.print("Cancelled.")
        return

    # Ensure terraform is initialized (may not be if provision was interrupted early)
    if (ws / ".terraform").exists():
        run_terraform(ws, "destroy", "-auto-approve")
    elif (ws / "terraform.tfstate").exists():
        # State exists but .terraform dir was cleaned — re-init first
        console.print("  Re-initializing terraform...")
        run_terraform(ws, "init")
        run_terraform(ws, "destroy", "-auto-approve")
    else:
        console.print("  No Terraform state found — skipping terraform destroy.")

    # Delete key pair from AWS
    key_name = meta.get("key_pair_name")
    if key_name:
        console.print(f"  Deleting key pair '{key_name}'...")
        delete_key_pair(key_name, meta.get("region", DEFAULT_REGION))

    # Remove workspace directory
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
        "\n[bold]Enter recipe numbers (comma-separated, or 'q' to skip): [/bold]"
    )
    if selection.strip().lower() == "q":
        return

    indices = []
    for part in selection.split(","):
        part = part.strip()
        if part.isdigit():
            i = int(part)
            if 1 <= i <= len(recipes):
                indices.append(i - 1)

    if not indices:
        console.print("[yellow]No valid recipes selected.[/yellow]")
        return

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
        default=str(SCRIPT_DIR / "aws_gpu_instances_2026-04-01.json"),
        help="Path to the GPU instances JSON file (default: aws_gpu_instances_2026-04-01.json)",
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

    args = parser.parse_args()

    if args.list:
        list_flow()
    elif args.destroy:
        destroy_flow()
    elif args.install:
        install_flow()
    else:
        if not Path(args.instances_file).exists():
            console.print(
                f"[red]Instances file not found: {args.instances_file}\n"
                f"Use --instances-file to specify the path.[/red]"
            )
            sys.exit(1)
        provision_flow(args.instances_file)


if __name__ == "__main__":
    main()
