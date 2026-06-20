terraform {
  required_version = ">= 1.6"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.13"
    }
  }

  # Local state for now, matching the Luminik marketing-site Cloudflare stack.
  # Move this to shared remote state before more people manage this Worker.
  #
  # backend "s3" {
  #   bucket         = "luminik-tfstate-<account-id>"
  #   key            = "alfred/telemetry-worker.tfstate"
  #   region         = "us-east-1"
  #   dynamodb_table = "luminik-tflock"
  #   encrypt        = true
  # }
}
