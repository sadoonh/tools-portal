# Additive resources; existing portal resources keep their addresses/configuration.
locals {
  hub_count     = var.marimohub_enabled && var.auth_enabled ? 1 : 0
  hub_run_count = local.hub_count == 1 && var.marimohub_service_enabled ? 1 : 0
  hub_url       = local.hub_count == 1 ? "https://${aws_cloudfront_distribution.hub[0].domain_name}" : ""
  hub_owner     = "${var.project_name}-marimohub"
  hub_tags      = { Tool = "Marimohub", Purpose = "Trusted-user notebook pilot" }
  hub_roles     = local.hub_count == 1 ? toset(["hub", "kernel", "hub-execution", "kernel-execution"]) : toset([])
  hub_names     = local.hub_count == 1 ? toset(["hub", "kernel"]) : toset([])
  kernel_ports  = local.hub_count == 1 ? toset(["2717", "2718"]) : toset([])
  ecs_trust = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect = "Allow", Principal = { Service = "ecs-tasks.amazonaws.com" }, Action = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = var.aws_account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:ecs:${var.aws_region}:${var.aws_account_id}:*" }
      }
    }]
  })
}

resource "aws_ecr_repository" "hub_images" {
  for_each             = local.hub_names
  name                 = "${var.project_name}-${each.key}"
  image_tag_mutability = "IMMUTABLE"
  force_delete         = false
  image_scanning_configuration { scan_on_push = true }
  tags = local.hub_tags
}
resource "aws_cloudwatch_log_group" "hub_logs" {
  for_each          = local.hub_names
  name              = "/ecs/${var.project_name}-${each.key}"
  retention_in_days = 7
  tags              = local.hub_tags
}
resource "aws_s3_bucket" "hub" {
  count         = local.hub_count
  bucket        = "${var.project_name}-${var.aws_account_id}-notebooks"
  force_destroy = false
  tags          = local.hub_tags
}
resource "aws_s3_bucket_public_access_block" "hub" {
  count                   = local.hub_count
  bucket                  = aws_s3_bucket.hub[0].id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}
resource "aws_s3_bucket_ownership_controls" "hub" {
  count  = local.hub_count
  bucket = aws_s3_bucket.hub[0].id
  rule { object_ownership = "BucketOwnerEnforced" }
}
resource "aws_s3_bucket_versioning" "hub" {
  count  = local.hub_count
  bucket = aws_s3_bucket.hub[0].id
  versioning_configuration { status = "Enabled" }
}
resource "aws_s3_bucket_server_side_encryption_configuration" "hub" {
  count  = local.hub_count
  bucket = aws_s3_bucket.hub[0].id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}
resource "aws_s3_bucket_policy" "hub" {
  count  = local.hub_count
  bucket = aws_s3_bucket.hub[0].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Sid       = "DenyNonTLS", Effect = "Deny", Principal = "*", Action = "s3:*"
      Resource  = [aws_s3_bucket.hub[0].arn, "${aws_s3_bucket.hub[0].arn}/*"]
      Condition = { Bool = { "aws:SecureTransport" = "false" } }
    }]
  })
  depends_on = [aws_s3_bucket_public_access_block.hub]
}

resource "aws_security_group" "hub_network" {
  for_each    = local.hub_names
  name        = "${var.project_name}-${each.key}"
  description = "Marimohub ${each.key}: private peer-restricted ingress"
  vpc_id      = aws_vpc.app.id
  tags        = local.hub_tags
}
resource "aws_vpc_security_group_ingress_rule" "hub" {
  count                        = local.hub_count
  security_group_id            = aws_security_group.hub_network["hub"].id
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
}
resource "aws_vpc_security_group_egress_rule" "alb_hub" {
  count                        = local.hub_count
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = aws_security_group.hub_network["hub"].id
  ip_protocol                  = "tcp"
  from_port                    = 3000
  to_port                      = 3000
}
resource "aws_vpc_security_group_ingress_rule" "kernel" {
  for_each                     = local.kernel_ports
  security_group_id            = aws_security_group.hub_network["kernel"].id
  referenced_security_group_id = aws_security_group.hub_network["hub"].id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}
resource "aws_vpc_security_group_egress_rule" "hub_kernel" {
  for_each                     = local.kernel_ports
  security_group_id            = aws_security_group.hub_network["hub"].id
  referenced_security_group_id = aws_security_group.hub_network["kernel"].id
  ip_protocol                  = "tcp"
  from_port                    = tonumber(each.value)
  to_port                      = tonumber(each.value)
}
resource "aws_vpc_security_group_egress_rule" "hub_https" {
  for_each          = local.hub_names
  security_group_id = aws_security_group.hub_network[each.key].id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_lb_target_group" "hub" {
  count                = local.hub_count
  name                 = "${var.project_name}-hub"
  port                 = 3000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.app.id
  deregistration_delay = 30
  health_check {
    path                = "/api/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
  tags = local.hub_tags
}
# CachingDisabled governs CloudFront, not browser caches. Upstream API/proxy
# responses do not consistently set no-store, so enforce it at this hub's edge.
resource "aws_cloudfront_response_headers_policy" "hub" {
  count = local.hub_count
  name  = "${var.project_name}-hub-no-store"
  custom_headers_config {
    items {
      header   = "Cache-Control"
      value    = "private, no-store"
      override = true
    }
  }
  security_headers_config {
    referrer_policy {
      referrer_policy = "no-referrer"
      override        = true
    }
    content_type_options { override = true }
  }
}
resource "aws_cloudfront_distribution" "hub" {
  count               = local.hub_count
  enabled             = true
  comment             = "${var.project_name} Marimohub"
  wait_for_deployment = true
  retain_on_delete    = false
  is_ipv6_enabled     = true
  http_version        = "http2"
  origin {
    domain_name = aws_lb.app.dns_name
    origin_id   = "private-alb"
    vpc_origin_config {
      vpc_origin_id            = aws_cloudfront_vpc_origin.app.id
      origin_read_timeout      = 60
      origin_keepalive_timeout = 5
    }
  }
  default_cache_behavior {
    target_origin_id           = "private-alb"
    viewer_protocol_policy     = "redirect-to-https"
    allowed_methods            = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods             = ["GET", "HEAD"]
    cache_policy_id            = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id   = data.aws_cloudfront_origin_request_policy.viewer.id
    response_headers_policy_id = aws_cloudfront_response_headers_policy.hub[0].id
    compress                   = false
  }
  dynamic "custom_error_response" {
    for_each = toset([400, 403, 404, 405, 414, 416, 500, 501, 502, 503, 504])
    content {
      error_code            = custom_error_response.value
      error_caching_min_ttl = 0
    }
  }
  restrictions {
    geo_restriction { restriction_type = "none" }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1"
  }
  tags = local.hub_tags
  # No dependency on the host rule: its hostname comes from this distribution.
  depends_on = [aws_vpc_security_group_ingress_rule.alb]
}
resource "aws_lb_listener_rule" "hub" {
  count        = local.hub_count
  listener_arn = aws_lb_listener.app.arn
  priority     = 100
  action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.hub[0].arn
  }
  condition {
    host_header { values = [aws_cloudfront_distribution.hub[0].domain_name] }
  }
  tags = local.hub_tags
}

resource "aws_cognito_user_pool_client" "hub" {
  count                                = local.hub_count
  name                                 = "${var.project_name}-marimohub"
  user_pool_id                         = aws_cognito_user_pool.app[0].id
  generate_secret                      = true
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.hub_url}/api/auth/callback"]
  logout_urls                          = ["${local.hub_url}/"]
  default_redirect_uri                 = "${local.hub_url}/api/auth/callback"
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  access_token_validity                = 1
  id_token_validity                    = 1
  refresh_token_validity               = 1
  token_validity_units {
    access_token  = "hours"
    id_token      = "hours"
    refresh_token = "hours"
  }
}
resource "aws_cognito_managed_login_branding" "hub" {
  count                       = local.hub_count
  client_id                   = aws_cognito_user_pool_client.hub[0].id
  user_pool_id                = aws_cognito_user_pool.app[0].id
  use_cognito_provided_values = true
  depends_on                  = [aws_cognito_user_pool_domain.app]
}
resource "aws_secretsmanager_secret" "hub" {
  count                   = local.hub_count
  name                    = "${var.project_name}/marimohub"
  recovery_window_in_days = 7
  tags                    = local.hub_tags
}
# Ephemeral randomness is not stored in plans/state. Only the explicit WO
# revision triggers rotation; do NOT bump it with active notebook tasks.
ephemeral "random_password" "hub_session" {
  count   = local.hub_count
  length  = 64
  special = false
}
ephemeral "random_password" "hub_agent" {
  count   = local.hub_count
  length  = 64
  special = false
}
resource "aws_secretsmanager_secret_version" "hub" {
  count                    = local.hub_count
  secret_id                = aws_secretsmanager_secret.hub[0].id
  secret_string_wo_version = var.marimohub_secret_revision
  secret_string_wo = jsonencode({
    oidc_client_secret = aws_cognito_user_pool_client.hub[0].client_secret
    session_secret     = ephemeral.random_password.hub_session[0].result
    agent_secret       = ephemeral.random_password.hub_agent[0].result
  })
  # A replaced OIDC client needs matching credentials; drain kernels before
  # any such replacement because this also changes session/agent secrets.
  lifecycle {
    replace_triggered_by = [aws_cognito_user_pool_client.hub[0].id]
  }
}
resource "aws_iam_role" "hub_roles" {
  for_each           = local.hub_roles
  name               = "${var.project_name}-${each.key}"
  assume_role_policy = local.ecs_trust
  tags               = local.hub_tags
}
resource "aws_iam_role_policy" "hub_execution" {
  for_each = local.hub_names
  name     = "project-image-and-logs"
  role     = aws_iam_role.hub_roles["${each.key}-execution"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = concat([
      { Effect = "Allow", Action = ["ecr:GetAuthorizationToken"], Resource = "*" },
      {
        Effect   = "Allow", Action = ["ecr:BatchCheckLayerAvailability", "ecr:GetDownloadUrlForLayer", "ecr:BatchGetImage"]
        Resource = aws_ecr_repository.hub_images[each.key].arn
      },
      {
        Effect   = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.hub_logs[each.key].arn}:*"
      }
      ], each.key == "hub" ? [{
        Effect = "Allow", Action = ["secretsmanager:GetSecretValue"], Resource = aws_secretsmanager_secret.hub[0].arn
    }] : [])
  })
}

resource "aws_ecs_task_definition" "kernel" {
  count                    = local.hub_run_count
  family                   = "${var.project_name}-kernel"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "1024"
  memory                   = "2048"
  execution_role_arn       = aws_iam_role.hub_roles["kernel-execution"].arn
  task_role_arn            = aws_iam_role.hub_roles["kernel"].arn # Deliberately no IAM policy.
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name             = "marimo", image = var.marimohub_kernel_image_uri, essential = true, user = "10001:10001"
    workingDirectory = "/workspace"
    command          = ["/usr/local/bin/python3", "/opt/marimohub/fargate_agent.py"]
    environment      = [{ name = "MARIMOHUB_AGENT_PORT", value = "2717" }]
    portMappings     = [for port in [2717, 2718] : { containerPort = port, hostPort = port, protocol = "tcp" }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.hub_logs["kernel"].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "kernel"
      }
    }
    stopTimeout = 30
  }])
  tags       = merge(local.hub_tags, { "managed-by" = "marimohub", purpose = "notebook-kernel" })
  depends_on = [aws_iam_role_policy.hub_execution]
}
resource "aws_iam_role_policy" "hub_runtime" {
  count = local.hub_run_count
  name  = "catalog-and-owned-kernels"
  role  = aws_iam_role.hub_roles["hub"].id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "CatalogBucket", Effect = "Allow", Action = ["s3:ListBucket", "s3:GetBucketLocation"]
        Resource = aws_s3_bucket.hub[0].arn
      },
      {
        Sid      = "CatalogObjects", Effect = "Allow", Action = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
        Resource = "${aws_s3_bucket.hub[0].arn}/*"
      },
      {
        Sid       = "RunApprovedKernel", Effect = "Allow", Action = ["ecs:RunTask"]
        Resource  = aws_ecs_task_definition.kernel[0].arn
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.app.arn } }
      },
      {
        Sid       = "ListConfiguredCluster", Effect = "Allow", Action = ["ecs:ListTasks"], Resource = "*"
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.app.arn } }
      },
      {
        Sid      = "InspectAndStopOwnedKernels", Effect = "Allow", Action = ["ecs:DescribeTasks", "ecs:ListTagsForResource", "ecs:StopTask"]
        Resource = "arn:aws:ecs:${var.aws_region}:${var.aws_account_id}:task/${var.project_name}/*"
        Condition = {
          ArnEquals    = { "ecs:cluster" = aws_ecs_cluster.app.arn }
          StringEquals = { "aws:ResourceTag/marimohub:owner" = local.hub_owner }
        }
      },
      {
        Sid = "DescribeConfiguredCluster", Effect = "Allow", Action = ["ecs:DescribeClusters"], Resource = aws_ecs_cluster.app.arn
      },
      {
        Sid = "DescribeTaskDefinitions", Effect = "Allow", Action = ["ecs:DescribeTaskDefinition"], Resource = "*"
      },
      {
        Sid       = "TagOnlyOnCreate", Effect = "Allow", Action = ["ecs:TagResource"]
        Resource  = "arn:aws:ecs:${var.aws_region}:${var.aws_account_id}:task/${var.project_name}/*"
        Condition = { StringEquals = { "ecs:CreateAction" = "RunTask" } }
      },
      {
        Sid       = "PassOnlyKernelRoles", Effect = "Allow", Action = ["iam:PassRole"]
        Resource  = [aws_iam_role.hub_roles["kernel"].arn, aws_iam_role.hub_roles["kernel-execution"].arn]
        Condition = { StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" } }
      }
    ]
  })
}
resource "aws_ecs_task_definition" "hub" {
  count                    = local.hub_run_count
  family                   = "${var.project_name}-hub"
  network_mode             = "awsvpc"
  requires_compatibilities = ["FARGATE"]
  cpu                      = "512"
  memory                   = "1024"
  execution_role_arn       = aws_iam_role.hub_roles["hub-execution"].arn
  task_role_arn            = aws_iam_role.hub_roles["hub"].arn
  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }
  container_definitions = jsonencode([{
    name         = "hub", image = var.marimohub_server_image_uri, essential = true, user = "10001:10001"
    portMappings = [{ containerPort = 3000, hostPort = 3000, protocol = "tcp" }]
    environment = [for name, value in {
      NODE_ENV                                        = "production"
      PORT                                            = "3000"
      AWS_REGION                                      = var.aws_region
      MARIMOHUB_APP_BASE_URL                          = local.hub_url
      MARIMOHUB_ALLOWED_ORIGINS                       = local.hub_url
      MARIMOHUB_STORAGE_BACKEND                       = "s3"
      MARIMOHUB_STORAGE_S3_BUCKET                     = aws_s3_bucket.hub[0].id
      MARIMOHUB_STORAGE_S3_REGION                     = var.aws_region
      MARIMOHUB_AUTH_BACKEND                          = "oidc"
      MARIMOHUB_AUTH_OIDC_ISSUER                      = "https://cognito-idp.${var.aws_region}.amazonaws.com/${aws_cognito_user_pool.app[0].id}"
      MARIMOHUB_AUTH_OIDC_CLIENT_ID                   = aws_cognito_user_pool_client.hub[0].id
      MARIMOHUB_AUTH_OIDC_REDIRECT_URI                = "${local.hub_url}/api/auth/callback"
      MARIMOHUB_AUTH_OIDC_SCOPES                      = "openid email"
      MARIMOHUB_AUTH_OIDC_PROMPT                      = "login"
      MARIMOHUB_AUTH_OIDC_EMAIL_VERIFICATION          = "required"
      MARIMOHUB_AUTH_ALLOWED_EMAIL_DOMAINS            = join(",", var.marimohub_allowed_email_domains)
      MARIMOHUB_AUTH_SESSION_TTL_SECONDS              = "3600"
      MARIMOHUB_COMPUTE_BACKEND                       = "fargate"
      MARIMOHUB_COMPUTE_FARGATE_CLUSTER               = aws_ecs_cluster.app.arn
      MARIMOHUB_COMPUTE_FARGATE_TASK_DEFINITION       = aws_ecs_task_definition.kernel[0].arn
      MARIMOHUB_COMPUTE_FARGATE_CONTAINER_NAME        = "marimo"
      MARIMOHUB_COMPUTE_FARGATE_SUBNETS               = join(",", [for subnet in aws_subnet.private : subnet.id])
      MARIMOHUB_COMPUTE_FARGATE_SECURITY_GROUPS       = aws_security_group.hub_network["kernel"].id
      MARIMOHUB_COMPUTE_FARGATE_ASSIGN_PUBLIC_IP      = "false"
      MARIMOHUB_COMPUTE_FARGATE_PLATFORM_VERSION      = "1.4.0"
      MARIMOHUB_COMPUTE_FARGATE_OWNER                 = local.hub_owner
      MARIMOHUB_COMPUTE_FARGATE_AGENT_PORT            = "2717"
      MARIMOHUB_COMPUTE_FARGATE_READY_TIMEOUT_SECONDS = "180"
      MARIMOHUB_SANDBOX_STARTUP_TIMEOUT_SECONDS       = "240"
      MARIMOHUB_COMPUTE_PROFILES                      = "pilot:cpu=1;mem=2Gi"
      MARIMOHUB_COMPUTE_PROFILE_OVERRIDE              = "none"
      MARIMOHUB_COMPUTE_WORKDIR                       = "/workspace"
      MARIMOHUB_SANDBOX_EXPOSURE                      = "proxy"
      MARIMOHUB_SANDBOX_PROXY_ACK_UNTRUSTED           = tostring(var.marimohub_proxy_ack)
      MARIMOHUB_SANDBOX_AUTH                          = "on"
      MARIMOHUB_DEFAULT_ROLE                          = "none"
      MARIMOHUB_PROJECT_CREATION                      = "open"
      MARIMOHUB_VIEWER_MODE                           = "static"
      MARIMOHUB_EDITOR_SANDBOX_SHARING                = "exclusive"
      MARIMOHUB_PERSIST_WORKSPACE                     = "source"
      MARIMOHUB_RUN_MAINTENANCE                       = "true"
      MARIMOHUB_MAX_SESSIONS_PER_USER                 = "1"
      MARIMOHUB_MAX_APPS_PER_PROJECT                  = "1"
      MARIMOHUB_APP_MAX_SESSIONS_PER_VERSION          = "1"
      MARIMOHUB_SESSION_IDLE_TIMEOUT_SECONDS          = "900"
      MARIMOHUB_SESSION_MAX_LIFETIME_SECONDS          = "7200"
      MARIMOHUB_SESSION_SNAPSHOT_INTERVAL_SECONDS     = "60"
      MARIMOHUB_SESSION_SWEEP_INTERVAL_SECONDS        = "60"
      MARIMOHUB_SURFACES                              = "marimo"
      MARIMOHUB_AI_BACKEND                            = "none"
      MARIMOHUB_JOBS                                  = "off"
      MARIMOHUB_MCP                                   = "off"
      MARIMOHUB_AUTOMATIC_THUMBNAILS                  = "false"
    } : { name = name, value = value }]
    secrets = [for name, key in {
      MARIMOHUB_AUTH_OIDC_CLIENT_SECRET      = "oidc_client_secret"
      MARIMOHUB_AUTH_SESSION_SECRET          = "session_secret"
      MARIMOHUB_COMPUTE_FARGATE_AGENT_SECRET = "agent_secret"
    } : { name = name, valueFrom = "${aws_secretsmanager_secret.hub[0].arn}:${key}::" }]
    healthCheck = {
      command  = ["CMD", "node", "-e", "fetch('http://127.0.0.1:3000/api/health').then(r=>{if(!r.ok)process.exit(1)}).catch(()=>process.exit(1))"]
      interval = 30, timeout = 5, retries = 3, startPeriod = 60
    }
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.hub_logs["hub"].name
        awslogs-region        = var.aws_region
        awslogs-stream-prefix = "hub"
      }
    }
    stopTimeout = 60
  }])
  tags = local.hub_tags
  depends_on = [
    aws_iam_role_policy.hub_runtime, aws_iam_role_policy.hub_execution,
    aws_secretsmanager_secret_version.hub, aws_cognito_managed_login_branding.hub,
    aws_s3_bucket_versioning.hub, aws_s3_bucket_policy.hub,
    aws_s3_bucket_server_side_encryption_configuration.hub, aws_s3_bucket_ownership_controls.hub
  ]
}
resource "aws_ecs_service" "hub" {
  count                              = local.hub_run_count
  name                               = "${var.project_name}-hub"
  cluster                            = aws_ecs_cluster.app.id
  task_definition                    = aws_ecs_task_definition.hub[0].arn
  launch_type                        = "FARGATE"
  platform_version                   = "1.4.0"
  desired_count                      = 1
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100
  health_check_grace_period_seconds  = 120
  wait_for_steady_state              = true
  network_configuration {
    subnets          = [for subnet in aws_subnet.private : subnet.id]
    security_groups  = [aws_security_group.hub_network["hub"].id]
    assign_public_ip = false
  }
  load_balancer {
    target_group_arn = aws_lb_target_group.hub[0].arn
    container_name   = "hub"
    container_port   = 3000
  }
  timeouts {
    create = "20m"
    update = "20m"
    delete = "20m"
  }
  tags = local.hub_tags
  depends_on = [
    aws_lb_listener_rule.hub, aws_iam_role_policy.hub_runtime, aws_iam_role_policy.hub_execution,
    aws_vpc_security_group_ingress_rule.hub, aws_vpc_security_group_egress_rule.alb_hub,
    aws_vpc_security_group_ingress_rule.kernel, aws_vpc_security_group_egress_rule.hub_kernel,
    aws_vpc_security_group_egress_rule.hub_https, aws_route.nat, aws_route.internet,
    aws_route_table_association.public, aws_route_table_association.private
  ]
}
