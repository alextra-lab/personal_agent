# FRE-227 Security Audit + Hardening — Artifact Substrate Auth Model

**Date**: 2026-05-17
**Trigger**: During FRE-227 smoke testing, the gateway `users` table received a row for `starry-plaza-1s@icloud.com` (a Linear-tied iCloud Hide-My-Email relay) at `2026-05-17 04:45:18 UTC`. That email is **NOT** on the Cloudflare Access policy include list (live terraform state contains only `lextra@gmail.com`, `petizon_laurent@yahoo.fr`, `sceilidh@gmail.com`, `erika.tarazona@proton.me`). The user explicitly never authorized that email anywhere. We must reconcile how an off-allowlist email reached the Worker → gateway path before resuming FRE-227 verification.

---

## Context — What we know

| Layer | State |
|---|---|
| Cloudflare Access policy `personal_only` | Include list = 4 family emails. **`starry-plaza-1s@icloud.com` is NOT in it.** |
| Git history `personal_agent_secrets/.../*.tf` | No occurrence of `starry-plaza` or `@icloud` ever |
| Personal Agent `users` table | New row at 04:45:18 for `starry-plaza-1s@icloud.com` |
| Gateway log at 04:45:18 | `artifact_resolve_not_found user_id=7420fdef-...` (single request from tunnel ingress 172.25.0.1) |
| Worker code (private repo `worker/artifacts.js`) | Reads `Cf-Access-Authenticated-User-Email` from incoming request header; forwards to gateway as `X-Authenticated-User-Email` |
| Gateway code `service/artifacts_router.py:84` | Trusts `X-Authenticated-User-Email` after constant-time matching `X-Internal-Token` — performs **no JWT verification** |
| `cf_access_team_domain`, `cf_access_aud` settings | Defined in `config/settings.py:1019-1033` but **no verification code references them anywhere in `src/`** |

## What this means

For an off-allowlist email to land in `Cf-Access-Authenticated-User-Email` on the Worker side, **one of these must be true**:

1. **The Worker was reached via a path that bypasses CF Access**, with the header user-controlled. The two known bypass routes are:
   - `<script>.<account>.workers.dev` default URL (always provisioned by CF unless `workers_dev = false`)
   - Direct invocation via CF API / wrangler tail with crafted headers
2. **A second CF Access policy or service token grants access more broadly** than the `personal_only` policy visible in terraform state
3. **A tool the user ran (laptop Claude, wrangler, curl) on 2026-05-17 ~04:45 UTC made a test call** to the Worker with a forged email header — most likely from FRE-371 smoke-testing context where laptop Claude had `starry-plaza-1s@icloud.com` available from the Linear MCP response

**The Worker as currently coded trusts whatever the request header says.** The gateway as currently coded trusts whatever the Worker forwards (auth'd by shared secret). Neither layer cryptographically verifies that the email originated from a CF Access JWT.

## Security implications (regardless of origin of this specific request)

- **Worker spoofing**: anyone who can reach `artifacts-substrate.<account>.workers.dev/<uuid>` can supply an arbitrary email header. They can enumerate artifact metadata (r2_key, content_type, size_bytes, created_at) for any artifact whose owner email they can guess. They cannot read bytes directly (no R2 binding in their session), but the metadata leak is enough to confirm an artifact's existence.
- **Gateway spoofing**: anyone with the `X-Internal-Token` value who can reach `api.frenchforet.com/internal/artifacts/{id}` over the tunnel can spoof any email. The token's secrecy is the only thing between them and the same enumeration capability.
- **`users` table inflation**: every fresh email arriving at the gateway creates a new row idempotently. No rate-limiting on `get_or_create_user_by_email`. An attacker with the workers.dev URL can fill the `users` table with arbitrary email addresses, denial-of-service through bloat.

## Plan — three phases, executed in order, with a stop after Phase A

### Phase A — Determine origin of the 04:45:18 request (read-only, today)

This decides whether we're investigating an incident or a self-test. Run on **laptop** (where wrangler is authenticated):

1. **Confirm workers.dev URL state** — if `workers_dev` isn't explicitly `false`, the workers.dev URL is live:
   ```bash
   cd ~/Dev/personal_agent_secrets/infrastructure/terraform-cloudflare
   terraform state show cloudflare_workers_script.artifacts_substrate \
     | grep -i workers_dev
   # If `workers_dev = false` is not present, the URL is enabled.
   ```
2. **Tail Worker logs covering 04:45:18 UTC**:
   ```bash
   wrangler tail artifacts-substrate --format=json \
     --since '2026-05-17T04:40:00Z' --until '2026-05-17T04:50:00Z' \
     2>&1 | tee /tmp/wrangler-tail.json
   ```
   Inspect the `Cf-Connecting-IP`, `cf.colo`, and incoming `Cf-Access-Authenticated-User-Email` header for the 04:45:18 hit. If `Cf-Connecting-IP` matches the user's iPad IP and the URL was `artifacts.frenchforet.com`, we have a CF Access bypass to investigate (option 2 above). If `Cf-Connecting-IP` is a Cloudflare internal IP or the URL was the workers.dev variant, it was a non-Access path.
3. **Check Cloudflare audit log** for any policy edits / service tokens issued on 2026-05-16 / 17:
   ```bash
   # Cloudflare dashboard: My Profile → Audit Log → filter by date
   ```
4. **Confirm there's no second Access app or policy** routing the same hostname:
   ```bash
   terraform state list | grep -i 'access_application\|access_policy'
   ```

Phase A ends here. **Stop and report findings before Phase B.** Hardening should be informed by what's actually exploitable, not what's theoretical.

### Phase B — Harden the perimeter (post-Phase A)

Two hardenings, both required regardless of Phase A's outcome — they close the spoofing class of vulnerabilities permanently. Both ship in one PR.

**B1. Disable workers.dev URL on the artifacts Worker** (laptop terraform, ~5 min)
- Add `workers_dev = false` to `cloudflare_workers_script.artifacts_substrate`. The Worker becomes reachable only via `artifacts.frenchforet.com` (Access-gated) and via authenticated CF API calls from the account owner.

**B2. Verify CF Access JWT in the Worker before trusting the email** (Worker JS, ~30 min)
- Add JWT validation in `worker/artifacts.js`:
  - Read `Cf-Access-Jwt-Assertion` from incoming request.
  - Fetch JWKS from `https://<team>.cloudflareaccess.com/cdn-cgi/access/certs` (cached at module scope; refresh every 1h).
  - Verify signature + `aud` claim matches the app's audience tag (bind via env var `ACCESS_AUD`).
  - Extract `email` from the validated JWT claims; **discard** the `Cf-Access-Authenticated-User-Email` header.
  - If JWT verification fails → 404 (existence-hiding, per ADR-0064 D3).
- Use the `jose` library (CF Workers compatible) or a hand-rolled `crypto.subtle` verification. Implementation choice goes in the laptop ticket since it's Worker code in the private repo.

**B3. Verify CF Access JWT in the gateway's `/internal/artifacts/{id}` endpoint too** (Python, ~45 min) — defense-in-depth
- Add an httpx-based JWKS client in `service/artifacts_router.py` (cached singleton, refresh on signing-key miss).
- Use the `cf_access_team_domain` + `cf_access_aud` settings already defined at `config/settings.py:1019-1033`.
- Verify `X-Cf-Access-Jwt-Assertion` (forwarded by the Worker) instead of trusting `X-Authenticated-User-Email`.
- Extract `email` from validated claims.
- Keep the `X-Internal-Token` shared secret as a first-gate filter (defense-in-depth, not the sole auth).
- New unit tests in `tests/personal_agent/service/test_artifacts_router.py`: 401 on missing/bad JWT, 401 on `aud` mismatch, 200 on valid JWT.

**Pyjwt** is already in the project transitive deps via `litellm` — confirmed:
```bash
uv run python -c "import jwt; print(jwt.__version__)"
```

The same hardening should be ported to other inbound paths that consume `Cf-Access-Authenticated-User-Email` (e.g., `service/auth.py:get_or_create_user_by_email` is called from the SSE endpoint). **Out of scope** for this plan — a separate Wave E ticket (`Identity hardening: cryptographic CF Access JWT verification across all entry points`) tracks that.

### Phase C — Cleanup (after Phase B merges and deploys)

- Delete the spurious `users` row:
  ```sql
  DELETE FROM users WHERE email = 'starry-plaza-1s@icloud.com';
  ```
  Safe: it owns no artifacts (`SELECT count(*) FROM artifacts WHERE user_id = '7420fdef-32a4-435a-b37b-0b13cf30b290'` returns 0) and no sessions (same check on `sessions`).
- Re-run the FRE-227 smoke test from iPad. The artifact URL must succeed (Alex's CF Access JWT validates to `lextra@gmail.com` → resolves to user_id `1f7cc4bc-...` → matches artifact owner → 200 + bytes).

### Phase D — Document

- Amend ADR-0069 with a new section `## Update 2026-05-17 — Auth model hardening`. Note that the substrate now requires both X-Internal-Token AND a valid CF Access JWT on the gateway path, and the workers.dev URL is disabled.
- Amend ADR-0064 if needed — clarify that JWT verification is required end-to-end, not just at the Access edge.

---

## Critical Files

### Read in Phase A (laptop)
- `~/Dev/personal_agent_secrets/infrastructure/terraform-cloudflare/*.tf` (Worker, policy, app definitions)
- Cloudflare dashboard audit log

### Modified in Phase B
- `worker/artifacts.js` (private repo) — JWT validation
- `personal_agent_secrets/.../*.tf` — `workers_dev = false` on artifacts Worker
- `src/personal_agent/service/artifacts_router.py` — JWT verification, drop trust of `X-Authenticated-User-Email` alone
- `tests/personal_agent/service/test_artifacts_router.py` — new auth tests

### Reused
- `cf_access_team_domain` / `cf_access_aud` settings at `config/settings.py:1019-1033`
- `pyjwt` (transitive dep)

---

## Verification (end-to-end)

After Phase B deployment:

1. **Negative: workers.dev probe rejected** — `curl https://artifacts-substrate.<account>.workers.dev/<uuid>` → should return 404 or 530, not the artifact. (Will fail to resolve DNS if `workers_dev = false`.)
2. **Negative: forged email header on the public route** — `curl -H "Cf-Access-Authenticated-User-Email: attacker@example.com" https://artifacts.frenchforet.com/<uuid>` (without CF Access cookie) → CF Access intercepts before the Worker; redirect to login; no leak.
3. **Negative: gateway internal endpoint with shared token but no JWT** — `curl -H "X-Internal-Token: <token>" -H "X-Authenticated-User-Email: attacker@example.com" http://localhost:9001/internal/artifacts/<uuid>` → should return 401 (was 404 previously, will now hard-fail on JWT verification).
4. **Positive: legitimate flow from iPad** — open `https://artifacts.frenchforet.com/94c09610-...` after CF Access OTP with `lextra@gmail.com` → 200 + markdown bytes.
5. **Verify no off-allowlist emails reach `users` table** — `SELECT email FROM users` should only contain seeded family members and any genuine CF Access auths from the four allowlisted emails.

---

## Open question to resolve in Phase A

If wrangler tail shows the offending request **came from the user's iPad against `artifacts.frenchforet.com`** (not the workers.dev URL), then **CF Access itself authenticated the off-allowlist email**, which would be a much more serious finding — it would mean the policy isn't being enforced as defined. In that case, Phase B alone is insufficient; we'd need to file a CF support ticket.

The most likely Phase A outcome is that the request came from a laptop-Claude test call with the email header set from the Linear MCP `createdBy` value — a self-inflicted artifact of the implementation process, not an external attack. **But we should not assume that without evidence.**
