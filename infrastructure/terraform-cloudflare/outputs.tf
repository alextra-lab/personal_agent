output "tunnel_id" {
  description = "Cloudflare Tunnel ID"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat.id
}

output "tunnel_token" {
  description = "Cloudflare Tunnel token — set as CLOUDFLARE_TUNNEL_TOKEN in VPS .env then restart cloudflared"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat.token
  sensitive   = true
}

output "agent_cname" {
  description = "CNAME target for agent subdomain"
  value       = cloudflare_dns_record.agent.content
}

output "api_cname" {
  description = "CNAME target for api subdomain"
  value       = cloudflare_dns_record.api.content
}
