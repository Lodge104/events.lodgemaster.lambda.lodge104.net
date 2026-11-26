terraform {
  # >= 1.10 for S3-native state locking (use_lockfile).
  required_version = ">= 1.10.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    archive = {
      source  = "hashicorp/archive"
      version = "~> 2.7"
    }
  }

  # Shared Lodge 104 state bucket (versioned, SSE-S3, public access blocked).
  # Keys follow the <repo-domain>/terraform.tfstate convention used by the
  # other Lodge 104 Terraform repos. Locking uses an S3 lock file, so no
  # DynamoDB lock table is needed.
  backend "s3" {
    bucket       = "lodge104-terraform-state"
    key          = "events.lodgemaster.lambda.lodge104.net/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true
  }
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      Project   = "lodge104-eventdata"
      ManagedBy = "terraform"
      Repo      = "Lodge104/events.lodgemaster.lambda.lodge104.net"
    }
  }
}
