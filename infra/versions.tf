terraform {
  required_version = "= 1.16.5"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.68.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.1"
    }
  }
  # Intentionally local state. Never publish state or disable locking.
}

provider "aws" {
  region              = var.aws_region
  allowed_account_ids = [var.aws_account_id]
  default_tags {
    tags = {
      Project = var.project_name
      Owner   = var.owner
      Purpose = var.deployment_purpose
    }
  }
}
