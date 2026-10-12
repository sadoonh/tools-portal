output "marimohub_url" {
  value = local.hub_count == 1 ? local.hub_url : null
}
output "marimohub_distribution_id" {
  value = local.hub_count == 1 ? aws_cloudfront_distribution.hub[0].id : null
}
output "marimohub_service_name" {
  value = local.hub_run_count == 1 ? aws_ecs_service.hub[0].name : null
}
output "marimohub_kernel_task_definition_arn" {
  value = local.hub_run_count == 1 ? aws_ecs_task_definition.kernel[0].arn : null
}
output "marimohub_bucket_name" {
  value = local.hub_count == 1 ? aws_s3_bucket.hub[0].id : null
}
output "marimohub_ecr_repositories" {
  value = local.hub_count == 1 ? { for name, repository in aws_ecr_repository.hub_images : name => repository.repository_url } : null
}
output "marimohub_log_groups" {
  value = local.hub_count == 1 ? { for name, group in aws_cloudwatch_log_group.hub_logs : name => group.name } : null
}
output "marimohub_cognito_client_id" {
  value = local.hub_count == 1 ? aws_cognito_user_pool_client.hub[0].id : null
}
output "marimohub_secret_arn" {
  description = "Identifier only, never secret contents."
  value       = local.hub_count == 1 ? aws_secretsmanager_secret.hub[0].arn : null
}
