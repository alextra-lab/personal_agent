# FRE-1144 — ADR-0132 D1 complete: Caddy terminates outbound CF Access for both upstream classes

**Ticket:** [FRE-1144](https://linear.app/frenchforest/issue/FRE-1144) (Approved → In Progress)
**ADR:** `docs/architecture_decisions/ADR-0132-outbound-authenticated-egress.md` (Accepted) — D1 (both phases) + D4 + Completion
**Branch:** `fre-1144-caddy-slm-egress-cutover`
**Risk tier:** Complex → codex plan-review done (v1 rejected, 6 blocking findings); owner approval required before coding.

> **Revision 2.** Codex rejected v1 outright ("not safe to implement as planned"). Five of its six findings
> were confirmed against the code and are fixed below; one was rejected with reason. Owner then directed
> folding the artifact origins in, which resolves the most severe finding.

---

## Scope change from the ticket as written

FRE-1144 was ticketed as "Phase 1" (SLM tunnel only). **It cannot satisfy its own AC-a in that form.**
AC-a requires the CF pair to be absent from the gateway process; but `cf_access_service_token_headers()`
reads those same fields, so removing them silently breaks four live call sites. Phase 1 and Phase 2 are
therefore one atomic change, and the owner has directed folding artifacts in.

**This PR delivers ADR-0132 D1 in full (both phases) + D4 + Completion.** It absorbs **FRE-1145**
(Phase 2) — master should close or rescope that ticket rather than leave it dangling.

**Still out of scope:** Filebeat / `caddy-access-*` shipping (**D3, FRE-1146**, which this ticket blocks)
and domain-guard wiring (**D2**, its own ticket). Codex argued D3 must ship here on the ADR's
"not a follow-up" language; rejected — the implementation chain already ticketed D3 separately and
FRE-1144 blocks it. Sequencing is the control plane's call. Flagged to master in the handoff.

---

## Correction to the ADR's credential inventory

ADR-0132's Context buckets `memory/embeddings.py` and `memory/reranker.py` under "class 2 — CF
Access-protected artifact origins" because they call the shared `cf_service_token.py` helper. **Measured,
they reach the SLM tunnel, not artifacts:** both gate on `settings.slm_tunnel_base_url in endpoint`
(`embeddings.py:541`, `reranker.py:194`), and `reranker.py:115` builds its endpoint from
`slm_tunnel_base_url`. The ADR classified by helper, not by upstream.

| Upstream class | Consumers (measured) |
|---|---|
| **SLM tunnel** (Mac, via `slm.<domain>`) | `llm_client/client.py`, `observability/slm_health/scheduler_runner.py`, `llm_client/provider_health.py`, `memory/embeddings.py`, `memory/reranker.py` |
| **Artifacts origin** (Worker) | `service/artifacts_router.py:376`, `observability/artifact_envelope/probe.py:73` |

Recorded as an ADR status-update entry (Step 14) so the next reader is not misled.

---

## Design

```
BEFORE  app ──(CF headers built in-process, 7 sites)──> slm.<domain> / artifacts.<domain>
AFTER   app ──> caddy:8600 ──(CF injected)──> slm.<domain>
        app ──> caddy:8601 ──(CF injected)──> artifacts.<domain>
```

### Settings consolidation (fixes Codex #2, #3, #4)

v1 invented four fields and a `@property` raising a non-existent `ConfigurationError`. Replaced with **one
field per upstream class**, each declared by the deployment's own compose file, and enforcement in a
`model_validator(mode="after")` so it fails at construction rather than on first access.

| Field | Replaces | cloud | local | eval | `APP_ENV=test` |
|---|---|---|---|---|---|
| `slm_base_url` | `llm_base_url` **and** `slm_tunnel_base_url` (both deleted) | `http://caddy:8600` | direct SLM URL | eval compose value | fixture URL (conftest) |
| `artifacts_egress_base_url` | — (new) | `http://caddy:8601` | unset (no CF locally) | unset | unset |

`llm_base_url` and `slm_tunnel_base_url` collapse into `slm_base_url`: with all local inference on the
tunnel they name the same thing, and the duplicate is what let the dead `127.0.0.1:1234` default survive.
No default — unset on a profile that needs it raises at validation.

### Deletions (Completion, owner-approved)

`cf_access_client_id`, `cf_access_client_secret`, `slm_tunnel_base_url`, `llm_base_url`, and
`service/cf_service_token.py` are all deleted. Inbound `cf_access_team_domain` / `cf_access_aud` are
**untouched** — they authenticate arriving requests.

---

## Steps

### Step 1 — Tests first (TDD)

**`tests/test_config/test_slm_endpoint_resolution.py`** (new) — AC-c's instrument.
Exact-value assertion per `deployment_profile ∈ {local, cloud, eval}` and the `APP_ENV=test` axis;
unset-on-required-profile raises at construction; executable assertion that
`grep -rn "127.0.0.1:1234\|localhost:1234" src/ config/ .env.example` is empty.

**`tests/test_llm_client/test_no_cf_injection.py`** (new) — AC-b's instrument.
Header construction on all five SLM consumers yields no `CF-Access-*` key; repo-scan asserting
`cf_access_client|slm_tunnel_base_url` matches **nothing** in `src/` (now fully achievable — no Phase-2
residue, since Phase 2 is in this PR).

**Verify:** `make test-file FILE=tests/test_config/test_slm_endpoint_resolution.py` → fails.

### Step 2 — `config/settings.py`

Delete `llm_base_url` (160-162), `cf_access_client_id` / `cf_access_client_secret` (1964-1982),
`slm_tunnel_base_url` (1983-1992). Add `slm_base_url: str | None` and
`artifacts_egress_base_url: str | None` (`AGENT_`-prefixed, Google docstrings citing ADR-0132 D4).
Change `slm_health_url` default off the placeholder; derive from `slm_base_url` when unset.
Add `_validate_slm_endpoint_per_profile` `model_validator(mode="after")` raising on an unset
required value for the active profile.

### Step 3 — SLM consumers (five)

- `llm_client/client.py` — delete constants 61-62 and the injection block 440-448; line 95
  `settings.llm_base_url` → `settings.slm_base_url`; drop the `localhost:1234` docstring example (72).
- `llm_client/provider_health.py` — delete `_cf_access_headers` (35-50) and its call site (77).
- `observability/slm_health/scheduler_runner.py` — delete `cf_headers` (58-61) and the argument.
- `observability/slm_health/probe.py` — remove the `cf_headers` parameter entirely; update the 403
  `hint=` to name Caddy's credential as the rotation target.
- `memory/embeddings.py` — delete the CF branch (541-543); **the benign `User-Agent` moves to Caddy**
  (`header_up`), since the WAF block is a Cloudflare-topology concern.
- `memory/reranker.py` — `slm_base` (115) from `slm_base_url`; delete the CF branch (194-195).

### Step 4 — Artifact consumers (two) + helper deletion

- Delete `service/cf_service_token.py`.
- `service/artifacts_router.py` — the export fetcher rewrites an artifacts-origin URL's origin to
  `artifacts_egress_base_url` (path preserved) instead of attaching headers; non-origin hosts unchanged.
- `observability/artifact_envelope/probe.py` — same origin rewrite before probing `public_url`.

### Step 5 — `config/model_loader.py`

Replace `_apply_slm_tunnel_override` (63-114) with the same placeholder rewrite keyed on
`settings.slm_base_url`, renamed to reflect profile resolution rather than tunnel override.

### Step 6 — `llm_client/concurrency.py` + remaining `llm_base_url` consumers (fixes Codex #3)

`concurrency.py:168` — delete the `"http://127.0.0.1:1234/v1"` default. Migrate
`dspy_adapter.py:174`, `captains_log/reflection.py:367`, `llm_client/models.py:192` to `slm_base_url`.

### Step 7 — Test migration (fixes Codex #5 — v1 omitted this entirely)

- `tests/test_llm_client/test_client.py` — rewrite `test_respond_sends_cf_access_headers_with_trace_on_tunnel` (224), `test_cf_access_headers_injected_for_slm_endpoint` (758), `test_cf_access_headers_not_injected_for_localhost` (814) to assert **absence** of injection; replace `base_url="http://localhost:1234/v1"` fixtures (116, 501, 599, 632, 670, 707).
- `tests/test_config/test_model_loader.py` — 8 `slm_tunnel_base_url` monkeypatches (292-398) → `slm_base_url`.
- `tests/test_config/test_settings.py` — delete the `127.0.0.1:1234` default assertion (91) and the three CF-field tests (342-366).

### Step 8 — `config/cloud-sim/Caddyfile` — two egress blocks

```caddyfile
# ── OUTBOUND EGRESS — Mac SLM tunnel (ADR-0132 D1, FRE-1144) ─────────────────
# Every other block here is INBOUND (external → internal service). These two are
# OUTBOUND: the app dials them on the compose network and Caddy adds the
# Cloudflare Access service token, so the application holds no CF credential.
# Neither listener is published in `ports:` — compose network only.
:8600 {
	reverse_proxy https://{$SLM_HOST:slm.example.com} {
		header_up CF-Access-Client-Id {$CF_ACCESS_CLIENT_ID}
		header_up CF-Access-Client-Secret {$CF_ACCESS_CLIENT_SECRET}
		header_up Host {$SLM_HOST:slm.example.com}
		# The OpenAI SDK's default UA is WAF-blocked at the edge; the override is
		# a Cloudflare-topology concern, so it lives here, not in the app.
		header_up User-Agent "seshat-gateway/1.0"

		transport http {
			dial_timeout 10s
			response_header_timeout 60s
			keepalive 90s
			keepalive_idle_conns 8
		}

		# LOAD-BEARING OMISSION 1 (ADR-0132 D1.1): no `flush_interval`. Caddy
		# auto-flushes for text/event-stream or unknown length — which SSE
		# inference responses are. Forcing -1 is unnecessary AND prevents Caddy
		# cancelling upstream on client disconnect, orphaning long generations.
		#
		# LOAD-BEARING OMISSION 2 (ADR-0132 D1.2): no body-duration/idle timeout.
		# Real turns run to 417 s. response_header_timeout covers
		# time-to-first-header only and cannot cut a stream already flowing.
	}
	log {
		output stdout
		format json
	}
}

# ── OUTBOUND EGRESS — artifacts origin (ADR-0132 D1 Phase 2, folded in) ──────
:8601 {
	reverse_proxy https://{$ARTIFACTS_HOST:artifacts.example.com} {
		header_up CF-Access-Client-Id {$CF_ACCESS_CLIENT_ID}
		header_up CF-Access-Client-Secret {$CF_ACCESS_CLIENT_SECRET}
		header_up Host {$ARTIFACTS_HOST:artifacts.example.com}
		transport http {
			dial_timeout 10s
			response_header_timeout 30s
			keepalive 90s
		}
	}
	log {
		output stdout
		format json
	}
}
```

**Verify:** `docker run --rm -v "$PWD/config/cloud-sim/Caddyfile:/etc/caddy/Caddyfile:ro" caddy:2-alpine caddy validate --config /etc/caddy/Caddyfile` exits 0.

### Step 9 — `docker-compose.cloud.yml`

`caddy`: add `env_file: - /opt/seshat/.env.caddy`; add `SLM_HOST` / `ARTIFACTS_HOST` to `environment:`.
No new `ports:` entries. `seshat-gateway`: delete the `AGENT_CF_ACCESS_*` remap (369-370) and its stale
comment (366-368, which wrongly claims cloudflared uses the pair); add `AGENT_SLM_BASE_URL:
http://caddy:8600` and `AGENT_ARTIFACTS_EGRESS_BASE_URL: http://caddy:8601`.

### Step 10 — `docker-compose.eval.yml` + local + `.env.example` (fixes Codex #2)

Set `AGENT_SLM_BASE_URL` explicitly in the eval compose file and document the local value — v1 invented
fields nothing set. `.env.example`: delete the `AGENT_LLM_BASE_URL` block (118-126); add `SLM_HOST`,
`ARTIFACTS_HOST`, `AGENT_SLM_BASE_URL`; state that `CF_ACCESS_CLIENT_ID`/`SECRET` belong in
`.env.caddy`, **not** `.env`.

### Step 11 — `tests/conftest.py`

`os.environ.setdefault("AGENT_SLM_BASE_URL", "http://localhost:9999")` in the FRE-375 block, same
idiom, commented with ADR-0132 D4 — tests never reach the tunnel.

### Step 12 — `.github/workflows/ci.yml`

New `caddy-validate` job modeled on `config-guard` (216-237); `needs: changes`, gated
`if: github.event_name == 'push' || needs.changes.outputs.backend == 'true'` (`config/**` already in the
`backend` filter). One `docker run ... caddy validate` step.

### Step 13 — Docs

ADR-0132 status-update: D1 complete both phases, the inventory correction above, D3/D2 still open.
Update `src/personal_agent/llm_client/AGENTS.md` (4 × `localhost:1234`) and `config/AGENTS.md`
(`llm_base_url` references).

---

## Acceptance criteria → evidence

| AC | Proof | By whom |
|---|---|---|
| **AC-a** | Runbook: live completion; `docker logs cloud-sim-caddy` shows the 8600 entry; `docker exec cloud-sim-seshat-gateway env \| grep -i cf_access` → **empty** | master, post-deploy |
| **AC-b** | `test_no_cf_injection.py` + repo scan — now fully clean, no Phase-2 residue | this session |
| **AC-c** | `test_slm_endpoint_resolution.py` exact-value per profile + executable grep | this session |
| **AC-d** | CI `caddy-validate` green on the PR; probe-through-Caddy is a runbook step | CI + master |

---

## Quality gates

`make test` (module then full) · `make mypy` · `make ruff-check` · `make ruff-format` ·
`pre-commit run --all-files` · code-review skill at **high** · security-review skill.

---

## Post-deploy runbook (master)

1. Create `/opt/seshat/.env.caddy` with `CF_ACCESS_CLIENT_ID` + `CF_ACCESS_CLIENT_SECRET`; `chmod 600`.
2. **Remove both from `/opt/seshat/.env`** — this is what makes the custody move real.
3. Add `SLM_HOST=<real>` and `ARTIFACTS_HOST=<real>` to `/opt/seshat/.env`.
4. `docker compose -f docker-compose.cloud.yml up -d caddy seshat-gateway`.
5. **AC-a:** issue a completion; `docker exec cloud-sim-seshat-gateway env | grep -i cf_access` → empty;
   `docker logs cloud-sim-caddy --since 5m` → JSON entry with **no** `CF-Access-*` on the incoming request.
6. **AC-d:** `/api/inference/status` healthy, its request visible in the same log.
7. **Regression (7 consumers):** inference, provider health, embedding, reranking, artifact export,
   envelope probe — the six features ADR-0132 names, plus the SLM health probe.

**Safety:** do not publish 8600/8601 in `ports:`. Do not add `flush_interval` or any body-duration
timeout to the SLM block — both omissions are load-bearing and commented as such.
