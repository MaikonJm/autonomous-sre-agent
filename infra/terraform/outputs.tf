output "instance_id" {
  value = aws_instance.node.id
}

output "region" {
  value = var.region
}

output "connect" {
  description = "Abre um terminal no nó (precisa do Session Manager plugin)"
  value       = "aws ssm start-session --region ${var.region} --target ${aws_instance.node.id}"
}

output "deploy_log" {
  description = "Acompanhe o deploy automático do primeiro boot (dentro do nó)"
  value       = "sudo tail -f /var/log/sre-lab-deploy.log"
}
