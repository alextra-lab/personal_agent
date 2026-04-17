terraform {
  required_version = ">= 1.9"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      # ~> 5.0: safe to receive minor updates within major; blocks breaking v6.x changes.
      # Uses a loose pin (unlike the OVH module's exact pin) because the Cloudflare
      # provider follows semver reliably and there is no known v5.x API quirk requiring
      # a specific patch.
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
