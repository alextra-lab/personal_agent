output "tunnel_id" {
  description = "Cloudflare Tunnel ID for Mac slm_server"
  value       = cloudflare_zero_trust_tunnel_cloudflared.seshat_mac.id
}

# tunnel_token is not exported by the v5 provider (creation-time secret only).
# After apply, retrieve it with:
#   curl -s "https://api.cloudflare.com/client/v4/accounts/<ACCOUNT_ID>/cfd_tunnel/$(terraform output -raw tunnel_id)/token" \
#        -H "Authorization: Bearer <API_TOKEN>" | jq -r '.result'

output "slm_cname" {
  description = "CNAME target for slm subdomain"
  value       = cloudflare_dns_record.slm.content
}

output "cf_access_client_id" {
  description = "CF Access service token client ID — add to VPS .env as CF_ACCESS_CLIENT_ID"
  value       = cloudflare_zero_trust_access_service_token.vps_gateway.client_id
  sensitive   = true
}

output "cf_access_client_secret" {
  description = "CF Access service token client secret — add to VPS .env as CF_ACCESS_CLIENT_SECRET"
  value       = cloudflare_zero_trust_access_service_token.vps_gateway.client_secret
  sensitive   = true
}
