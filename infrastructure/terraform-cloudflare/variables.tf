variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:Edit, DNS:Edit, Cloudflare Tunnel:Edit permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for the target domain"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare Account ID"
  type        = string
  sensitive   = true
}

variable "tunnel_name" {
  description = "Human-readable name for the Cloudflare Tunnel"
  type        = string
  default     = "seshat-vps"
}

variable "domain" {
  description = "Root domain managed in this Cloudflare zone"
  type        = string
  default     = "frenchforet.com"
}
