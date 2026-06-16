output "public_ip" {
  description = "Public IP address of the GPU instance"
  value       = aws_instance.gpu.public_ip
}

output "instance_id" {
  description = "EC2 instance ID"
  value       = aws_instance.gpu.id
}

output "ami_id" {
  description = "AMI used for the instance"
  value       = aws_instance.gpu.ami
}

output "storage_type" {
  description = "Type of additional storage attached (s3, ebs, efs, or none)"
  value       = var.storage_type
}

output "storage_mount_point" {
  description = "Filesystem path where the additional storage is mounted on the instance"
  value = (
    var.storage_type == "s3" ? "/mnt/s3" :
    var.storage_type == "ebs" ? "/mnt/ebs" :
    var.storage_type == "efs" ? "/mnt/efs" :
    ""
  )
}

output "s3_bucket_name" {
  description = "Name of the S3 bucket created for storage (empty unless storage_type=s3)"
  value       = local.is_s3 ? aws_s3_bucket.storage[0].id : ""
}

output "efs_dns_name" {
  description = "DNS name of the EFS filesystem (empty unless storage_type=efs)"
  value       = local.is_efs ? aws_efs_file_system.storage[0].dns_name : ""
}
