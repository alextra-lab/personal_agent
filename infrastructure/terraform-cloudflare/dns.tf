# CNAME record: agent.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
#
# ttl = 1 means "automatic" when proxied = true. Cloudflare ignores the TTL
# for proxied records but the API requires it to be set.
resource "cloudflare_dns_record" "agent" {
  zone_id = var.cloudflare_zone_id
  name    = "agent"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}

# CNAME record: api.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
resource "cloudflare_dns_record" "api" {
  zone_id = var.cloudflare_zone_id
  name    = "api"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}
