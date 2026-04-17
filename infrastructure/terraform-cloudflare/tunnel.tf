# Named Cloudflare Tunnel — remotely managed config (ingress rules via API)
#
# config_src = "cloudflare" means ingress routing is managed via the
# cloudflare_zero_trust_tunnel_cloudflared_config resource below, not a
# local config.yml file. cloudflared fetches config from Cloudflare's API
# at startup using the tunnel_token.
resource "cloudflare_zero_trust_tunnel_cloudflared" "seshat" {
  account_id = var.cloudflare_account_id
  name       = var.tunnel_name
  config_src = "cloudflare"
}

# Ingress routing — maps public hostnames to internal Docker services.
#
# cloudflared is on the cloud-sim Docker network and resolves service
# hostnames via Docker's internal DNS. Traffic arrives at Caddy with the
# original Host header preserved; Caddy matches the appropriate site block.
#
# Catch-all rule (no hostname) is required by the API — returns 404 for
# any hostname not explicitly listed.
resource "cloudflare_zero_trust_tunnel_cloudflared_config" "seshat" {
  account_id = var.cloudflare_account_id
  tunnel_id  = cloudflare_zero_trust_tunnel_cloudflared.seshat.id

  config = {
    ingress = [
      {
        hostname = "agent.${var.domain}"
        service  = "http://caddy:80"
      },
      {
        hostname = "api.${var.domain}"
        service  = "http://caddy:80"
      },
      # Required catch-all — must be last, no hostname
      {
        service = "http_status:404"
      },
    ]
  }
}
