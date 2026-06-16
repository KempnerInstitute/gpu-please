#!/bin/bash
# Bootstrap script rendered by Terraform via templatefile().
# Bash variables are escaped with a double dollar so they pass through.

set -euo pipefail
exec > >(tee /var/log/provisioner-user-data.log) 2>&1
echo "==== provisioner user_data starting at $(date) ===="

# Always: install uv system-wide so any user has it on PATH
curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

STORAGE_TYPE="${storage_type}"
USER_NAME="ubuntu"
USER_HOME="/home/$${USER_NAME}"
MOUNT_POINT=""

case "$${STORAGE_TYPE}" in
  s3)
    MOUNT_POINT="/mnt/s3"
    BUCKET="${bucket_name}"
    ARCH=$(dpkg --print-architecture)
    echo "Installing mountpoint-s3 (arch: $${ARCH})..."
    cd /tmp
    if [ "$${ARCH}" = "amd64" ]; then
      MS3_URL="https://s3.amazonaws.com/mountpoint-s3-release/latest/x86_64/mount-s3.deb"
    else
      MS3_URL="https://s3.amazonaws.com/mountpoint-s3-release/latest/arm64/mount-s3.deb"
    fi
    curl -LsSf -o mount-s3.deb "$${MS3_URL}"
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y ./mount-s3.deb
    mkdir -p "$${MOUNT_POINT}"
    UID_NUM=$(id -u "$${USER_NAME}")
    GID_NUM=$(id -g "$${USER_NAME}")
    echo "Mounting s3://$${BUCKET} at $${MOUNT_POINT}..."
    /usr/bin/mount-s3 --allow-other --uid "$${UID_NUM}" --gid "$${GID_NUM}" "$${BUCKET}" "$${MOUNT_POINT}"
    # systemd unit so the mount persists across reboots
    cat > /etc/systemd/system/mountpoint-s3.service <<UNIT
[Unit]
Description=Mount S3 bucket via mountpoint-s3
After=network-online.target
Wants=network-online.target

[Service]
Type=forking
ExecStart=/usr/bin/mount-s3 --allow-other --uid $${UID_NUM} --gid $${GID_NUM} $${BUCKET} $${MOUNT_POINT}
ExecStop=/bin/fusermount -u $${MOUNT_POINT}
Restart=on-failure

[Install]
WantedBy=multi-user.target
UNIT
    systemctl daemon-reload
    systemctl enable mountpoint-s3.service
    ;;

  ebs)
    MOUNT_POINT="/mnt/ebs"
    VOLUME_ID="${ebs_volume_id}"
    # AWS Nitro encodes the EBS volume ID (without the dash) in the NVMe serial,
    # so udev creates a deterministic by-id symlink. This is reliable across
    # AMIs regardless of NVMe enumeration order (DLAMI puts root on nvme1n1).
    DEVICE="/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_$${VOLUME_ID//-/}"
    echo "Waiting for EBS volume $${VOLUME_ID} at $${DEVICE}..."
    for i in $(seq 1 60); do
      if [ -e "$${DEVICE}" ]; then break; fi
      sleep 2
    done
    if [ ! -e "$${DEVICE}" ]; then
      echo "ERROR: EBS volume $${VOLUME_ID} did not appear at $${DEVICE} within 120s"
      exit 1
    fi
    if ! blkid "$${DEVICE}" >/dev/null 2>&1; then
      echo "Formatting $${DEVICE} as ext4..."
      mkfs.ext4 -F "$${DEVICE}"
    fi
    mkdir -p "$${MOUNT_POINT}"
    mount "$${DEVICE}" "$${MOUNT_POINT}"
    UUID=$(blkid -s UUID -o value "$${DEVICE}")
    echo "UUID=$${UUID} $${MOUNT_POINT} ext4 defaults,nofail 0 2" >> /etc/fstab
    chown "$${USER_NAME}:$${USER_NAME}" "$${MOUNT_POINT}"
    ;;

  efs)
    MOUNT_POINT="/mnt/efs"
    EFS_DNS="${efs_dns}"
    echo "Installing NFS client..."
    apt-get update
    DEBIAN_FRONTEND=noninteractive apt-get install -y nfs-common
    mkdir -p "$${MOUNT_POINT}"
    echo "Mounting EFS $${EFS_DNS} at $${MOUNT_POINT}..."
    # EFS mount target can take a moment to become reachable
    for i in $(seq 1 30); do
      if mount -t nfs4 -o nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport "$${EFS_DNS}:/" "$${MOUNT_POINT}"; then
        break
      fi
      echo "  mount attempt $${i} failed, retrying..."
      sleep 5
    done
    echo "$${EFS_DNS}:/ $${MOUNT_POINT} nfs4 nfsvers=4.1,rsize=1048576,wsize=1048576,hard,timeo=600,retrans=2,noresvport,_netdev 0 0" >> /etc/fstab
    chown "$${USER_NAME}:$${USER_NAME}" "$${MOUNT_POINT}"
    ;;

  none)
    echo "No additional storage requested."
    ;;

  *)
    echo "Unknown storage_type: $${STORAGE_TYPE}"
    ;;
esac

# Convenience symlink in the user's home directory
if [ -n "$${MOUNT_POINT}" ]; then
  ln -sfn "$${MOUNT_POINT}" "$${USER_HOME}/storage"
  chown -h "$${USER_NAME}:$${USER_NAME}" "$${USER_HOME}/storage"
  echo "Symlink: $${USER_HOME}/storage -> $${MOUNT_POINT}"
fi

echo "==== provisioner user_data complete at $(date) ===="
