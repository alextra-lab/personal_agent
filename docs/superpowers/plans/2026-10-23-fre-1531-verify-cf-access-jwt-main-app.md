# FRE-1531 — Verify the Cloudflare Access JWT on the main API path

Ticket: FRE-1531 (ADR-0153 D11). Backing ADRs: ADR-0153 D11, ADR-0064 D1/D4, ADR-0069 Dev-2/Dev-3.
Class: Standard (`src/` security logic). One phase, one PR.

## Scope

When `gateway_auth_enabled=True`, identity comes from the verified `email` claim of
`Cf-Access-Jwt-Assertion`. The plaintext `Cf-Access-Authenticated-User-Email` header never resolves a
user in that mode. The main app and the artifact Worker are separate Access applications, so the main
API uses a second audience and a second verifier. The Worker path (`resolve_artifact`) is unchanged.

## Step 1 result (JWT reaches the gateway) — evidence, 2026-10-23

- Caddy access log, tunnel block, last 3000 lines: `Cf-Access-Jwt-Assertion` is present on real owner
  requests (`/api/v1/sessions/*` 200, `/api/ws-ticket` 200, `/ws/*` 101). Caddy `reverse_proxy` forwards
  request headers unchanged. Key presence only was checked, no token was printed.
- Decoded claims of one live token (no signature check): `aud` is a one-element **list**, `iss` is the
  team domain, `type` is `app`, `email` is present, lifetime 30 days. PyJWT accepts a list `aud` that
  contains the configured audience.
- The main-app `aud` differs from the Worker `CF_ACCESS_AUD` in `/opt/seshat/.env` (boolean compare).
  Two settings are required, as D11 states.

## Design

- `config/settings.py`: new `cf_access_main_aud` (alias `CF_ACCESS_MAIN_AUD`) next to `cf_access_aud`.
  The team domain is shared (`cf_access_team_domain`).
- `service/cf_access_jwt.py`: new `get_main_app_verifier()` with its own singleton.
  `reset_verifier_for_testing()` resets both. `get_verifier()` is untouched.
- `service/auth.py`: new `resolve_request_email(request, *, owner_fallback: bool = True) -> str`.
  - `gateway_auth_enabled=True`: main-app verifier is `None` → 503. No JWT header → 401. Verify fails →
    401 (opaque). Else return the claim email. The plaintext header is not read.
  - `gateway_auth_enabled=False` (dev, unchanged): header, else owner email if `owner_fallback`, else 401.
  - `get_request_user` calls it, and logs `identity_source`.
  - One info event `request_user_jwt_identity_first_verified` per process, with `user_id` only (AC-7 proof;
    the per-request line is debug and not emitted in production).
- Fold-in: `session_api`, `feedback_api` and `knowledge_api` read the plaintext header directly. They are
  the same hole, in the same `/api/*` path. They call `resolve_request_email(request, owner_fallback=False)`.
  Their dev behaviour (header or 401, no owner fallback) is unchanged.

## Steps

1. Tests first — `tests/test_service/test_auth_jwt.py` (new). Mount `artifacts_router` in a `FastAPI` app
   with `gateway_auth_enabled=True`, a real RSA keypair, and the JWKS fetch stubbed (pattern:
   `tests/personal_agent/service/test_cf_access_jwt.py`). Stub only the DB layer.
   - AC-1 email header, no JWT → `GET /api/v1/artifacts/{id}` 401.
   - AC-2 JWT signed by a foreign key → 401.
   - AC-3 Worker-audience JWT → 401 on the main API. Main-app JWT → rejected (401) on
     `GET /internal/artifacts/{id}`.
   - AC-4 valid main JWT → user resolved from the claim. Conflicting header → claim wins.
   - AC-5 `cf_access_main_aud` unset → 503. `cf_access_team_domain` unset → 503. Header + any JWT does
     not fall back.
   - AC-6 `gateway_auth_enabled=False`, no headers → owner.
   - Also: expired JWT 401; list `aud` accepted; one test per sibling module (header only → 401 when
     enabled; valid JWT → 200 path resolves).
   - Run: `uv run pytest tests/test_service/test_auth_jwt.py -q` → expect FAIL (no implementation).
2. Add the setting and `get_main_app_verifier()`.
3. Add `resolve_request_email()`; rewire `get_request_user`.
4. Rewire the three gateway modules.
5. Update existing tests: `tests/test_service/test_auth.py` (header test sets auth disabled), the three
   gateway test files (auth disabled by fixture, so their header-only tests stay valid).
6. Docs: `.env.example` (`CF_ACCESS_MAIN_AUD`), `docker-compose.cloud.yml` comment if the gateway env needs
   it (env_file already imports `.env`), ADR-0064 update note, ADR-0153 D11 status line.
7. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
8. Commit, then `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Deploy gotcha (master)

After deploy, a production gateway without `CF_ACCESS_MAIN_AUD` returns 503 on every `get_request_user`
route and on the sessions, feedback and knowledge APIs. Master must add `CF_ACCESS_MAIN_AUD` to
`/opt/seshat/.env` in the same command chain as the gateway rebuild, then read the deployed setting.
AC-7 check: after one owner PWA turn, the gateway log shows `request_user_jwt_identity_first_verified`.

Codex plan review (2026-10-23): no header-trusting gaps, no PWA-breakage findings. Accepted: AC-5
team-domain case, AC-7 verification step. Declined: `/api/ws-ticket` and `/chat/stream` smoke tests (same
`Depends(get_request_user)` that the new suite covers). Kept: the once-per-process event, because the
per-request line is debug level and is not emitted in production.
