# Mac SLM Tunnel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Expose the MacBook's slm_server (port 8000, MLX inference) through a Cloudflare Tunnel (`slm.frenchforet.com`) so the VPS seshat-gateway can reach it when the `local` profile is selected in the PWA.

**Architecture:** A new Terraform module (`infrastructure/terraform-cloudflare-mac/`) provisions the Mac tunnel, DNS record, and a Cloudflare Access service token. The VPS gateway's LLM client injects CF-Access headers when posting to `slm.frenchforet.com`. A new `GET /api/inference/status` gateway endpoint probes the Mac tunnel and the PWA polls it to show a live availability indicator.

**Tech Stack:** Terraform (cloudflare provider ~> 5.0), cloudflared (Homebrew, launchd), Python/httpx (gateway), TypeScript/React (PWA).

**Spec:** `docs/superpowers/specs/2026-04-17-mac-slm-tunnel-design.md`

---

## File Map

| File | Action | Responsibility |
|---|---|---|
| `infrastructure/terraform-cloudflare-mac/providers.tf` | Create | Cloudflare provider config |
| `infrastructure/terraform-cloudflare-mac/variables.tf` | Create | Input variables |
| `infrastructure/terraform-cloudflare-mac/tunnel.tf` | Create | Tunnel resource + ingress config |
| `infrastructure/terraform-cloudflare-mac/dns.tf` | Create | CNAME for slm.frenchforet.com |
| `infrastructure/terraform-cloudflare-mac/access.tf` | Create | Access Application, Service Token, Policy |
| `infrastructure/terraform-cloudflare-mac/outputs.tf` | Create | Tunnel ID, CF access token outputs |
| `infrastructure/terraform-cloudflare-mac/terraform.tfvars.example` | Create | Credentials template |
| `infrastructure/terraform-cloudflare-mac/.gitignore` | Create | Ignore tfvars/state/provider cache |
| `src/personal_agent/config/settings.py` | Modify | Add `cf_access_client_id` + `cf_access_client_secret` to `AppConfig` |
| `src/personal_agent/llm_client/client.py` | Modify | Inject CF-Access headers when endpoint is `slm.frenchforet.com` |
| `src/personal_agent/service/app.py` | Modify | Add `GET /api/inference/status` endpoint |
| `config/models.cloud.yaml` | Modify | Override primary/sub_agent endpoints to `https://slm.frenchforet.com/v1` |
| `docker-compose.cloud.yml` | Modify | Inject `CF_ACCESS_CLIENT_ID` + `CF_ACCESS_CLIENT_SECRET` into seshat-gateway |
| `seshat-pwa/src/hooks/useInferenceStatus.ts` | Create | Poll `/api/inference/status`; return `up`/`down`/`unknown` + latency |
| `seshat-pwa/src/components/ProfileSelector.tsx` | Modify | Show availability dot + tooltip on Local card |
| `tests/test_config/test_settings.py` | Modify | Test CF fields read from env vars |
| `tests/test_llm_client/test_client.py` | Modify | Test CF headers injected in outbound request |
| `tests/test_service/test_inference_status.py` | Create | Test inference status endpoint |

---

## Task 1: Terraform Module — Mac Cloudflare Tunnel

**Files:**
- Create: `infrastructure/terraform-cloudflare-mac/providers.tf`
- Create: `infrastructure/terraform-cloudflare-mac/variables.tf`
- Create: `infrastructure/terraform-cloudflare-mac/tunnel.tf`
- Create: `infrastructure/terraform-cloudflare-mac/dns.tf`
- Create: `infrastructure/terraform-cloudflare-mac/access.tf`
- Create: `infrastructure/terraform-cloudflare-mac/outputs.tf`
- Create: `infrastructure/terraform-cloudflare-mac/terraform.tfvars.example`
- Create: `infrastructure/terraform-cloudflare-mac/.gitignore`

- [ ] **Step 1: Create providers.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/providers.tf
terraform {
  required_version = ">= 1.9"

  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
```

- [ ] **Step 2: Create variables.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/variables.tf
variable "cloudflare_api_token" {
  description = "Cloudflare API token with Zone:Edit, DNS:Edit, Cloudflare Tunnel:Edit, Access:Edit permissions"
  type        = string
  sensitive   = true
}

variable "cloudflare_zone_id" {
  description = "Cloudflare Zone ID for the target domain"
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare Account ID"
  type        = string
  sensitive   = true
}

variable "tunnel_name" {
  description = "Human-readable name for the Mac Cloudflare Tunnel"
  type        = string
  default     = "seshat-mac"
}

variable "domain" {
  description = "Root domain managed in this Cloudflare zone"
  type        = string
  default     = "frenchforet.com"
}
```

- [ ] **Step 3: Create tunnel.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/tunnel.tf
#
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
```

- [ ] **Step 4: Create dns.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/dns.tf
# CNAME record: slm.frenchforet.com → tunnel (proxied — TLS at Cloudflare edge)
resource "cloudflare_dns_record" "slm" {
  zone_id = var.cloudflare_zone_id
  name    = "slm"
  type    = "CNAME"
  content = "${cloudflare_zero_trust_tunnel_cloudflared.seshat_mac.id}.cfargotunnel.com"
  proxied = true
  ttl     = 1
}
```

- [ ] **Step 5: Create access.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/access.tf
#
# Cloudflare Zero Trust Access protection for slm.frenchforet.com.
# Only requests carrying the VPS gateway's service token are allowed through.
# All other requests → 403 at the Cloudflare edge (slm_server never sees them).

resource "cloudflare_zero_trust_access_application" "slm" {
  account_id       = var.cloudflare_account_id
  name             = "Mac SLM Server"
  domain           = "slm.${var.domain}"
  type             = "self_hosted"
  session_duration = "24h"
}

resource "cloudflare_zero_trust_access_service_token" "vps_gateway" {
  account_id = var.cloudflare_account_id
  name       = "VPS Gateway to Mac SLM"
}

resource "cloudflare_zero_trust_access_policy" "slm_allow_service_token" {
  account_id     = var.cloudflare_account_id
  application_id = cloudflare_zero_trust_access_application.slm.id
  name           = "Allow VPS gateway service token"
  precedence     = 1
  decision       = "allow"

  include = [
    {
      service_token = [cloudflare_zero_trust_access_service_token.vps_gateway.id]
    }
  ]
}
```

- [ ] **Step 6: Create outputs.tf**

```hcl
# infrastructure/terraform-cloudflare-mac/outputs.tf
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
```

- [ ] **Step 7: Create terraform.tfvars.example**

```hcl
# infrastructure/terraform-cloudflare-mac/terraform.tfvars.example
# Copy to terraform.tfvars and fill in real values.
# terraform.tfvars is gitignored — never commit real secrets.

cloudflare_api_token  = "YOUR_CLOUDFLARE_API_TOKEN"
cloudflare_zone_id    = "YOUR_ZONE_ID"
cloudflare_account_id = "YOUR_ACCOUNT_ID"
tunnel_name           = "seshat-mac"
domain                = "frenchforet.com"
```

- [ ] **Step 8: Create .gitignore**

```
*.tfvars
.terraform/
terraform.tfstate
terraform.tfstate.backup
```

- [ ] **Step 9: Validate the module**

```bash
cd infrastructure/terraform-cloudflare-mac
terraform init
terraform validate
```

Expected: `Success! The configuration is valid.`

If you see any resource attribute errors (the v5 provider occasionally renames attributes between minor versions), check the provider changelog at `.terraform/providers/registry.terraform.io/cloudflare/cloudflare/5.*/` and fix the attribute name.

- [ ] **Step 10: Commit**

```bash
git add infrastructure/terraform-cloudflare-mac/
git commit -m "feat(infra): add terraform-cloudflare-mac module for Mac SLM tunnel"
```

---

## Task 2: AppConfig — CF Access credential fields

**Files:**
- Modify: `src/personal_agent/config/settings.py`
- Test: `tests/test_config/test_settings.py`

- [ ] **Step 1: Write the failing tests**

Open `tests/test_config/test_settings.py` and add this class at the end of the file:

```python
class TestCFAccessSettings:
    """Test CF Access credential fields on AppConfig."""

    def test_cf_access_client_id_reads_from_env(self) -> None:
        """CF_ACCESS_CLIENT_ID env var is read into cf_access_client_id."""
        os.environ["CF_ACCESS_CLIENT_ID"] = "test-client-id"
        try:
            config = AppConfig()
            assert config.cf_access_client_id == "test-client-id"
        finally:
            del os.environ["CF_ACCESS_CLIENT_ID"]

    def test_cf_access_client_secret_reads_from_env(self) -> None:
        """CF_ACCESS_CLIENT_SECRET env var is read into cf_access_client_secret."""
        os.environ["CF_ACCESS_CLIENT_SECRET"] = "test-client-secret"
        try:
            config = AppConfig()
            assert config.cf_access_client_secret == "test-client-secret"
        finally:
            del os.environ["CF_ACCESS_CLIENT_SECRET"]

    def test_cf_access_fields_default_to_none(self) -> None:
        """CF access fields are None when env vars are not set."""
        os.environ.pop("CF_ACCESS_CLIENT_ID", None)
        os.environ.pop("CF_ACCESS_CLIENT_SECRET", None)
        config = AppConfig()
        assert config.cf_access_client_id is None
        assert config.cf_access_client_secret is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_config/test_settings.py::TestCFAccessSettings -v
```

Expected: `FAILED` — `AttributeError: 'AppConfig' object has no attribute 'cf_access_client_id'`

- [ ] **Step 3: Add the fields to AppConfig**

In `src/personal_agent/config/settings.py`, find the gateway settings block (around line 774, the `gateway_access_config` field) and add after it, before `_settings: AppConfig | None = None`:

```python
    # Cloudflare Access credentials (Mac SLM tunnel — see docs/superpowers/specs/2026-04-17-mac-slm-tunnel-design.md)
    cf_access_client_id: str | None = Field(
        default=None,
        alias="CF_ACCESS_CLIENT_ID",
        description=(
            "Cloudflare Zero Trust service token client ID for Mac SLM tunnel. "
            "Injected as CF-Access-Client-Id header on requests to slm.frenchforet.com."
        ),
    )
    cf_access_client_secret: str | None = Field(
        default=None,
        alias="CF_ACCESS_CLIENT_SECRET",
        description=(
            "Cloudflare Zero Trust service token secret for Mac SLM tunnel. "
            "Injected as CF-Access-Client-Secret header on requests to slm.frenchforet.com."
        ),
    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_config/test_settings.py::TestCFAccessSettings -v
```

Expected: `3 passed`

- [ ] **Step 5: Run type checking**

```bash
uv run mypy src/personal_agent/config/settings.py
```

Expected: `Success: no issues found`

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/config/settings.py tests/test_config/test_settings.py
git commit -m "feat(config): add CF_ACCESS_CLIENT_ID/SECRET fields to AppConfig"
```

---

## Task 3: LLM Client — CF header injection

**Files:**
- Modify: `src/personal_agent/llm_client/client.py`
- Test: `tests/test_llm_client/test_client.py`

- [ ] **Step 1: Write the failing tests**

Open `tests/test_llm_client/test_client.py`. Find the `TestLocalLLMClient` class and add these two test methods to it:

```python
    @pytest.mark.asyncio
    async def test_cf_access_headers_injected_for_slm_endpoint(
        self, mock_model_config: Path
    ) -> None:
        """CF-Access headers are injected when endpoint contains slm.frenchforet.com."""
        # Create client with slm.frenchforet.com endpoint
        config_file = mock_model_config.parent / "models_slm.yaml"
        config_file.write_text(
            """
models:
  primary:
    id: "test-primary"
    context_length: 32768
    max_concurrency: 1
    default_timeout: 60
    endpoint: "https://slm.frenchforet.com/v1"
  sub_agent:
    id: "test-sub"
    context_length: 32768
    max_concurrency: 1
    default_timeout: 60
"""
        )
        client = LocalLLMClient(
            base_url="https://slm.frenchforet.com/v1",
            timeout_seconds=30,
            max_retries=0,
            model_config_path=config_file,
        )

        captured_headers: dict[str, str] = {}

        async def mock_post(url: str, **kwargs: object) -> httpx.Response:
            captured_headers.update(kwargs.get("headers") or {})
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "model": "test-primary",
                },
            )

        with (
            patch("personal_agent.llm_client.client.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_async_client,
        ):
            mock_settings.cf_access_client_id = "test-id-123"
            mock_settings.cf_access_client_secret = "test-secret-456"
            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(side_effect=mock_post)
            mock_async_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
            )

        assert captured_headers.get("CF-Access-Client-Id") == "test-id-123"
        assert captured_headers.get("CF-Access-Client-Secret") == "test-secret-456"

    @pytest.mark.asyncio
    async def test_no_cf_headers_for_localhost_endpoint(
        self, client: LocalLLMClient
    ) -> None:
        """CF-Access headers are NOT added for localhost endpoints."""
        captured_headers: dict[str, str] = {}

        async def mock_post(url: str, **kwargs: object) -> httpx.Response:
            captured_headers.update(kwargs.get("headers") or {})
            return httpx.Response(
                200,
                json={
                    "id": "test",
                    "object": "chat.completion",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "hello"},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                    "model": "test-primary",
                },
            )

        with (
            patch("personal_agent.llm_client.client.settings") as mock_settings,
            patch("httpx.AsyncClient") as mock_async_client,
        ):
            mock_settings.cf_access_client_id = "test-id-123"
            mock_settings.cf_access_client_secret = "test-secret-456"
            mock_client_instance = AsyncMock()
            mock_client_instance.post = AsyncMock(side_effect=mock_post)
            mock_async_client.return_value.__aenter__ = AsyncMock(
                return_value=mock_client_instance
            )
            mock_async_client.return_value.__aexit__ = AsyncMock(return_value=None)

            await client.respond(
                role=ModelRole.PRIMARY,
                messages=[{"role": "user", "content": "hi"}],
            )

        assert "CF-Access-Client-Id" not in captured_headers
        assert "CF-Access-Client-Secret" not in captured_headers
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_llm_client/test_client.py::TestLocalLLMClient::test_cf_access_headers_injected_for_slm_endpoint tests/test_llm_client/test_client.py::TestLocalLLMClient::test_no_cf_headers_for_localhost_endpoint -v
```

Expected: `FAILED` — headers are not injected yet.

- [ ] **Step 3: Add CF header injection to `_do_request`**

In `src/personal_agent/llm_client/client.py`, find the httpx request block (around line 358–372). It looks like:

```python
                async with httpx.AsyncClient(timeout=timeout_config, verify=verify_ssl) as client:
                    response = await client.post(current_endpoint, json=payload)
```

Replace that block with:

```python
                # Inject Cloudflare Access headers for the Mac SLM tunnel.
                # Only added when the endpoint is slm.frenchforet.com and
                # credentials are configured — transparent for all other endpoints.
                cf_headers: dict[str, str] = {}
                if "slm.frenchforet.com" in current_endpoint:
                    if settings.cf_access_client_id and settings.cf_access_client_secret:
                        cf_headers["CF-Access-Client-Id"] = settings.cf_access_client_id
                        cf_headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret

                async with httpx.AsyncClient(timeout=timeout_config, verify=verify_ssl) as client:
                    response = await client.post(
                        current_endpoint, json=payload, headers=cf_headers or None
                    )
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_llm_client/test_client.py::TestLocalLLMClient::test_cf_access_headers_injected_for_slm_endpoint tests/test_llm_client/test_client.py::TestLocalLLMClient::test_no_cf_headers_for_localhost_endpoint -v
```

Expected: `2 passed`

- [ ] **Step 5: Run the full llm_client test suite to check for regressions**

```bash
uv run pytest tests/test_llm_client/ -v
```

Expected: all tests pass (same count as before).

- [ ] **Step 6: Run type checking**

```bash
uv run mypy src/personal_agent/llm_client/client.py
```

Expected: `Success: no issues found`

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/llm_client/client.py tests/test_llm_client/test_client.py
git commit -m "feat(llm-client): inject CF-Access headers for slm.frenchforet.com endpoint"
```

---

## Task 4: Gateway — `GET /api/inference/status` endpoint

**Files:**
- Modify: `src/personal_agent/service/app.py`
- Create: `tests/test_service/test_inference_status.py`

- [ ] **Step 1: Create the test file**

Create `tests/test_service/test_inference_status.py`:

```python
"""Tests for the inference_status endpoint function."""

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

# Import the endpoint function directly — same pattern as test_chat_hydration.py.
# This avoids spinning up the full FastAPI lifespan (DB, ES, etc.).
from personal_agent.service.app import inference_status


@pytest.mark.asyncio
async def test_status_up_when_health_returns_200() -> None:
    """Returns {"local": "up", "latency_ms": N} when slm_server /health returns 200."""
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.raise_for_status = MagicMock()

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = "test-id"
        mock_settings.cf_access_client_secret = "test-secret"
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "up"
    assert isinstance(result["latency_ms"], int)
    assert result["latency_ms"] >= 0


@pytest.mark.asyncio
async def test_status_down_on_connect_timeout() -> None:
    """Returns {"local": "down", "latency_ms": None} on ConnectTimeout."""
    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(
            side_effect=httpx.ConnectTimeout("timed out")
        )
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_502() -> None:
    """Returns {"local": "down"} when cloudflared returns 502 (slm_server not running)."""
    mock_response = MagicMock()
    mock_response.status_code = 502
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "502", request=MagicMock(), response=mock_response
        )
    )

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = None
        mock_settings.cf_access_client_secret = None
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    assert result["latency_ms"] is None


@pytest.mark.asyncio
async def test_status_down_on_403_logs_warning() -> None:
    """Returns {"local": "down"} and logs a warning when CF returns 403."""
    mock_response = MagicMock()
    mock_response.status_code = 403
    mock_response.raise_for_status = MagicMock(
        side_effect=httpx.HTTPStatusError(
            "403", request=MagicMock(), response=mock_response
        )
    )

    with (
        patch("personal_agent.service.app.settings") as mock_settings,
        patch("personal_agent.service.app.log") as mock_log,
        patch("personal_agent.service.app.httpx.AsyncClient") as mock_http,
    ):
        mock_settings.cf_access_client_id = "bad-id"
        mock_settings.cf_access_client_secret = "bad-secret"
        mock_http_instance = AsyncMock()
        mock_http_instance.get = AsyncMock(return_value=mock_response)
        mock_http.return_value.__aenter__ = AsyncMock(return_value=mock_http_instance)
        mock_http.return_value.__aexit__ = AsyncMock(return_value=None)

        result = await inference_status()

    assert result["local"] == "down"
    mock_log.warning.assert_called_once()
    call_args = mock_log.warning.call_args
    assert "inference_tunnel_auth_failed" in call_args[0]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_service/test_inference_status.py -v
```

Expected: `FAILED` — endpoint doesn't exist yet.

- [ ] **Step 3: Add the endpoint to app.py**

First, add `import httpx` and `import time` to the module-level imports in `src/personal_agent/service/app.py`. Find the existing imports block (around line 1–10) and add after the existing stdlib imports:

```python
import httpx
import time
```

Then add the following after line 1081 (after `return {"session_id": session_id, "status": "streaming"}`), before the Memory Endpoints section:

```python

# ============================================================================
# Inference Availability (Mac SLM Tunnel)
# ============================================================================

_SLM_HEALTH_URL = "https://slm.frenchforet.com/health"


@app.get("/api/inference/status")
async def inference_status() -> dict[str, Any]:
    """Probe the Mac SLM tunnel and return availability for the local profile.

    Makes a GET /health request to https://slm.frenchforet.com/health with
    Cloudflare Access service token headers. Times out in 3 seconds.

    Returns:
        {"local": "up", "latency_ms": N} if reachable, {"local": "down", "latency_ms": None} otherwise.
    """
    headers: dict[str, str] = {}
    if settings.cf_access_client_id and settings.cf_access_client_secret:
        headers["CF-Access-Client-Id"] = settings.cf_access_client_id
        headers["CF-Access-Client-Secret"] = settings.cf_access_client_secret

    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=3.0) as client:
            resp = await client.get(_SLM_HEALTH_URL, headers=headers)
            resp.raise_for_status()
        latency_ms = int((time.monotonic() - start) * 1000)
        return {"local": "up", "latency_ms": latency_ms}
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 403:
            log.warning(
                "inference_tunnel_auth_failed",
                status=403,
                hint="Rotate CF_ACCESS_CLIENT_ID/SECRET via terraform apply",
            )
        return {"local": "down", "latency_ms": None}
    except Exception:
        return {"local": "down", "latency_ms": None}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
uv run pytest tests/test_service/test_inference_status.py -v
```

Expected: `4 passed`

- [ ] **Step 5: Run type checking and linting**

```bash
uv run mypy src/personal_agent/service/app.py
uv run ruff check src/personal_agent/service/app.py
```

Expected: no errors. If ruff flags `import inside function`, add `# noqa: PLC0415` to the import lines.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/service/app.py tests/test_service/test_inference_status.py
git commit -m "feat(gateway): add GET /api/inference/status endpoint for Mac SLM tunnel probe"
```

---

## Task 5: Config file changes (VPS-side)

**Files:**
- Modify: `config/models.cloud.yaml`
- Modify: `docker-compose.cloud.yml`

- [ ] **Step 1: Update models.cloud.yaml**

In `config/models.cloud.yaml`, update the `primary` and `sub_agent` endpoint lines. Find:

```yaml
    endpoint: "http://localhost:8000/v1"
```

For `primary` (around line 67), change to:

```yaml
    endpoint: "https://slm.frenchforet.com/v1"
```

For `sub_agent` (around line 89), change to:

```yaml
    endpoint: "https://slm.frenchforet.com/v1"
```

Also update the header comment block at the top of the file to reflect the new override:

Find:
```yaml
# Differences from models.yaml:
#   - embedding endpoint → http://embeddings:8503/v1  (llama.cpp Docker service)
#   - reranker endpoint  → http://embeddings:8504/v1  (llama.cpp Docker service)
#   - primary/sub_agent endpoints unchanged (unused on cloud path — cloud APIs used instead)
```

Replace with:
```yaml
# Differences from models.yaml:
#   - primary/sub_agent endpoint → https://slm.frenchforet.com/v1  (Mac SLM tunnel)
#   - embedding endpoint → http://embeddings:8503/v1  (llama.cpp Docker service)
#   - reranker endpoint  → http://embeddings:8504/v1  (llama.cpp Docker service)
```

- [ ] **Step 2: Verify the YAML parses correctly**

```bash
uv run python -c "
from personal_agent.config.model_loader import load_model_config
from pathlib import Path
cfg = load_model_config(Path('config/models.cloud.yaml'))
print('primary endpoint:', cfg.models['primary'].endpoint)
print('sub_agent endpoint:', cfg.models['sub_agent'].endpoint)
print('embedding endpoint:', cfg.models['embedding'].endpoint)
assert cfg.models['primary'].endpoint == 'https://slm.frenchforet.com/v1'
assert cfg.models['sub_agent'].endpoint == 'https://slm.frenchforet.com/v1'
assert 'embeddings:8503' in cfg.models['embedding'].endpoint
print('OK')
"
```

Expected:
```
primary endpoint: https://slm.frenchforet.com/v1
sub_agent endpoint: https://slm.frenchforet.com/v1
embedding endpoint: http://embeddings:8503/v1
OK
```

- [ ] **Step 3: Update docker-compose.cloud.yml**

In `docker-compose.cloud.yml`, find the `seshat-gateway` service's `environment:` block (around line 220). After the existing `GATEWAY_TOKEN_EXTERNAL_AGENT` line, add:

```yaml
      # Cloudflare Access service token for Mac SLM tunnel (local inference profile)
      CF_ACCESS_CLIENT_ID: ${CF_ACCESS_CLIENT_ID}
      CF_ACCESS_CLIENT_SECRET: ${CF_ACCESS_CLIENT_SECRET}
```

- [ ] **Step 4: Verify docker-compose parses without error**

```bash
docker compose -f docker-compose.cloud.yml config --quiet
```

Expected: no output (valid YAML). If `CF_ACCESS_CLIENT_ID` is not set locally, docker compose may warn — that's fine for now (it won't be set until after `terraform apply`).

- [ ] **Step 5: Commit**

```bash
git add config/models.cloud.yaml docker-compose.cloud.yml
git commit -m "feat(config): route local profile LLM calls to slm.frenchforet.com tunnel"
```

---

## Task 6: PWA — inference availability indicator

**Files:**
- Create: `seshat-pwa/src/hooks/useInferenceStatus.ts`
- Modify: `seshat-pwa/src/components/ProfileSelector.tsx`

- [ ] **Step 1: Create `useInferenceStatus.ts`**

Create `seshat-pwa/src/hooks/useInferenceStatus.ts`:

```typescript
'use client';

import { useState, useEffect, useRef } from 'react';

import { SESHAT_API } from '@/lib/agui-client';

export type InferenceStatus = 'unknown' | 'up' | 'down';

export interface InferenceStatusResult {
  status: InferenceStatus;
  latencyMs: number | null;
}

/**
 * Poll GET /api/inference/status every 60 seconds while the local profile is active.
 *
 * Returns "unknown" until the first check completes, "up"/"down" thereafter.
 * Polling stops immediately when `active` becomes false.
 */
export function useInferenceStatus(active: boolean): InferenceStatusResult {
  const [result, setResult] = useState<InferenceStatusResult>({
    status: 'unknown',
    latencyMs: null,
  });
  const intervalRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (!active) {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
      setResult({ status: 'unknown', latencyMs: null });
      return;
    }

    const check = async () => {
      try {
        const resp = await fetch(`${SESHAT_API}/api/inference/status`);
        const data = (await resp.json()) as {
          local: 'up' | 'down';
          latency_ms: number | null;
        };
        setResult({ status: data.local, latencyMs: data.latency_ms });
      } catch {
        setResult({ status: 'down', latencyMs: null });
      }
    };

    check(); // immediate check on activation
    intervalRef.current = setInterval(check, 60_000);

    return () => {
      if (intervalRef.current !== null) {
        clearInterval(intervalRef.current);
        intervalRef.current = null;
      }
    };
  }, [active]);

  return result;
}
```

- [ ] **Step 2: Update `ProfileSelector.tsx`**

Replace the full contents of `seshat-pwa/src/components/ProfileSelector.tsx` with:

```typescript
'use client';

import type { ExecutionProfile } from '@/lib/types';
import { useInferenceStatus } from '@/hooks/useInferenceStatus';

interface ProfileSelectorProps {
  selected: ExecutionProfile;
  onSelect: (profile: ExecutionProfile) => void;
  disabled?: boolean;
}

interface ProfileOption {
  id: ExecutionProfile;
  label: string;
  model: string;
  description: string;
  cost: string;
}

const PROFILES: ProfileOption[] = [
  {
    id: 'local',
    label: 'Local',
    model: 'Qwen3.5-35B',
    description: 'Runs on your machine. Private, free, no internet required.',
    cost: 'Free',
  },
  {
    id: 'cloud',
    label: 'Cloud',
    model: 'Claude Sonnet',
    description: 'Faster and more capable. Requires backend cloud credentials.',
    cost: '$0.01–0.05 / msg',
  },
];

/**
 * Profile selector shown at the start of a new conversation.
 *
 * Shows a live availability dot on the Local card: green when the Mac tunnel
 * is reachable, grey when offline, dim while the first check is pending.
 */
export function ProfileSelector({
  selected,
  onSelect,
  disabled = false,
}: ProfileSelectorProps) {
  const inferenceStatus = useInferenceStatus(selected === 'local');

  return (
    <div className="flex flex-col gap-3">
      <p className="text-sm text-slate-400 text-center">
        Choose an execution profile for this conversation
      </p>
      <div className="grid grid-cols-2 gap-3">
        {PROFILES.map((profile) => {
          const isSelected = selected === profile.id;
          const isLocal = profile.id === 'local';

          // Status dot only shown on the Local card
          const statusDot = isLocal ? (
            <span
              className={`inline-block w-2 h-2 rounded-full ml-1 ${
                inferenceStatus.status === 'up'
                  ? 'bg-emerald-400'
                  : inferenceStatus.status === 'down'
                    ? 'bg-slate-500'
                    : 'bg-slate-600'
              }`}
              title={
                inferenceStatus.status === 'up'
                  ? `Mac inference online${inferenceStatus.latencyMs !== null ? ` (${inferenceStatus.latencyMs}ms)` : ''}`
                  : inferenceStatus.status === 'down'
                    ? 'Mac inference offline — start slm_server on your Mac'
                    : 'Checking Mac inference…'
              }
            />
          ) : null;

          return (
            <button
              key={profile.id}
              onClick={() => !disabled && onSelect(profile.id)}
              disabled={disabled}
              className={`
                flex flex-col items-start gap-1.5 p-4 rounded-xl border text-left
                transition-all duration-150 cursor-pointer
                ${
                  isSelected
                    ? 'border-blue-500 bg-blue-900/30 ring-1 ring-blue-500/50'
                    : 'border-slate-600 bg-slate-800/50 hover:border-slate-500 hover:bg-slate-800'
                }
                ${disabled ? 'opacity-60 cursor-not-allowed' : ''}
              `}
            >
              <div className="flex items-center gap-2 w-full">
                <span className="text-sm font-semibold text-slate-100">
                  {profile.label}
                </span>
                {statusDot}
                {isSelected && (
                  <span className="ml-auto text-xs text-blue-400 font-medium">
                    Selected
                  </span>
                )}
              </div>
              <span className="text-xs font-mono text-slate-400">
                {profile.model}
              </span>
              <p className="text-xs text-slate-500 leading-snug">
                {profile.description}
              </p>
              <span
                className={`text-xs font-medium mt-1 ${
                  profile.id === 'local' ? 'text-emerald-400' : 'text-amber-400'
                }`}
              >
                {profile.cost}
              </span>
            </button>
          );
        })}
      </div>
    </div>
  );
}
```

- [ ] **Step 3: TypeScript type check**

```bash
cd seshat-pwa && npx tsc --noEmit
```

Expected: no errors.

- [ ] **Step 4: Commit**

```bash
git add seshat-pwa/src/hooks/useInferenceStatus.ts seshat-pwa/src/components/ProfileSelector.tsx
git commit -m "feat(pwa): add local inference availability indicator to ProfileSelector"
```

---

## Task 7: Mac Setup — cloudflared launchd service

This task is run manually on the MacBook after `terraform apply`. It cannot be automated.

- [ ] **Step 1: Copy tfvars and apply**

```bash
cd infrastructure/terraform-cloudflare-mac
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — fill in cloudflare_api_token, zone_id, account_id
# (same values as infrastructure/terraform-cloudflare/terraform.tfvars)
terraform apply
```

Expected: Terraform creates tunnel, DNS record, Access Application, Service Token, and Policy. Review the plan before typing `yes`.

- [ ] **Step 2: Retrieve the tunnel token**

The tunnel token is NOT in Terraform outputs (provider limitation). Get it via the Cloudflare API:

```bash
ACCOUNT_ID=$(terraform output -raw cloudflare_account_id 2>/dev/null || echo "YOUR_ACCOUNT_ID")
TUNNEL_ID=$(terraform output -raw tunnel_id)
API_TOKEN="YOUR_CLOUDFLARE_API_TOKEN"

curl -s "https://api.cloudflare.com/client/v4/accounts/${ACCOUNT_ID}/cfd_tunnel/${TUNNEL_ID}/token" \
     -H "Authorization: Bearer ${API_TOKEN}" | jq -r '.result'
```

Copy the token — you will need it in the next step.

- [ ] **Step 3: Retrieve the CF Access credentials**

```bash
terraform output -raw cf_access_client_id
terraform output -raw cf_access_client_secret
```

Copy both values. Add them to the VPS `.env` file:

```bash
CF_ACCESS_CLIENT_ID=<value from output>
CF_ACCESS_CLIENT_SECRET=<value from output>
```

- [ ] **Step 4: Install cloudflared on the Mac**

```bash
brew install cloudflared
sudo cloudflared service install <TUNNEL_TOKEN_FROM_STEP_2>
```

Expected:
```
INFO Using Systemd
INFO cloudflared service for agent is installed
```

(macOS uses launchd, not systemd, but the message is similar.)

Verify the service is running:

```bash
sudo launchctl list | grep cloudflared
```

Expected: a process entry with exit status `0`.

- [ ] **Step 5: Verify the tunnel reaches slm_server**

Make sure slm_server is running (`cd slm_server && ./start.sh`), then:

```bash
curl -H "CF-Access-Client-Id: <client_id>" \
     -H "CF-Access-Client-Secret: <client_secret>" \
     https://slm.frenchforet.com/health
```

Expected: `{"status": "ok"}` (or similar health response from slm_server).

- [ ] **Step 6: Verify the gateway can reach it from the VPS**

SSH to the VPS and run:

```bash
curl -H "CF-Access-Client-Id: <CF_ACCESS_CLIENT_ID>" \
     -H "CF-Access-Client-Secret: <CF_ACCESS_CLIENT_SECRET>" \
     https://slm.frenchforet.com/health
```

Expected: `{"status": "ok"}`

- [ ] **Step 7: Restart the VPS gateway and verify inference status endpoint**

```bash
# On VPS: restart the gateway container to pick up the new env vars
docker compose -f docker-compose.cloud.yml restart seshat-gateway

# Then test the inference status endpoint
curl https://api.frenchforet.com/api/inference/status \
     -H "Authorization: Bearer <GATEWAY_TOKEN_EXTERNAL_AGENT>"
```

Expected: `{"local": "up", "latency_ms": <N>}`

- [ ] **Step 8: End-to-end smoke test via PWA**

1. Open `https://agent.frenchforet.com` in a browser.
2. Verify the Local card shows a **green dot** (Mac inference online).
3. Select Local and send a message.
4. Verify the response comes from the Mac's slm_server (check `docker logs cloud-sim-seshat-gateway` — should show requests to `slm.frenchforet.com`).

- [ ] **Step 9: Final commit (update VPS .env.example if it exists)**

If there is a `.env.example` or `.env.cloud.example` file on the VPS, add the new variables:

```bash
# Add to VPS .env.example (if it exists):
CF_ACCESS_CLIENT_ID=your-cf-access-client-id
CF_ACCESS_CLIENT_SECRET=your-cf-access-client-secret
```

```bash
git add .  # only if .env.example was changed
git commit -m "docs: add CF_ACCESS_CLIENT_ID/SECRET to env example for Mac SLM tunnel"
```

---

## Full Test Suite

Run once before opening the PR to ensure no regressions:

```bash
uv run pytest tests/test_config/test_settings.py tests/test_llm_client/ tests/test_service/test_inference_status.py -v
```

Expected: all tests pass.

```bash
uv run mypy src/
uv run ruff check src/
```

Expected: no errors.
