# FRE-1663 — Grafana verifies the Cloudflare Access JWT (observe app)

Ticket: FRE-1663 (Approved 2026-10-24). Sibling: FRE-1531 (gateway JWT check, ADR-0153 D11).
Class: Standard (access control on a tunnel-exposed origin). Codex plan-review required.

## Scope

The `observe` tunnel route goes straight to `http://grafana:3000` and skips Caddy. Grafana runs
anonymous Viewer, so any request that reaches the container reads every dashboard. Grafana must
verify that Cloudflare Access issued the request.

In scope: the `grafana` service of `docker-compose.cloud.yml`, its tests, `.env.example`, one doc
row. Out of scope: `docker-compose.yml` (local dev stack stays anonymous), Caddy, Neo4j, ES, OTLP.

## Design

`docker-compose.cloud.yml`, grafana `environment`:

| Setting | Value |
|---|---|
| `GF_AUTH_ANONYMOUS_ENABLED` | `false` (explicit; replaces `true`) |
| `GF_AUTH_ANONYMOUS_ORG_ROLE` | removed |
| `GF_AUTH_JWT_ENABLED` | `true` |
| `GF_AUTH_JWT_HEADER_NAME` | `Cf-Access-Jwt-Assertion` |
| `GF_AUTH_JWT_JWK_SET_URL` | `https://${CF_ACCESS_TEAM_DOMAIN:?...}/cdn-cgi/access/certs` |
| `GF_AUTH_JWT_EXPECT_CLAIMS` | `{"aud":"${GRAFANA_CF_ACCESS_AUD:?...}"}` |
| `GF_AUTH_JWT_EMAIL_CLAIM` / `GF_AUTH_JWT_USERNAME_CLAIM` | `email` |
| `GF_AUTH_JWT_AUTO_SIGN_UP` | `true` (new users get the default org role, Viewer) |
| `GF_AUTH_BASIC_ENABLED` | `true` (unchanged — admin basic auth on the loopback port) |
| `GF_AUTH_DISABLE_LOGIN_FORM` | `true` (unchanged) |

Both new variables use the `${VAR:?}` guard, like the sibling secrets. An unset audience then
stops compose at render time. It never starts Grafana with an empty `aud` expectation.
`GRAFANA_CF_ACCESS_AUD` is already in `/opt/seshat/.env` (master, ticket comment 2026-10-24).
The grafana service has no `env_file`, so the compose file passes both keys explicitly.

Why no `iss` claim: the key set comes from the team certs URL, so only tokens signed by this
team verify. The `aud` check separates the apps of one team.

## Steps

1. **Test first** — `tests/scripts/test_grafana_compose_service.py`:
   - Split `test_anonymous_viewer_access_configured`: dev compose keeps anonymous Viewer. Cloud
     compose must set anonymous `false` and no anonymous role.
   - Cloud source test: each JWT setting above is present. The audience and team domain use the
     `:?` guard. No literal 64-hex tag and no literal team domain in the file.
   - Add `CF_ACCESS_TEAM_DOMAIN` and `GRAFANA_CF_ACCESS_AUD` to the render env of this file and of
     the seven other test files that render `docker-compose.cloud.yml`.
   - Confirm the new tests fail on `main` config: `make test-file FILE=tests/scripts/test_grafana_compose_service.py`.
2. **Behaviour test (AC-1, AC-2, AC-3)** — new `tests/scripts/test_grafana_cf_access_jwt.py`.
   Skips without docker or without the pinned image already present (it never pulls).
   - Render the cloud compose, take the grafana service `environment` as committed.
   - Replace only `GF_AUTH_JWT_JWK_SET_URL` with `GF_AUTH_JWT_JWK_SET_FILE` (a throwaway RSA key
     set, mounted read-only). Grafana accepts the URL only over https, so a file replaces it.
     The file does not cover TLS, DNS or key rotation. The test mounts no provisioning volumes
     and drops the datasource secret that needs Postgres.
   - Start a throwaway container on `127.0.0.1:<free port>`, on its own network, no link to
     production. Remove it in `finally`.
   - Assert, against `/api/dashboards/home`:
     - no credentials → 401 (AC-1)
     - JWT signed by a different key, valid `aud` → 401 (AC-2)
     - JWT signed by the test key, wrong `aud` (the agent app tag) → 401 (AC-2)
     - JWT signed by the test key, right `aud`, `email` claim → 200, user is Viewer (positive control)
     - the same token with an `aud` array holding the tag (Cloudflare sends an array) → 200
     - expired token → 401
     - admin basic auth → 200 (AC-3)
3. **Compose edit** — `docker-compose.cloud.yml` grafana block and its header comment.
4. **Docs** — `.env.example` (`GRAFANA_CF_ACCESS_AUD`, and note the `CF_ACCESS_TEAM_DOMAIN`
   dependency); `.claude/skills/create-visualization/SKILL.md` rows that say prod is anonymous
   Viewer; `tests/integration/test_fre1072_tempo_grafana_acceptance.py` AC-10 class docstring and
   target note, if it runs against the cloud stack.
5. **AC-5** — tunnel route for the "monitoring" hostname, read from the cloudflared startup log:
   `http://kibana:5601`. No `kibana` service exists in either compose file and no such container
   runs. Recommendation: delete the Access app and the tunnel route. Reported in the handoff only.

## Codex plan-review disposition

| Finding | Disposition |
|---|---|
| Basic auth and a session cookie are separate auth paths, so a bad JWT beside a valid Basic header still reads | Accepted: ticket item 4 and AC-3 keep Basic. Pinned by a test and named in the compose comment. A test shows the JWT login sets no session cookie. |
| Persisted `grafana_data_cloud` may hold users, tokens or sessions | Not changed by this PR. Master inventories them after deploy (runbook below). A JWT email that matches an existing user logs in as that user, with that user's role. |
| `aud` string against an array claim | Confirmed correct. The behaviour test covers a string, an array and a mixed array. |
| `jwk_set_file` is a partial stand-in for `jwk_set_url` | Accepted. The docstring says what it does not cover. The live URL returns a JWKS (2 RS256 keys, each with a `kid`). AC-4 covers the real fetch. |
| Behaviour test skips without docker or the image | Accepted. Evidence comes from the local run, recorded in the handoff. |

## Gates

`make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
Commit, then `feature-dev:code-reviewer` and `security-review` on `git diff origin/main...HEAD`.

## Post-deploy (master)

A `docker compose up -d grafana` recreate of the grafana container only. The compose edit does
not reach the running container until then. Deploy class: stricter (access control). Verify:

- `curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:${GRAFANA_HOST_PORT}/api/dashboards/home` → 401
- `curl -s -u admin:<pw> .../api/dashboards/home` → 200 (do not echo the password)
- Inventory the persisted Grafana state (read-only, admin Basic on the loopback port):
  `/api/users`, `/api/serviceaccounts/search`, `/api/auth/keys`. Revoke any token or account that
  the owner does not recognise.
- `docker logs cloud-sim-grafana` shows no JWT key-set fetch error.
- Owner opens observe on phone or laptop: dashboards show, no login form (AC-4).

Rollback: restore the three anonymous lines, recreate the grafana container.
