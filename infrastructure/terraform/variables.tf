variable "ovh_application_key" {
  description = "OVH API application key (from eu.api.ovh.com/createApp)"
  type        = string
  sensitive   = true
}

variable "ovh_application_secret" {
  description = "OVH API application secret"
  type        = string
  sensitive   = true
}

variable "ovh_consumer_key" {
  description = "OVH API consumer key (generated via token request)"
  type        = string
  sensitive   = true
}

variable "vps_ip" {
  description = "VPS public IPv4 address"
  type        = string
  sensitive   = true

  validation {
    condition     = can(regex("^(\\d{1,3}\\.){3}\\d{1,3}$", var.vps_ip))
    error_message = "vps_ip must be a valid IPv4 address."
  }
}

variable "ssh_port" {
  description = "SSH daemon port configured on the server"
  type        = number
  sensitive   = true

  validation {
    condition     = var.ssh_port > 1024 && var.ssh_port < 65535
    error_message = "SSH port must be in the range 1025–65534."
  }
}
