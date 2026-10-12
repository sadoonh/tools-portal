output "aws_region" {
  value = var.aws_region
}
output "aws_account_id" {
  value = var.aws_account_id
}
output "project_name" {
  value = var.project_name
}
output "cloudfront_url" {
  value = local.cloudfront_url
}
output "cloudfront_distribution_id" {
  value = aws_cloudfront_distribution.app.id
}
output "cloudfront_vpc_origin_id" {
  value = aws_cloudfront_vpc_origin.app.id
}
output "ecr_repository_url" {
  value = aws_ecr_repository.app.repository_url
}
output "ecr_repository_name" {
  value = aws_ecr_repository.app.name
}
output "ecs_cluster_name" {
  value = aws_ecs_cluster.app.name
}
output "ecs_service_name" {
  value = var.enable_service ? aws_ecs_service.app[0].name : null
}
output "log_group_name" {
  value = aws_cloudwatch_log_group.app.name
}
output "alb_arn" {
  value = aws_lb.app.arn
}
output "target_group_arn" {
  value = aws_lb_target_group.app.arn
}
output "vpc_id" {
  value = aws_vpc.app.id
}
output "cognito_user_pool_id" {
  value = var.auth_enabled ? aws_cognito_user_pool.app[0].id : null
}
output "cognito_client_id" {
  value = var.auth_enabled ? aws_cognito_user_pool_client.app[0].id : null
}
output "cognito_domain_url" {
  value = var.auth_enabled ? local.cognito_domain : null
}
