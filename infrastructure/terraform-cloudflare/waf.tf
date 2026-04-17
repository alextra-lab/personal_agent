# WAF custom ruleset — block api.frenchforet.com requests without Authorization.
#
# This is the primary security layer for the API subdomain. External agents
# MUST send an Authorization header; requests without it are blocked at the
# Cloudflare edge before reaching the VPS.
#
# Note: the WAF checks header *presence*, not token validity. Full token
# validation requires AGENT_GATEWAY_AUTH_ENABLED=true — tracked separately
# (requires PWA Bearer token support + EventSource auth solution).
#
# Header names in Cloudflare's expression engine are always lowercase.
resource "cloudflare_ruleset" "api_auth_check" {
  zone_id     = var.cloudflare_zone_id
  name        = "Require Authorization on API subdomain"
  description = "Block requests to api.frenchforet.com missing Authorization header"
  kind        = "zone"
  phase       = "http_request_firewall_custom"

  rules {
    action      = "block"
    expression  = "(http.host eq \"api.${var.domain}\" and not any(http.request.headers.names[*] eq \"authorization\"))"
    description = "Block API requests without Authorization header"
    enabled     = true
  }
}
