# Cloudflare Zero Trust Access protection for slm.frenchforet.com.
# Only requests carrying the VPS gateway's service token are allowed through.
# All other requests → 403 at the Cloudflare edge (slm_server never sees them).
#
# v5 provider: policy is standalone (no application_id/precedence on policy resource);
# linkage is via the `policies` attribute on the application.

resource "cloudflare_zero_trust_access_service_token" "vps_gateway" {
  account_id = var.cloudflare_account_id
  name       = "VPS Gateway to Mac SLM"
}

resource "cloudflare_zero_trust_access_policy" "slm_allow_service_token" {
  account_id = var.cloudflare_account_id
  name       = "Allow VPS gateway service token"
  decision   = "allow"

  include = [
    {
      service_token = {
        token_id = cloudflare_zero_trust_access_service_token.vps_gateway.id
      }
    }
  ]
}

resource "cloudflare_zero_trust_access_application" "slm" {
  account_id       = var.cloudflare_account_id
  name             = "Mac SLM Server"
  domain           = "slm.${var.domain}"
  type = "self_hosted"

  policies = [
    {
      id         = cloudflare_zero_trust_access_policy.slm_allow_service_token.id
      precedence = 1
    }
  ]
}
