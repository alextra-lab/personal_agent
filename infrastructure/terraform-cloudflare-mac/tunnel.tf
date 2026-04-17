# Named Cloudflare Tunnel — remotely managed config (ingress rules via API)
# cloudflared runs on the Mac as a launchd system daemon.
# Ingress target is localhost:8000 (Mac's own loopback — no firewall needed).
resource "cloudflare_zero_trust_tunnel_cloudflared" "seshat_mac" {
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name
  config_src = "cloudflare"
}

resource "cloudflare_zero_trust_tunnel_cloudflared_config" "seshat_mac" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.seshat_mac.id

  config = {
    ingress = [
      {
        hostname = "slm.${var.domain}"
        service  = "http://localhost:8000"
      },
      # Required catch-all — must be last, no hostname
      {
        service = "http_status:404"
      },
    ]
  }
}
