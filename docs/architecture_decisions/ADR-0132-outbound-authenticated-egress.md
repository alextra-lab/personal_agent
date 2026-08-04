# ADR-0132: Outbound Authenticated Egress — Caddy Terminates the Cloudflare Barrier; Application Credentials Stay in the Application

**Status:** Proposed
**Date:** 2026-08-04
**Deciders:** Owner + adr session (FRE-1143)
**Tags:** security, infrastructure, egress, caddy, cloudflare, observability, configuration

---

## Context

**What is the issue we're addressing?**

A dead configuration value opened into a security-boundary question. The gateway's
`llm_base_url` setting defaults to `http://127.0.0.1:1234/v1` — legacy LM Studio
configuration from before development moved to the VPS, unreachable from inside the
container, and nothing noticed *(measured: `src/personal_agent/config/settings.py:161`)*.
The owner's standing position is that all development happens on the VPS and every local
model call traverses the Cloudflare tunnel to the Mac.

The owner asked whether a reverse proxy could hold the Cloudflare barrier so the
application need not know the tokens, and whether the other authenticated endpoints could
be managed the same way. That splits into three decisions — tunnel termination, credential
custody, and evidence capture — plus a configuration correction and a sequencing call.

### What is true today

Claims below are marked **measured** (with the file or command that produced them) or
**reasoned** (an inference the reader can check).

- Eight credentials live in the gateway process: the Anthropic, OpenAI, Perplexity,
  Voyage and Linear keys, the managed embedding token, the internal artifact-resolve
  token, and the Cloudflare Access client id + secret pair *(measured:
  `src/personal_agent/config/settings.py` — fields at lines 762, 1267, 1322, 1327, 1332,
  1352, 1372, 1964, 1973)*.
- The outbound Cloudflare Access headers are injected at three call sites — the
  language-model client, the health-check scheduler runner, and the health probe
  *(measured: `src/personal_agent/llm_client/client.py:440-448`,
  `src/personal_agent/observability/slm_health/scheduler_runner.py:59-61`,
  `src/personal_agent/observability/slm_health/probe.py:49-50`)*. Injection is
  conditional: headers are added only when the target URL contains
  `slm_tunnel_base_url` *(measured: `client.py:440-443`)*. Each site is an independent
  place to forget the headers and an independent place for them to drift *(reasoned)*.
- The outbound pair (`cf_access_client_id`/`cf_access_client_secret`) is distinct from
  the **inbound** JWT-verification fields (`cf_access_team_domain`/`cf_access_aud`),
  which authenticate arriving requests and are out of scope here *(measured:
  `settings.py:1999-2014`)*.
- An in-process egress guard already exists: a domain guard with off, blocklist and
  allowlist modes, from FRE-225 under ADR-0028. It checks outbound URLs before a request
  is formed, holds no credentials, and cannot constrain code that does not consult it
  *(measured: `src/personal_agent/security.py`; behaviour reasoned from its design)*.
- Caddy v2.11.2 runs on this host *(measured: `docker exec cloud-sim-caddy caddy
  version`)* and already fronts an inbound Cloudflare-tunnelled path to Elasticsearch,
  path-allowlisted so a leaked token cannot touch indices outside one family
  *(measured: `config/cloud-sim/Caddyfile:91-107`, the FRE-411 block)*. A neighbouring
  block already sets explicit dial and response-header timeouts *(measured:
  `Caddyfile:118-121`)*.
- The existing cloudflared container publishes this host outward; there is no
  client-side tunnel machinery to extend *(measured: `docker-compose.cloud.yml:514-525`)*.
- Caddy logs JSON to standard output and nothing ships it: there is no Caddy, access or
  proxy index in Elasticsearch *(measured: no `caddy*`/`access*`/`proxy*` index in ES;
  `Caddyfile` log blocks all say `output stdout`)*. The Docker log driver keeps three
  files of ten megabytes and a container recreation resets them *(measured:
  `/etc/docker/daemon.json` — `json-file`, `max-size 10m`, `max-file 3`)*.
- Inference is streamed end-to-end; streaming exists specifically to avoid Cloudflare
  524 timeouts on long generations *(measured: `client.py:450-459` comment and
  `payload["stream"] = True`)*. A real turn has been observed running 417 seconds
  *(measured: reported in FRE-1143 from ES route traces)*.

### Constraint discovered during discussion

A proxy removes credentials from the application only if the proxy is **always in the
path**. If Caddy is optional — present on the VPS, absent in a local non-Cloudflare
install — then for any credential the proxy holds, a proxyless deployment either needs a
second, app-held credential path (two custody modes, one rarely exercised — exactly the
species of rot that produced the dead `llm_base_url` value) or cannot call the service at
all *(reasoned)*. This constraint is what forces a principled split rather than a bulk
move.

---

## Decision

Four parts, decided together because they only make sense together.

### D1 — Caddy terminates the outbound Cloudflare barrier for the SLM tunnel

A new egress site block in `config/cloud-sim/Caddyfile` listens on an internal
address reachable only from the compose network, injects `CF-Access-Client-Id` /
`CF-Access-Client-Secret` from environment variables supplied to the **caddy** service,
and reverse-proxies to the tunnel hostname over HTTPS. The application addresses a plain
internal URL and holds no Cloudflare egress credentials.

Three constraints are settled **in this decision**, not left to implementation:

1. **Streaming must not buffer.** The block sets `flush_interval -1` so Caddy flushes
   response bytes immediately; inference is SSE-shaped and buffering would reintroduce
   the 524-class stalls streaming was adopted to kill *(reasoned from Caddy
   `reverse_proxy` documentation; asserted live by AC-1)*.
2. **No body-duration timeout.** `response_header_timeout` covers time-to-first-header
   only, so it cannot sever a stream that has begun; what would sever a 417-second turn
   is an overall response/idle timeout, which Caddy's HTTP transport does not impose by
   default. The block sets `dial_timeout` (fail fast on a dead tunnel), may set
   `response_header_timeout`, and **deliberately omits** any body-duration timeout, with
   a comment in the Caddyfile stating that the omission is load-bearing *(reasoned)*.
3. **Connection reuse is pinned.** The transport's `keepalive` is set explicitly rather
   than inherited from defaults, so connection reuse to the tunnel survives later edits
   to the block *(reasoned)*.

The three app-side injection sites and the outbound settings fields
(`cf_access_client_id`, `cf_access_client_secret`, `slm_tunnel_base_url`) are
**deleted**, not kept as fallback. The application has no outbound Cloudflare concept at
all. The `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET` environment variables move from
the gateway service to the caddy service in compose. The inbound JWT-verification fields
(`cf_access_team_domain`, `cf_access_aud`) are untouched.

### D2 — Credential custody principle: environment vs application credentials

The test: **if this system redeploys on a different topology, does the credential
survive?**

- **Environment credentials** authenticate a deployment topology to its own plumbing.
  The CF Access pair exists only because this deployment reaches the Mac through a
  Cloudflare tunnel; a local install has no tunnel and the credential ceases to exist.
  These belong to the environment layer — Caddy and compose — and D1 moves them there.
- **Application credentials** are deployment-invariant: every Seshat, on any box, needs
  the Anthropic key to be Seshat. The seven application credentials (Anthropic, OpenAI,
  Perplexity, Voyage, Linear, managed embedding token, artifact-resolve token) **stay in
  the application**. This is a decision of principle, not timing — there is no "revisit
  later" condition, because the answer is not "not yet" but "wrong layer".

Consciously accepted trade: for the application credentials, the "enforced network
boundary" security argument is forgone. A prompt-injected agent or the sandboxed
execution tool could still read those keys out of the process; that exposure remains
mitigated by the in-process domain guard, the cost gate (ADR-0120 lineage), and sandbox
isolation — not by a network boundary. The proxy alternative mostly converts credential
theft into credential use anyway (see Alternatives), which is why the portability and
single-custody-mode arguments win.

**The in-process domain guard is retained in either case.** It catches the URL before a
request is formed; Caddy catches the host at the boundary. Two checks at two layers,
deliberately redundant.

### D3 — Caddy access logs are captured into Elasticsearch, in scope, not a follow-up

A Filebeat sidecar container reads the Docker JSON log files for the caddy container and
ships them to a `caddy-access-*` index with an ILM policy from day one. The owner's
stated benefit of the chokepoint — one place to look when connectivity is troubled —
does not exist while the only record is a thirty-megabyte ring buffer that resets on
container recreation. Centralizing the failure surface without centralizing the evidence
would be worse than the status quo, and this project has a live precedent: the per-call
token emit went dark on 2026-05-10 and nobody noticed for three months.

### D4 — Per-profile endpoint correction

The trigger of this ADR was a config defect, and D1 changes what "correct" means per
environment, so the correction is explicit scope:

| Profile | SLM endpoint value | CF concept in app config |
|---|---|---|
| Prod / VPS | internal Caddy egress URL | none |
| Dev (on the VPS — the owner's standing position) | same as prod; the `127.0.0.1:1234` default is **deleted**, not overridden | none |
| Local Mac install | direct SLM server URL; no Caddy required | none |
| Test | FRE-375 isolation substrate value; tests never reach the tunnel | none |

Enforcement is structural: deleting the settings fields makes the CI config guard's
orphan-env check the backstop for leftover `CF_ACCESS_*` variables in the gateway's
environment, and AC-4 asserts the dead default cannot silently return.

### Sequencing

**FRE-1142 lands first.** It instruments the inference path (usage deltas from the
inference server) and removes the estimator; this ADR's chain then changes the transport
path with the new instrument already watching. The reverse order would cut the path over
blind and take FRE-1142's baseline on a path that had just changed. The implementation
chain carries a blocked-by relation on FRE-1142.

---

## Alternatives Considered

### Option 1: Full credential custody — Caddy holds all eight credentials

**Description:** Every outbound authenticated call traverses Caddy; per-service site
blocks inject the provider key; the application holds no credentials at all.

**Pros:**
- A key not in the process cannot be read out of it by prompt injection or the sandbox
  tool; theft-from-process is structurally closed.
- Per-path scoping, rate limiting and a uniform audit trail at one chokepoint (the
  FRE-411 block shows the shape).
- Secrets centralized in one component, making a later move to the host's secrets
  manager a single integration.

**Cons:**
- **Theft → use is most of the story, not a caveat.** For provider keys, essentially any
  well-formed request with the key is legitimate-shaped; the payload is where the harm
  lives and the proxy cannot judge payloads. Custody buys audit and rate-bounding, far
  less than "the app holds no credentials" sounds like.
- **Breaks the portability the proxy was meant to buy.** Caddy is optional in a
  non-Cloudflare install, so a proxyless deployment needs app-held keys anyway → two
  custody modes, one rarely exercised (the dead-value failure mode this ADR exists to
  kill).
- **Config coupling.** Today a bad provider config breaks one provider; with custody,
  one bad Caddyfile edit breaks all seven upstreams at once, on a host where Caddyfile
  edits are routine. On a single box Caddy already fronts inbound, so the *availability*
  SPOF is not new — but the coupling of unrelated upstreams into one file is.
- **Seven migrations, not one decision.** Cloud model clients need base-URL overrides
  and some libraries resist them; each client is its own verification burden.

**Why Rejected:** The environment/application principle (D2) answers it categorically:
provider keys are deployment-invariant application credentials, so relocating them to an
optional environment component is the wrong layer regardless of security appetite.

### Option 2: Status quo plus config fix — keep app-side CF injection, just repair the dead value

**Description:** Fix `llm_base_url` per profile and keep the three in-process CF-Access
injection sites.

**Pros:**
- Smallest possible diff; no new Caddy block, no sidecar.
- No new component in the inference path.

**Cons:**
- Three independent injection sites remain three independent places to forget or drift.
- The app keeps a topology-specific credential and the conditional
  `slm_tunnel_base_url` matching logic — CF plumbing inside application code.
- Local-install portability stays worse: the app carries dormant CF code that a
  non-Cloudflare deployment never exercises.

**Why Rejected:** It preserves exactly the structure that produced the trigger defect —
topology knowledge embedded in application config, exercised only on some deployments.

### Option 3: Client-side cloudflared instead of Caddy

**Description:** Run `cloudflared access` as a local forward proxy that attaches the
Access token, instead of a Caddy site block.

**Pros:**
- First-party Cloudflare tooling for exactly this handshake.
- No CF secrets in the Caddyfile's environment.

**Cons:**
- Adds a second proxy component where Caddy already runs the identical pattern one block
  away (FRE-411); the existing cloudflared container runs in the opposite direction and
  shares nothing with this use.
- No path scoping, no unified access log, no reuse of the log-shipping decided in D3.

**Why Rejected:** Duplicates a capability the host already runs, while delivering less
(no scoping, no unified evidence trail).

### Option 4: Log capture as a follow-up (D3 deferred)

**Description:** Land the egress block now; persist Caddy logs to a volume file as a
floor; ship to ES in a later ticket.

**Pros:**
- Smaller first PR; one fewer container in the initial change.

**Cons:**
- The chokepoint's stated benefit (one place to look) does not exist until capture
  exists; a volume file is grep-only evidence, invisible to Kibana and to the
  self-diagnosing pipeline.
- Deferred capture is how the token-emit outage stayed dark for three months; "later"
  tickets for evidence have a measured record of not firing until an incident forces
  them.

**Why Rejected:** Owner decision 2026-08-04: capture is part of the decision, not a
follow-up. Centralizing failure without centralizing evidence is worse than the status
quo.

---

## Consequences

### Positive Consequences

- The application holds no outbound Cloudflare credentials and no tunnel topology
  knowledge; one URL differs per deployment profile and nothing else does.
- Three drift-prone injection sites collapse into one declarative Caddyfile block, next
  to the FRE-411 block that already proves the pattern.
- Outbound tunnel traffic gains a durable, queryable evidence trail (`caddy-access-*`)
  that survives container recreation — the first shipped log pipeline on this host,
  reusable for other containers later.
- The dead `127.0.0.1:1234` default and its failure class are removed structurally, not
  patched.
- A written custody principle (environment vs application) now exists for the next
  credential question, ending per-credential relitigating.

### Negative Consequences

- SLM calls gain a proxy hop; the process's availability now includes the caddy
  container for inference (it already did for all inbound traffic).
- A new Filebeat container joins the compose file — more memory on a 10 GiB host, one
  more thing to health-check.
- The forgone network boundary for application credentials: theft-from-process of the
  seven app keys remains possible and is mitigated in-process only.
- Local Mac installs use a different call shape (direct, no CF) than the VPS (via
  Caddy) — environment parity is deliberately traded for app simplicity.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Proxy buffers or times out a long streamed turn | High | `flush_interval -1`, no body-duration timeout, load-bearing comment in the block; AC-1 asserts a real long turn live |
| Filebeat dies silently and evidence goes dark again | Medium | AC-3 asserts capture across recreation; Filebeat gets a healthcheck in compose; absence of fresh `caddy-access-*` docs is alertable in Kibana |
| Orphaned `CF_ACCESS_*` env vars linger on the gateway | Low | Config-guard orphan-env check fails CI once the settings fields are gone (AC-2 asserts the container env directly) |
| Caddyfile edit breaks the egress block along with inbound blocks | Medium | Egress is a separate site block; `caddy validate` runs in CI where the Caddyfile is touched; inbound blocks unchanged by this ADR |
| FRE-1142 slips and stalls this chain | Low | Sequencing is a blocked-by relation; master can re-sequence explicitly if FRE-1142 is re-scoped — the constraint is measurement hygiene, not a hard dependency |

---

## Implementation Notes

- **Caddyfile** (`config/cloud-sim/Caddyfile`): new internal egress site block — CF
  header injection from env, `flush_interval -1`, `dial_timeout`, explicit `keepalive`,
  no body-duration timeout (commented as load-bearing), JSON access log.
- **Compose** (`docker-compose.cloud.yml`): `CF_ACCESS_CLIENT_ID`/`SECRET` move from the
  gateway service environment to the caddy service; new `filebeat` service with
  healthcheck reading the Docker json-file logs; caddy port for the internal egress
  listener exposed on the compose network only.
- **Application deletions**: `cf_access_client_id`, `cf_access_client_secret`,
  `slm_tunnel_base_url` fields in `settings.py`; injection logic in
  `llm_client/client.py`, `observability/slm_health/scheduler_runner.py`,
  `observability/slm_health/probe.py` (and its caller wiring).
- **Config correction**: `llm_base_url` default and per-profile values per the D4
  matrix; `.env.example` updated.
- **ES**: `caddy-access-*` index template + ILM policy (monthly rollover per the
  FRE-1036 convention).
- **Testing**: unit tests for the client without injection logic; the FRE-375 guard
  keeps tests off the tunnel; a live streamed-turn verification is part of the seam
  adjudication, not CI.
- **Sequencing**: chain blocked-by FRE-1142.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria, asserted once, by the seam ticket below (ADR-0130
D1/D2). Each can fail; a half-finished implementation fails at least one.

- **AC-1 — A long streamed inference turn survives the proxy.** A real turn of ≥120
  seconds streamed through the Caddy egress path completes un-severed, with first-token
  latency comparable to the pre-cutover path. · **Check:** run one long-generation turn
  on the VPS path; confirm completion and duration in the `slm-requests-*` /
  route-trace records, and the matching request in `caddy-access-*` showing the same
  duration. · *Fails if* the stream is buffered (first token arrives only at
  completion), severed mid-stream by a proxy timeout, or the request bypassed Caddy.
- **AC-2 — The gateway process holds no outbound Cloudflare credentials, and inference
  still works.** · **Check:** `docker exec` the gateway container: `env` contains no
  `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET`; the settings model has no such
  fields; then a live SLM call through the internal endpoint succeeds. · *Fails if* the
  vars are still in the gateway env, any app code still injects `CF-Access-*` headers,
  or inference only works because credentials remain app-side.
- **AC-3 — The evidence trail survives what used to erase it.** Caddy access logs are
  queryable in Elasticsearch from both sides of a caddy container recreation.
  · **Check:** note the latest `caddy-access-*` doc, `docker compose up -d --force-recreate
  caddy`, drive one request through the egress block, then query the index for docs both
  before and after the recreation timestamp. · *Fails if* the index is missing, Filebeat
  is not running, pre-recreation docs are gone, or post-recreation requests never appear.
- **AC-4 — The dead-default failure class is closed, not patched.** No profile carries
  the unreachable loopback default. · **Check:** `grep -rn "127.0.0.1:1234"
  src/ config/ .env.example` returns nothing; the prod/VPS profile's SLM endpoint answers
  the existing SLM health probe through the egress block; the test profile's value stays
  inside the FRE-375 substrate (existing isolation guard passes). · *Fails if* the
  literal survives anywhere config-bearing, or the configured endpoint does not answer
  its own profile's health probe.
- **AC-5 — The in-process domain guard still enforces.** The guard was explicitly not
  deleted, and still blocks. · **Check:** a unit test asserts allowlist mode refuses a
  disallowed domain at the client layer (existing FRE-225 tests still pass and still
  execute — not skipped). · *Fails if* guard code was removed, bypassed for the new
  internal endpoint path, or its tests no longer assert refusal.

**Seam ticket:** filed with the implementation chain (FRE-1143 close-out names it), due
**2026-09-01** — the earliest date all five criteria are adjudicable (chain merged +
deployed behind FRE-1142, one recreation cycle observed).

---

## References

- [FRE-1143](https://linear.app/frenchforest/issue/FRE-1143) — the commissioning ticket, with the owner-verified factual baseline
- [FRE-225](https://linear.app/frenchforest/issue/FRE-225) — in-process egress domain guard (retained by D2)
- ADR-0028 — tool integration tiers; parent of the egress guard
- [FRE-411](https://linear.app/frenchforest/issue/FRE-411) — the precedent Caddy block: CF-tunnelled, path-allowlisted ES write endpoint
- [FRE-1142](https://linear.app/frenchforest/issue/FRE-1142) — inference-path instrumentation; sequencing predecessor
- ADR-0112 — configurable substrate backends; the profile mechanism the D4 matrix rides on
- ADR-0120 — cost governance; part of the retained in-process mitigation for application credentials
- ADR-0130 — two tiers of acceptance criteria; governs the seam ticket
- [Caddy `reverse_proxy` directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — `flush_interval`, transport timeouts, keepalive
- [Filebeat Docker input](https://www.elastic.co/guide/en/beats/filebeat/current/filebeat-input-container.html) — container log capture mechanism

---

## Status Updates

### 2026-08-04 - Proposed
**Changed By:** adr session (FRE-1143)
**Reason:** Authored after owner discussion settled all four decisions and the sequencing constraint.
