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
