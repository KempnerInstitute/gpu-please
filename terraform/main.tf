terraform {
  required_version = ">= 1.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = var.region
}

# ---------- networking ----------

data "aws_availability_zones" "available" {
  state = "available"
}

resource "aws_vpc" "gpu" {
  cidr_block           = "10.0.0.0/16"
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = {
    Name = "${var.workspace_name}-vpc"
  }
}

resource "aws_internet_gateway" "gpu" {
  vpc_id = aws_vpc.gpu.id

  tags = {
    Name = "${var.workspace_name}-igw"
  }
}

resource "aws_subnet" "public" {
  vpc_id                  = aws_vpc.gpu.id
  cidr_block              = "10.0.1.0/24"
  availability_zone       = data.aws_availability_zones.available.names[0]
  map_public_ip_on_launch = true

  tags = {
    Name = "${var.workspace_name}-public"
  }
}

resource "aws_route_table" "public" {
  vpc_id = aws_vpc.gpu.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.gpu.id
  }

  tags = {
    Name = "${var.workspace_name}-rt"
  }
}

resource "aws_route_table_association" "public" {
  subnet_id      = aws_subnet.public.id
  route_table_id = aws_route_table.public.id
}

resource "aws_security_group" "gpu_ssh" {
  name        = "${var.workspace_name}-ssh"
  description = "Allow SSH from provisioner IP"
  vpc_id      = aws_vpc.gpu.id

  ingress {
    description = "SSH"
    from_port   = 22
    to_port     = 22
    protocol    = "tcp"
    cidr_blocks = [var.allowed_ssh_cidr]
  }

  egress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }

  tags = {
    Name = "${var.workspace_name}-ssh"
  }
}

# ---------- AMI ----------

data "aws_ami" "dlami" {
  most_recent = true
  owners      = ["amazon"]

  filter {
    name   = "name"
    values = ["Deep Learning Base OSS Nvidia Driver GPU AMI (Ubuntu 22.04)*"]
  }

  filter {
    name   = "state"
    values = ["available"]
  }

  filter {
    name   = "architecture"
    values = [var.ami_architecture]
  }
}

# ---------- instance ----------

resource "aws_instance" "gpu" {
  ami                         = data.aws_ami.dlami.id
  instance_type               = var.instance_type
  key_name                    = var.key_pair_name
  subnet_id                   = aws_subnet.public.id
  vpc_security_group_ids      = [aws_security_group.gpu_ssh.id]
  associate_public_ip_address = true

  root_block_device {
    volume_size = var.root_volume_size_gb
    volume_type = "gp3"
  }

  tags = {
    Name = var.workspace_name
  }
}
