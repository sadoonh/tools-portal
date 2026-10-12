# Identity is downstream of CloudFront, never a dependency of the distribution.
resource "aws_cognito_user_pool" "app" {
  count                    = var.auth_enabled ? 1 : 0
  name                     = var.project_name
  user_pool_tier           = "ESSENTIALS"
  deletion_protection      = var.protect_resources ? "ACTIVE" : "INACTIVE"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]
  mfa_configuration        = "OFF" # Organization policy approval required; no SMS infrastructure.
  username_configuration {
    case_sensitive = false
  }
  admin_create_user_config {
    allow_admin_create_user_only = true
  }
  password_policy {
    minimum_length                   = 12
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }
  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
  email_configuration {
    email_sending_account = "COGNITO_DEFAULT"
  }
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }
}
resource "aws_cognito_user_pool_domain" "app" {
  count                 = var.auth_enabled ? 1 : 0
  domain                = "${var.project_name}-${var.aws_account_id}"
  user_pool_id          = aws_cognito_user_pool.app[0].id
  managed_login_version = 2
}
resource "aws_cognito_user_pool_client" "app" {
  count                                = var.auth_enabled ? 1 : 0
  name                                 = var.project_name
  user_pool_id                         = aws_cognito_user_pool.app[0].id
  generate_secret                      = false
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["${local.cloudfront_url}/callback"]
  logout_urls                          = ["${local.cloudfront_url}/logout"]
  default_redirect_uri                 = "${local.cloudfront_url}/callback"
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
resource "aws_cognito_managed_login_branding" "app" {
  count                       = var.auth_enabled ? 1 : 0
  client_id                   = aws_cognito_user_pool_client.app[0].id
  user_pool_id                = aws_cognito_user_pool.app[0].id
  use_cognito_provided_values = true
  depends_on                  = [aws_cognito_user_pool_domain.app]
}

