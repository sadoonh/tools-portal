# All extension switches are off by default: preserve the live portal.
variable "marimohub_enabled" {
  type        = bool
  default     = false
  description = "Create the additive hub foundation. Turning off after deployment is teardown, not recovery."
  validation {
    condition     = !var.marimohub_enabled || var.auth_enabled
    error_message = "Marimohub requires the existing administrator-only Cognito pool."
  }
}

variable "marimohub_service_enabled" {
  type        = bool
  default     = false
  description = "Run one private hub after approved images and explicit proxy-risk acceptance."
  validation {
    condition     = !var.marimohub_service_enabled || (var.marimohub_enabled && var.auth_enabled && var.marimohub_proxy_ack)
    error_message = "Hub service requires foundation, Cognito and explicit trusted-author proxy/XSS acceptance."
  }
}

variable "marimohub_proxy_ack" {
  type        = bool
  default     = false
  description = "Operator acceptance of upstream same-origin notebook JavaScript/XSS and trusted-user-only pilot."
}

variable "marimohub_portal_link_enabled" {
  type        = bool
  default     = false
  description = "Expose the hub link only after deploying the tested launcher-capable portal image."
  validation {
    condition     = !var.marimohub_portal_link_enabled || (var.marimohub_service_enabled && var.enable_service && var.auth_enabled)
    error_message = "Launcher link requires running authenticated portal and hub services."
  }
}

variable "marimohub_server_image_uri" {
  type        = string
  default     = ""
  description = "Immutable Linux/amd64 official hub mirror digest in this project's hub ECR repository."
  validation {
    condition = !var.marimohub_service_enabled || can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${var.project_name}-hub@sha256:[a-f0-9]{64}$", var.marimohub_server_image_uri
    ))
    error_message = "Enabled hub requires the project's hub ECR repo@sha256 digest."
  }
}

variable "marimohub_kernel_image_uri" {
  type        = string
  default     = ""
  description = "Immutable Linux/amd64 kernel image containing the pinned official Fargate agent."
  validation {
    condition = !var.marimohub_service_enabled || can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${var.project_name}-kernel@sha256:[a-f0-9]{64}$", var.marimohub_kernel_image_uri
    ))
    error_message = "Enabled hub requires the project's kernel ECR repo@sha256 digest."
  }
}

variable "marimohub_allowed_email_domains" {
  type        = list(string)
  default     = []
  description = "Explicit organization email domains; no personal domain default. Cognito invitations remain the exact-user gate."
  validation {
    condition = (!var.marimohub_enabled || length(var.marimohub_allowed_email_domains) > 0) && alltrue([
      for domain in var.marimohub_allowed_email_domains : can(regex("^([a-z0-9][a-z0-9-]*\\.)+[a-z]{2,63}$", domain))
    ])
    error_message = "Supply nonempty explicit lowercase DNS email domains; wildcard admission is not permitted."
  }
}

variable "marimohub_secret_revision" {
  type        = number
  default     = 1
  description = "Increase only after draining notebooks: rotates session/agent secrets, invalidating old sessions/tasks."
  validation {
    condition     = var.marimohub_secret_revision >= 1 && floor(var.marimohub_secret_revision) == var.marimohub_secret_revision
    error_message = "Secret revision must be a positive integer."
  }
}
