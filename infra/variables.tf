variable "aws_account_id" {
  type        = string
  description = "Confirmed target AWS account; guards against applying in another account."
  validation {
    condition     = can(regex("^[0-9]{12}$", var.aws_account_id))
    error_message = "Supply the confirmed 12-digit AWS account ID."
  }
}

variable "aws_region" {
  type        = string
  description = "Explicitly approved commercial AWS Region with VPC-origin support."
  validation {
    condition = contains([
      "us-east-1", "us-east-2", "us-west-1", "us-west-2", "af-south-1",
      "ap-east-1", "ap-east-2", "ap-south-1", "ap-south-2", "ap-southeast-1",
      "ap-southeast-2", "ap-southeast-3", "ap-southeast-4", "ap-southeast-5",
      "ap-southeast-6", "ap-southeast-7", "ap-northeast-1", "ap-northeast-2",
      "ap-northeast-3", "ca-central-1", "ca-west-1", "eu-central-1", "eu-central-2",
      "eu-west-1", "eu-west-2", "eu-west-3", "eu-south-1", "eu-south-2",
      "eu-north-1", "il-central-1", "me-south-1", "me-central-1", "sa-east-1", "mx-central-1"
    ], var.aws_region)
    error_message = "Region must support CloudFront VPC origins; recheck AWS official documentation."
  }
}

variable "project_name" {
  type        = string
  description = "Unique lower-case project name, also used for ALB/target-group names."
  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{2,23}[a-z0-9]$", var.project_name)) && !startswith(var.project_name, "internal-")
    error_message = "Use 4–25 lower-case letters/digits/hyphens, starting with a letter, ending alphanumeric, not internal-."
  }
}

variable "owner" {
  type        = string
  description = "Approved operator responsible for teardown (nonsecret tag)."
  validation {
    condition     = length(trimspace(var.owner)) > 0
    error_message = "A teardown owner is required."
  }
}

variable "availability_zones" {
  type        = list(string)
  description = "Exactly two reviewed standard AZ names; AZ IDs checked through AWS below."
  validation {
    condition     = length(var.availability_zones) == 2 && length(distinct(var.availability_zones)) == 2
    error_message = "Select exactly two different supported standard availability zones."
  }
}

variable "vpc_cidr" {
  type        = string
  description = "Reviewed nonoverlapping RFC1918 IPv4 /16–/22; creates three /24 subnets."
  validation {
    condition = can(cidrnetmask(var.vpc_cidr)) && try(
      tonumber(split("/", var.vpc_cidr)[1]) >= 16 && tonumber(split("/", var.vpc_cidr)[1]) <= 22 &&
    can(regex("^(10\\.|172\\.(1[6-9]|2[0-9]|3[01])\\.|192\\.168\\.)", cidrhost(var.vpc_cidr, 0))), false)
    error_message = "Use a reviewed nonoverlapping RFC1918 IPv4 CIDR with prefix /16 through /22."
  }
}

variable "deployment_purpose" {
  type        = string
  default     = "Disposable protected-page MVP"
  description = "Nonsecret purpose tag; optionally override it in Terraform inputs."
  validation {
    condition     = length(trimspace(var.deployment_purpose)) > 0
    error_message = "A deployment purpose is required."
  }
}

variable "protect_resources" {
  type        = bool
  default     = false
  description = "Optional ALB/Cognito/ECR deletion protection; false matches the original deployment."
}

variable "portal_title" {
  type        = string
  default     = ""
  description = "Optional plain-text portal title; blank preserves the existing task environment/title."
  validation {
    condition     = (var.portal_title == "" || length(trimspace(var.portal_title)) > 0) && length(var.portal_title) <= 80 && !can(regex("[\\r\\n]", var.portal_title))
    error_message = "Portal title must be at most 80 characters without line breaks."
  }
}

variable "enable_service" {
  type        = bool
  default     = false
  description = "False for first infrastructure-only apply or fail-closed recovery."
}

variable "auth_enabled" {
  type        = bool
  default     = false
  description = "True for the final pilot; never revert to false after pilot sharing."
}

variable "image_uri" {
  type        = string
  default     = ""
  description = "This stack's immutable ECR repository@sha256 digest; empty only while disabled."
  validation {
    condition = !var.enable_service || can(regex(
      "^${var.aws_account_id}\\.dkr\\.ecr\\.${var.aws_region}\\.amazonaws\\.com/${var.project_name}@sha256:[a-f0-9]{64}$", var.image_uri
    ))
    error_message = "Enabled service requires this stack's ECR repository and an immutable SHA-256 digest."
  }
}

variable "task_cpu" {
  type    = number
  default = 256
  validation {
    condition     = contains([256, 512, 1024, 2048, 4096], var.task_cpu)
    error_message = "Use a supported small Fargate CPU size (256–4096)."
  }
}

variable "task_memory" {
  type    = number
  default = 512
  validation {
    condition = contains(lookup({
      "256"  = [512, 1024, 2048]
      "512"  = [1024, 2048, 3072, 4096]
      "1024" = range(2048, 8193, 1024)
      "2048" = range(4096, 16385, 1024)
      "4096" = range(8192, 30721, 1024)
    }, tostring(var.task_cpu), []), var.task_memory)
    error_message = "Memory must be a supported Fargate CPU/memory combination."
  }
}
