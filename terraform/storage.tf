# Conditional storage resources gated on var.storage_type.
# Created only when the matching storage type is selected.

locals {
  is_s3  = var.storage_type == "s3"
  is_ebs = var.storage_type == "ebs"
  is_efs = var.storage_type == "efs"
}

# ---------- S3 bucket for mountpoint-s3 ----------
#
# Intentionally NO aws_iam_role / aws_iam_instance_profile here — this app
# does not create or modify IAM. The user supplies an existing instance
# profile (via var.iam_instance_profile_name) that already has the required
# S3 permissions on the buckets they want to mount.

resource "aws_s3_bucket" "storage" {
  count = local.is_s3 ? 1 : 0
  # workspace_name is already unique (instance-type + second-precision timestamp);
  # combined with "-storage" it stays under the 63-char S3 bucket name limit.
  bucket        = "${var.workspace_name}-storage"
  force_destroy = true

  tags = {
    Name = "${var.workspace_name}-storage"
  }
}

# ---------- Extra EBS data volume ----------

resource "aws_ebs_volume" "storage" {
  count = local.is_ebs ? 1 : 0
  # Use the same AZ as the subnet so the volume can attach to the instance.
  # Reading from the AZ data source (not aws_instance.gpu) avoids a dependency
  # cycle: aws_instance.gpu's user_data references this volume's id.
  availability_zone = data.aws_availability_zones.available.names[0]
  size              = var.storage_size_gb
  type              = "gp3"

  tags = {
    Name = "${var.workspace_name}-data"
  }
}

resource "aws_volume_attachment" "storage" {
  count       = local.is_ebs ? 1 : 0
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.storage[0].id
  instance_id = aws_instance.gpu.id
}

# ---------- EFS filesystem + mount target ----------

resource "aws_security_group" "efs" {
  count       = local.is_efs ? 1 : 0
  name        = "${var.workspace_name}-efs"
  description = "Allow NFS from the GPU instance's security group"
  vpc_id      = aws_vpc.gpu.id

  ingress {
    description     = "NFS"
    from_port       = 2049
    to_port         = 2049
    protocol        = "tcp"
    security_groups = [aws_security_group.gpu_ssh.id]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.workspace_name}-efs"
  }
}

resource "aws_efs_file_system" "storage" {
  count            = local.is_efs ? 1 : 0
  creation_token   = "${var.workspace_name}-efs"
  performance_mode = "generalPurpose"
  throughput_mode  = "bursting"

  tags = {
    Name = "${var.workspace_name}-storage"
  }
}

resource "aws_efs_mount_target" "storage" {
  count           = local.is_efs ? 1 : 0
  file_system_id  = aws_efs_file_system.storage[0].id
  subnet_id       = aws_subnet.public.id
  security_groups = [aws_security_group.efs[0].id]
}
