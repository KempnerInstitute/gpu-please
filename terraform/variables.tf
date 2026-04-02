variable "region" {
  description = "AWS region"
  type        = string
  default     = "us-east-1"
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
