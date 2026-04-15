terraform {
  required_version = ">= 1.9"

  required_providers {
    ovh = {
      source  = "ovh/ovh"
      # Pinned to avoid provider/API drift around ip_firewall behavior.
      version = "= 1.8.0"
    }
  }
}

provider "ovh" {
  endpoint           = "ovh-eu"
  application_key    = var.ovh_application_key
  application_secret = var.ovh_application_secret
  consumer_key       = var.ovh_consumer_key
}
