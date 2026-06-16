variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
}

variable "availability_zone" {
  description = "AZ within var.region where the subnet and instance are created. Must support the chosen instance type. provision.py picks one via ec2:DescribeInstanceTypeOfferings."
  type        = string
}

variable "instance_type" {
  description = "EC2 instance type (e.g. g5.xlarge)"
  type        = string
}

variable "key_pair_name" {
  description = "Name of the AWS key pair"
  type        = string
}

variable "workspace_name" {
  description = "Unique name for this provisioned workspace"
  type        = string
}

variable "allowed_ssh_cidr" {
  description = "CIDR block allowed to SSH (e.g. 1.2.3.4/32)"
  type        = string
}

variable "root_volume_size_gb" {
  description = "Size of root EBS volume in GiB"
  type        = number
  default     = 100
}

variable "ami_architecture" {
  description = "AMI architecture filter (x86_64 or arm64)"
  type        = string
  default     = "x86_64"
}

variable "ami_name_pattern" {
  description = "AMI Name filter pattern (used by the data.aws_ami.dlami lookup). provision.py sets this from the --ami CLI flag — pytorch (default), tensorflow, or base."
  type        = string
  # Default pattern matches the full PyTorch DLAMI: NVIDIA drivers + CUDA +
  # cuDNN + NCCL + PyTorch + Python pre-installed.
  default = "Deep Learning OSS Nvidia Driver AMI GPU PyTorch * (Ubuntu 22.04)*"
}

variable "storage_type" {
  description = "Additional storage type to attach: s3, ebs, efs, or none. Resources are created conditionally in main.tf."
  type        = string
  default     = "none"
  validation {
    condition     = contains(["s3", "ebs", "efs", "none"], var.storage_type)
    error_message = "storage_type must be one of: s3, ebs, efs, none."
  }
}

variable "storage_size_gb" {
  description = "Size of additional storage in GB. Used for EBS data volumes; informational for s3/efs (pay-as-you-go)."
  type        = number
  default     = 100
}

variable "iam_instance_profile_name" {
  description = "Name of an existing IAM instance profile to attach to the EC2 instance. Required for storage_type=s3 (so mountpoint-s3 can authenticate). Leave empty for no profile."
  type        = string
  default     = ""
}
