# Registry/logs and least-privilege ECS execution role. No application task role.
resource "aws_ecr_repository" "app" {
  name                 = var.project_name
  image_tag_mutability = "IMMUTABLE"
  force_delete         = !var.protect_resources # Company workspaces retain nonempty repositories.
  image_scanning_configuration {
    scan_on_push = true
  }
}
resource "aws_cloudwatch_log_group" "app" {
  name              = "/ecs/${var.project_name}"
  retention_in_days = 7
}
resource "aws_iam_role" "execution" {
  name = "${var.project_name}-execution"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole"
    }]
  })
}
resource "aws_iam_role_policy" "execution" {
  name = "image-pull-and-logs"
  role = aws_iam_role.execution.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect   = "Allow"
        Action   = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.app.arn
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.app.arn}:*"
      }
    ]
  })
}
resource "aws_ecs_cluster" "app" {
  name = var.project_name
}

# Both task definition and service are absent until an immutable image exists.
resource "aws_ecs_task_definition" "app" {
  count                    = var.enable_service ? 1 : 0
  family                   = var.project_name
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = tostring(var.task_cpu)
  memory                   = tostring(var.task_memory)
  execution_role_arn       = aws_iam_role.execution.arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "app"
    image        = var.image_uri
    essential    = true
    user         = "10001:10001"
    portMappings = [{ containerPort = 8000, hostPort = 8000, protocol = "tcp" }]
    environment = concat([
      { name = "APP_MODE", value = var.auth_enabled ? "cognito" : "placeholder" },
      { name = "PUBLIC_BASE_URL", value = local.cloudfront_url }
      ], var.portal_title != "" ? [
      { name = "PORTAL_TITLE", value = var.portal_title }
      ] : [], var.auth_enabled ? [
      { name = "COGNITO_CLIENT_ID", value = aws_cognito_user_pool_client.app[0].id },
      { name = "COGNITO_ISSUER", value = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.app[0].id}" },
      { name = "COGNITO_DOMAIN", value = local.cognito_domain }
      ] : [], var.marimohub_portal_link_enabled ? [
      { name = "MARIMOHUB_URL", value = local.hub_url }
    ] : [])
    healthCheck = {
      command  = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 30
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.app.name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "app"
      }
    }
    stopTimeout = 45
  }])
  lifecycle {
    precondition {
      condition     = var.image_uri == "${aws_ecr_repository.app.repository_url}@${try(split("@", var.image_uri)[1], "")}" && can(regex("@sha256:[a-f0-9]{64}$", var.image_uri))
      error_message = "Task image must be an immutable digest in this stack's repository."
    }
  }
  depends_on = [aws_iam_role_policy.execution, aws_cognito_managed_login_branding.app]
}
resource "aws_ecs_service" "app" {
  count                              = var.enable_service ? 1 : 0
  name                               = var.project_name
  cluster                            = aws_ecs_cluster.app.id
  task_definition                    = aws_ecs_task_definition.app[0].arn
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  desired_count                      = 1
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 60
  wait_for_steady_state              = true
  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.task.id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.app.arn
    container_name   = "app"
    container_port   = 8000
  }
  timeouts {
    create = "20m"
    update = "20m"
    delete = "20m"
  }
  depends_on = [
    aws_lb_listener.app, aws_iam_role_policy.execution,
    aws_vpc_security_group_ingress_rule.task, aws_vpc_security_group_ingress_rule.alb,
    aws_vpc_security_group_egress_rule.alb, aws_vpc_security_group_egress_rule.task_https,
    aws_route.nat, aws_route.internet, aws_route_table_association.public,
    aws_route_table_association.private, aws_cognito_managed_login_branding.app,
    aws_ecs_service.hub
  ]
}
