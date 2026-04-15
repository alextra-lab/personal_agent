output "firewall_enabled" {
  description = "Whether the OVH network firewall is active"
  value       = ovh_ip_firewall.vps.enabled
}

output "firewall_rule_sequences" {
  description = "Ordered list of configured rule sequences"
  value = [
    ovh_ip_firewall_rule.allow_established.sequence,
    ovh_ip_firewall_rule.allow_ssh.sequence,
    ovh_ip_firewall_rule.allow_http.sequence,
    ovh_ip_firewall_rule.allow_https.sequence,
    ovh_ip_firewall_rule.allow_icmp.sequence,
    ovh_ip_firewall_rule.deny_all.sequence,
  ]
}
