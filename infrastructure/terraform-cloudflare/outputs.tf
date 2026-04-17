output "tunnel_id" {
  description = "Cloudflare Tunnel ID"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat.id
}

# tunnel_token is not exported by the v5 provider (creation-time secret only).
# After apply, retrieve it with:
#   curl -s "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/cfd_tunnel/$(terraform output -raw tunnel_id)/token" \
#        -H "Authorization: Bearer <API_TOKEN>" | jq -r '.result'

output "agent_cname" {
  description = "CNAME target for agent subdomain"
  value       = cloudflare_dns_record.agent.content
}

output "api_cname" {
  description = "CNAME target for api subdomain"
  value       = cloudflare_dns_record.api.content
}
