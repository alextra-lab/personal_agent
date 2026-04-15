# OVH Network Firewall — Seshat VPS
#
# Stateless network-level firewall applied before traffic reaches the OS.
# Rules are processed in sequence order; first match wins.
# The OVH network firewall operates on /ip blocks — for a single VPS IP
# the block is ip/32 and ip_on_firewall is the bare IP.

locals {
  ip_block = "${var.vps_ip}/32"
}

# Create the firewall object for the VPS IP.
# NOTE: Do not set `enabled` here. OVH's creation endpoint currently rejects that
# field with HTTP 400 ("Received not described parameters: (enabled)").
resource "ovh_ip_firewall" "vps" {
  ip             = local.ip_block
  ip_on_firewall = var.vps_ip
}

# ── Permit rules ──────────────────────────────────────────────────────────────

# Rule 0: Allow established TCP connections (return traffic for outbound sessions)
# The OVH stateless firewall uses tcp_option "established" to match ACK/RST flags.
resource "ovh_ip_firewall_rule" "allow_established" {
  ip             = local.ip_block
  ip_on_firewall = var.vps_ip
  sequence       = 0
  action         = "permit"
  protocol       = "tcp"
  tcp_option     = "established"
  depends_on     = [ovh_ip_firewall.vps]
}

# Rule 1: Allow inbound SSH on the non-standard port
resource "ovh_ip_firewall_rule" "allow_ssh" {
  ip               = local.ip_block
  ip_on_firewall   = var.vps_ip
  sequence         = 1
  action           = "permit"
  protocol         = "tcp"
  destination_port = tostring(var.ssh_port)
  depends_on       = [ovh_ip_firewall.vps]
}

# Rule 2: Allow HTTP (Caddy handles HTTP→HTTPS redirect)
resource "ovh_ip_firewall_rule" "allow_http" {
  ip               = local.ip_block
  ip_on_firewall   = var.vps_ip
  sequence         = 2
  action           = "permit"
  protocol         = "tcp"
  destination_port = "80"
  depends_on       = [ovh_ip_firewall.vps]
}

# Rule 3: Allow HTTPS (Caddy TLS termination)
resource "ovh_ip_firewall_rule" "allow_https" {
  ip               = local.ip_block
  ip_on_firewall   = var.vps_ip
  sequence         = 3
  action           = "permit"
  protocol         = "tcp"
  destination_port = "443"
  depends_on       = [ovh_ip_firewall.vps]
}

# Rule 4: Allow ICMP (ping — useful for health checks and diagnostics)
resource "ovh_ip_firewall_rule" "allow_icmp" {
  ip             = local.ip_block
  ip_on_firewall = var.vps_ip
  sequence       = 4
  action         = "permit"
  protocol       = "icmp"
  depends_on     = [ovh_ip_firewall.vps]
}

# ── Deny catch-all ────────────────────────────────────────────────────────────

# Rule 19: Deny all remaining IPv4 traffic (must be last — OVH max sequence is 19)
resource "ovh_ip_firewall_rule" "deny_all" {
  ip             = local.ip_block
  ip_on_firewall = var.vps_ip
  sequence       = 19
  action         = "deny"
  protocol       = "ipv4"
  depends_on     = [ovh_ip_firewall.vps]
}
