# CNAME record: slm.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
resource "cloudflare_dns_record" "slm" {
  zone_id = var.cloudflare_zone_id
  name    = "slm"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat_mac.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}
