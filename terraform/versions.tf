terraform {
  required_version = ">= 1.6.0"

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

  # Partial configuration: supply bucket/key/region at init time, e.g.
  #   terraform init -backend-config=backend.hcl
  # See backend.hcl.example.
  backend "s3" {}
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
