#!/usr/bin/env bash
set -euo pipefail

echo "=== Installing NVIDIA DCGM ==="

# Detect CUDA major version from nvidia-smi
CUDA_VERSION=$(nvidia-smi | grep -oP 'CUDA Version: \K[0-9]+' || true)
if [[ -z "$CUDA_VERSION" ]]; then
    echo "ERROR: Could not detect CUDA version. Is nvidia-smi available?"
    exit 1
fi
echo "Detected CUDA major version: ${CUDA_VERSION}"

# Remove any previously installed DCGM packages
echo "Removing old DCGM packages (if any)..."
sudo apt-get remove -y datacenter-gpu-manager 2>/dev/null || true
sudo apt-get purge -y datacenter-gpu-manager 2>/dev/null || true

# Install DCGM 4 for the detected CUDA version
PACKAGE="datacenter-gpu-manager-4-cuda${CUDA_VERSION}"
echo "Installing ${PACKAGE}..."
sudo apt-get update -y
sudo apt-get install -y "${PACKAGE}"

# Enable and start the DCGM service
echo "Enabling nv-hostengine service..."
sudo systemctl --now enable nvidia-dcgm

# Verify installation
echo ""
echo "=== Verifying DCGM installation ==="
dcgmi discovery -l

echo ""
echo "=== DCGM installation complete ==="
