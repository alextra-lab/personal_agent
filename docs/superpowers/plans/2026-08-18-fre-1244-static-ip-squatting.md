# FRE-1244 — Static-IP squatting: stop addressing containers by IP

**Ticket**: [FRE-1244](https://linear.app/frenchforest/issue/FRE-1244/static-ip-squatting-recreating-one-service-at-a-time-can-make-caddy)
**Backing design intent**: none — no ADR governs this topology; `docs/guides/CLOUD_DEPLOYMENT.md` is the
living operational reference. Master's AC section on the ticket picked the remedy: *stop addressing
containers by IP*, over static-for-everyone or a narrowed dynamic range.

## Root cause (confirmed via git blame)

- Commit history shows `caddy` was originally pinned to `172.25.0.10`. Commit `4bc0ee0c` ("fix(infra):
  change Caddy static IP from .10 to .12 (IPAM corruption after Docker upgrade)") moved the declared
  address to `.12` but **never touched** `config/cloud-sim/Caddyfile`'s `http://172.25.0.10` site block,
  the service comment, or `docs/guides/CLOUD_DEPLOYMENT.md` — hence today's three-way disagreement.
- The squat mechanism itself: `seshat-pwa` (`.11`) and `caddy` (`.12`) are the only services with a
  declared `ipv4_address`; every other service free-floats to the lowest available address in
  `172.25.0.0/16`. If a static-holder is stopped while a free-floating service is recreated, the
  free-floater claims the vacated static address and never yields it back.

## Scope decision: WARP access is retired, not re-pointed

Cloudflare's WARP split-tunnel routes the raw CIDR `172.25.0.0/16` to the VPS Docker bridge — it is an
L3 private-network route, not app-layer hostname routing, so it fundamentally requires dialling a
specific, known-in-advance IP. That is incompatible with "no container has a declared static address."
Master's AC section chose the option that removes this operation entirely rather than making it more
reliable, so this plan **removes the WARP-by-raw-IP path** rather than re-declaring it against a new
address. Evidence gathered for AC-5 (below) supports that this path is not currently relied on.

The repo cannot change the Cloudflare Zero Trust dashboard-side split-tunnel route (no Terraform tracks
it in this repo — confirmed via `git ls-files | grep -i terraform`, empty). That side is out of scope;
the PR/handoff will flag it as a manual follow-up for whoever owns the CF dashboard.

## AC-5 evidence (gathered before touching anything, so removal isn't an assumption) — revised after codex review

- `docker-compose.cloud.yml` git blame: `.10` → `.12` move was 2026-04-26 (~4 months ago). The Caddyfile
  and docs have been silently wrong since.
- Elasticsearch `caddy-access-*` (filebeat-shipped, FRE-1146, live since ~2026-08-01): `_count` query for
  `message` containing `"172.25.0.10"` → **0 hits** across all 5,605 documents in the only index that
  exists (`caddy-access-2026-08`).
  - **Codex-flagged weakness, kept in the writeup rather than smoothed over**: a request dialling the
    wrong/stale `.10` address would fail at the Docker network/routing layer (nothing is listening
    there — `.10` is `cloud-sim-filebeat`, not an HTTP server on port 80) *before* it ever reaches
    Caddy's HTTP server to be logged. Zero hits in `caddy-access-*` therefore cannot distinguish "nobody
    tried" from "someone tried and got a connection-level failure that was never logged anywhere." This
    query is corroborating, not diagnostic, and the writeup must say so plainly rather than lean on it as
    proof.
  - Filebeat also only started shipping *this* container's logs with FRE-1146 (~2026-08-01), so the
    query's window is ~2.5 weeks, not the full ~4-month broken period.
- Ticket text itself: "Nobody has reported phone access failing." — the strongest evidence available,
  since it covers the full ~4-month window, not just the telemetry slice.
- **What removal actually changes for a WARP device**: `{$AGENT_HOST}` already has public Cloudflare
  Tunnel ingress (`config/cloud-sim/Caddyfile` line 56; the tunnel design spec exposes it at an
  `example.com` subdomain). A WARP-enrolled device does not need the private-network CIDR route to reach
  a *publicly* tunneled hostname — it can reach `{$AGENT_HOST}` exactly like any other internet client,
  with or without WARP's split-tunnel enabled. The private-IP route only mattered if the phone's
  configured entry point was the raw IP specifically (e.g. a bookmark, or an app config field) rather
  than the hostname.
  - **This is asserted, not verified** — no WARP-enrolled device is available to this build session to
    test against. Flag explicitly in the PR/handoff as a required manual confirmation: before or
    immediately after deploy, whoever owns a WARP-enrolled phone should confirm `https://{$AGENT_HOST}`
    still works from that device. This is the AC-5 gap codex review surfaced and it is not closable from
    inside this session — record it as an open verification, not a closed criterion.

## Steps

1. **`docker-compose.cloud.yml`** — remove the `ipv4_address` override for `seshat-pwa` (line ~437) and
   `caddy` (line ~501); both revert to the plain `- cloud-sim` network list form used by every other
   service. Remove the stale `# Static IP 172.25.0.10 ...` comment on the caddy service and the
   `# WARP devices route 172.25.0.0/16 through this tunnel → reach Caddy at 172.25.0.10` comment on
   `cloudflared`. The `ipam.config.subnet: 172.25.0.0/16` network-level declaration stays — that's the
   network's own address space, not a per-service claim, and AC-1's grep explicitly allows it.

2. **`config/cloud-sim/Caddyfile`** — delete the `http://172.25.0.10 { import routing }` site block
   (lines 47–50) and its block comment. Update the file-header comment (lines 9–10, "plain HTTP for WARP
   private network access (172.25.0.10)") to drop the now-false claim.

3. **`docs/guides/CLOUD_DEPLOYMENT.md`** — update every place that describes the static-IP/WARP-by-IP
   model as current: the top banner (`> **Access**: Cloudflare WARP private network → 172.25.0.10`), the
   architecture-overview ASCII diagram, the "Static IPs assigned only to..." network paragraph, the
   "Site blocks" list, and the "Cloudflare WARP Tunnel" section (architecture diagram + "Enrolling a
   device" step 3, which currently claims `172.25.0.10 is reachable`). Replace with: WARP/phone access
   goes through the same `{$AGENT_HOST}` hostname block as any other client; nothing in the compose
   network is addressed by a fixed IP.

4. **New guard test** — `tests/scripts/test_compose_no_hardcoded_container_ips.py`, mirroring
   `test_compose_port_collisions.py`'s static/no-docker-dependency style:
   - Parse every `docker-compose*.yml` under repo root (same file-name scope as the port-collision guard
     precedent, and the same scope AC-1's own "How checked" column specifies — `config/` and
     `docker-compose*.yml`, not an unbounded repo-wide scan); assert no service under
     `services.*.networks` declares `ipv4_address` **or** `ipv6_address` (codex flagged v6 as a same-class
     blind spot the original draft missed).
   - Parse `config/cloud-sim/Caddyfile`; assert no site-block *address* is a raw IPv4 literal, matched
     narrowly against Caddy's own site-address line shape (`^(https?://)?(\d{1,3}\.){3}\d{1,3}(:\d+)?\s*\{`
     at the start of a top-level, non-indented line — i.e. a block header, not any line containing an IP).
     Deliberately does **not** flag `remote_ip 172.25.0.0/16` (an ACL matcher, not a container address) or
     the network-level `subnet: 172.25.0.0/16` ipam declaration — both are legitimate CIDR-range uses that
     AC-1's own grep instruction excludes, and flagging them would be a false positive, not a stricter
     check.
   - **Scope explicitly not covered, documented rather than silently gapped** (per codex review): this
     guard does not scan arbitrary env-var values, non-compose YAML, or IPs appearing mid-line in Caddy
     directives other than a site-block header. It enforces the same surface the ticket's own AC-1 "How
     checked" column names. A broader repo-wide literal-IP ban was considered and rejected — it would
     false-positive on legitimate CIDR ACLs and the ipam subnet line without narrowing logic that isn't
     worth the complexity for what this ticket actually regresses (a container claiming a specific
     address, not any mention of an IP string).
   - Seeded-negative proof (per AC-4, run manually before committing, not left in the diff): temporarily
     reintroduce `ipv4_address: 172.25.0.12` under caddy → test fails with a clear message naming the
     service; remove it → test passes. Same for a reintroduced `http://172.25.0.13 {` block in the
     Caddyfile → fails, then passes once removed. Capture both fail/pass transcripts for the handoff.

5. **AC-3 demonstration** — do NOT run this against the live `cloud-sim` stack (it fronts real inbound
   traffic; reproducing the exact incident on purpose is out of scope for a build session). Instead, spin
   up a disposable Docker bridge network on a non-conflicting, deliberately small subnet (`172.30.0.0/29`
   — only a handful of usable addresses, so "lowest free address" is forced and deterministic rather than
   coincidental) with two `busybox:stable sleep infinity` containers standing in for "caddy"
   (static-IP-holder) and "another service" (free-floater), using `docker network create`/`docker run`
   directly (no compose file committed — throwaway, cleaned up after).
   - **Codex correction applied**: `docker stop` does NOT release a container's network endpoint —
     the address stays reserved until the container is removed. The repro must `docker rm -f` the
     static-holder (mirroring what a real `docker compose up -d --force-recreate` does), not just stop
     it, or the free-floater will never be offered the address and the whole demonstration is a no-op.
   - **Before-shape repro**: create container A with static `--ip 172.30.0.4`; `docker rm -f` it (release
     the endpoint); start container B with no static IP (dynamic allocation grabs the lowest free
     address, `.4`, since the subnet is small enough to make this deterministic); try to `docker run` A
     again with its static IP → expect Docker's `Address already in use` error, capturing the exact
     message.
   - **After-shape proof**: same sequence, but container A never declares a static IP (only takes
     whatever's free) → the run always succeeds regardless of order. Because nothing is reserved there is
     structurally nothing to fail on, so this is necessarily a "trivially always passes" demonstration —
     the real evidence for AC-3 is compound, not this run alone (see next point).
   - **Compound evidence, per codex review** — a single successful disposable-network run does not by
     itself prove "always." Pair it with: (a) `docker compose -f docker-compose.cloud.yml config` output
     after the fix, showing no service carries `ipv4_address`, and (b) the Step-4 guard test passing,
     which structurally enforces the same property on every future edit. All three go in the handoff as
     the AC-3 evidence bundle, not the Docker run in isolation.
   - Clean up the scratch network/containers immediately after capturing output.

6. **Quality gates**: `make test` (new guard test + full suite), `make mypy`, `make ruff-check` +
   `make ruff-format`, `pre-commit run --all-files`, `docker compose -f docker-compose.cloud.yml config`
   (validates the edited compose file parses) — all run without touching the live `cloud-sim` containers.

## Explicitly out of scope (flag in PR/handoff, don't implement)

- Cloudflare Zero Trust dashboard split-tunnel route for `172.25.0.0/16` — not tracked as code in this
  repo; removing/adjusting it is a manual dashboard step for whoever owns Cloudflare Access, noted as a
  post-deploy gotcha.
- `docs/architecture/2026-05-08-fre-214-vps-topology-audit.md` — a dated, historical audit snapshot that
  still describes Caddy by raw IP (codex review flagged this). Left as-is deliberately: AC-1's own "How
  checked" column scopes the requirement to `config/` and `docker-compose*.yml`, not `docs/` generally,
  so this file is outside the criterion's own stated verification surface. Rewriting a dated audit's
  point-in-time snapshot to match a later reality would misrepresent it as current. Only
  `CLOUD_DEPLOYMENT.md` — the living operational reference — is in scope for the doc update in Step 3.

## Acceptance criteria mapping

| AC | Satisfied by |
| -- | -- |
| AC-1 | Steps 1–2; guard test in Step 4 makes it structural |
| AC-2 | Steps 1–3 — the address is gone from all three locations, not reconciled to a new one |
| AC-3 | Step 5's before/after Docker demonstration |
| AC-4 | Step 4's guard test + seeded-negative transcripts |
| AC-5 | Evidence section above, recorded in the handoff regardless of outcome |
