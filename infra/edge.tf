resource "aws_lb" "app" {
  name                       = "${var.project_name}-alb"
  internal                   = true
  load_balancer_type         = "application"
  ip_address_type            = "ipv4"
  security_groups            = [aws_security_group.alb.id]
  subnets                    = [for subnet in aws_subnet.private : subnet.id]
  enable_deletion_protection = var.protect_resources
}
resource "aws_lb_target_group" "app" {
  name        = "${var.project_name}-tg"
  port        = 8000
  protocol    = "HTTP"
  target_type = "ip"
  vpc_id      = aws_vpc.app.id
  # Replacements deliberately stop/start. Don't delay old-task shutdown.
  deregistration_delay = 30
  health_check {
    path                = "/health"
    protocol            = "HTTP"
    matcher             = "200"
    interval            = 30
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}
resource "aws_lb_listener" "app" {
  load_balancer_arn = aws_lb.app.arn
  port              = 80
  protocol          = "HTTP"
  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.app.arn
  }
}

# CloudFront creates its AWS-owned SG first; lookup is unambiguous within VPC.
resource "aws_cloudfront_vpc_origin" "app" {
  vpc_origin_endpoint_config {
    name                   = var.project_name
    arn                    = aws_lb.app.arn
    http_port              = 80
    https_port             = 443 # Required schema field; no HTTPS listener exists.
    origin_protocol_policy = "http-only"
    origin_ssl_protocols {
      items    = ["TLSv1.2"]
      quantity = 1
    }
  }
  timeouts {
    create = "30m"
    delete = "30m"
  }
  depends_on = [aws_lb_listener.app, aws_internet_gateway.app]
}
data "aws_security_group" "cloudfront" {
  filter {
    name   = "vpc-id"
    values = [aws_vpc.app.id]
  }
  filter {
    name   = "group-name"
    values = ["CloudFront-VPCOrigins-Service-SG*"]
  }
  depends_on = [aws_cloudfront_vpc_origin.app]
}
resource "aws_vpc_security_group_ingress_rule" "alb" {
  security_group_id            = aws_security_group.alb.id
  referenced_security_group_id = data.aws_security_group.cloudfront.id
  ip_protocol                  = "tcp"
  from_port                    = 80
  to_port                      = 80
}
data "aws_cloudfront_cache_policy" "disabled" {
  name = "Managed-CachingDisabled"
}
data "aws_cloudfront_origin_request_policy" "viewer" {
  name = "Managed-AllViewer"
}
resource "aws_cloudfront_distribution" "app" {
  enabled             = true
  comment             = var.project_name
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
    target_origin_id         = "private-alb"
    viewer_protocol_policy   = "redirect-to-https"
    allowed_methods          = ["GET", "HEAD", "OPTIONS", "PUT", "POST", "PATCH", "DELETE"]
    cached_methods           = ["GET", "HEAD"]
    cache_policy_id          = data.aws_cloudfront_cache_policy.disabled.id
    origin_request_policy_id = data.aws_cloudfront_origin_request_policy.viewer.id
    compress                 = false
  }
  dynamic "custom_error_response" {
    for_each = toset([400, 403, 404, 405, 414, 416, 500, 501, 502, 503, 504])
    content {
      error_code            = custom_error_response.value
      error_caching_min_ttl = 0
    }
  }
  restrictions {
    geo_restriction {
      restriction_type = "none"
    }
  }
  viewer_certificate {
    cloudfront_default_certificate = true
    minimum_protocol_version       = "TLSv1" # Default-certificate limitation, not TLS1.2-only.
  }
  depends_on = [aws_vpc_security_group_ingress_rule.alb]
}

