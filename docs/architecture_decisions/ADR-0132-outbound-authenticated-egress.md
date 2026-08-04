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
**reasoned** (an inference the reader can check). Claims elsewhere in this document that
restate a fact from this section cite it implicitly.

- Eight credentials live in the gateway process: the Anthropic, OpenAI, Perplexity,
  Voyage and Linear keys, the managed embedding token, the internal artifact-resolve
  token, and the Cloudflare Access client id + secret pair *(measured:
  `src/personal_agent/config/settings.py` — fields at lines 762, 1267, 1322, 1327, 1332,
  1352, 1372, 1964, 1973)*. They are not all the same kind: six are **outbound**
  credentials the app presents to external services; the artifact-resolve token is an
  **inbound** shared secret the gateway *verifies* on requests arriving from the
  artifact Worker *(measured: verification site in
  `src/personal_agent/service/artifacts_router.py`; `settings.py:734` comment)*.
- The outbound Cloudflare Access headers are constructed at two call sites — the
  language-model client and the health-check scheduler runner — and the health probe
  accepts and forwards the headers it is handed, a third code location in the deletion
  scope *(measured: construction at `src/personal_agent/llm_client/client.py:440-448`
  and `src/personal_agent/observability/slm_health/scheduler_runner.py:59-61`;
  forwarding parameter documented at
  `src/personal_agent/observability/slm_health/probe.py:49-50`)*. Injection is
  conditional: headers are added only when the target URL contains
  `slm_tunnel_base_url` *(measured: `client.py:440-443`)*. Each location is an
  independent place to forget or drift *(reasoned)*.
- The outbound pair (`cf_access_client_id`/`cf_access_client_secret`) is distinct from
  the **inbound** JWT-verification fields (`cf_access_team_domain`/`cf_access_aud`),
  which authenticate arriving requests and are out of scope here *(measured:
  `settings.py:1999-2014`)*.
- An in-process egress guard exists but is **wired to nothing**: `DomainGuard` (off,
  blocklist and allowlist modes, from FRE-225 under ADR-0028) is defined in
  `src/personal_agent/security.py`, and its `check_url` has zero production callers —
  every caller is in `tests/test_security/test_domain_guard.py` *(measured: `ast-grep
  run -p '$X.check_url($$$)'` over `src/` and `tests/`, 2026-08-04; the LLM and health
  paths call httpx directly)*. Its tests pass while no real egress consults it — the
  guard is currently an advisory that nothing reads *(reasoned)*.
- Caddy v2.11.2 runs on this host *(measured: `docker exec cloud-sim-caddy caddy
  version`)* and already fronts an inbound Cloudflare-tunnelled path to Elasticsearch,
  path-allowlisted so a leaked token cannot touch indices outside one family
  *(measured: `config/cloud-sim/Caddyfile:91-107`, the FRE-411 block)*. A neighbouring
  block already sets explicit dial and response-header timeouts *(measured:
  `Caddyfile:118-121`)*. All inbound traffic — PWA, API, Neo4j, the ES write path —
  already routes through this Caddy instance *(measured: `Caddyfile` site blocks at
  lines 43-133)*.
- The Caddyfile is edited routinely: 8 commits touched it in the last three months
  *(measured: `git log --oneline --since=2026-05-01 -- config/cloud-sim/Caddyfile`)*.
  No CI job runs `caddy validate` today *(measured: `grep -rl caddy
  .github/workflows/` returns nothing)*.
- The existing cloudflared container publishes this host outward; there is no
  client-side tunnel machinery to extend *(measured: `docker-compose.cloud.yml:514-525`)*.
- Caddy logs JSON to standard output and nothing ships it: there is no Caddy, access or
  proxy index in Elasticsearch *(measured: no `caddy*`/`access*`/`proxy*` index in ES;
  `Caddyfile` log blocks all say `output stdout`)*. The Docker log driver keeps three
  files of ten megabytes and a container recreation resets them *(measured:
  `/etc/docker/daemon.json` — `json-file`, `max-size 10m`, `max-file 3`)*.
- Inference is streamed end-to-end; streaming was adopted to keep bytes flowing through
  the Cloudflare edge on long generations (avoiding 524-class edge timeouts)
  *(measured: `client.py:450-459` comment and `payload["stream"] = True`)*. A real turn
  has been observed running 417 seconds *(measured: reported in FRE-1143 from ES route
  traces)*.
- The host has 22 GiB of memory with ~6 GiB currently available *(measured: `free -h`,
  2026-08-04)*.
- An evidence-capture outage precedent exists: the per-call token emit went dark on
  2026-05-10 and was noticed only in the 2026-08-03 analysis that produced FRE-1142
  *(measured: FRE-1142's commissioning record)*.

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

1. **Streaming must not buffer — by relying on documented behaviour, not a flag.**
   Caddy's `reverse_proxy` flushes immediately when the response `Content-Type` is
   `text/event-stream` or the content length is unknown *(reasoned from the Caddy
   `reverse_proxy` documentation)* — which SSE inference responses are. The block
   therefore sets **no** `flush_interval`: forcing `-1` is unnecessary for SSE and has a
   documented side effect (it prevents Caddy from cancelling the upstream request when
   the downstream client disconnects, which would orphan long inference generations and
   burn Mac-side compute for an abandoned turn) *(reasoned from the same
   documentation)*. Whether streaming actually survives the hop is not assumed from
   documentation — it is AC-1, measured at first-event granularity. The buffering risk,
   stated precisely: buffering at this local Caddy would delay the app's first token and
   stall incremental consumption; it would **not** recreate an edge 524, because the
   Cloudflare edge sits upstream of this proxy and continues to receive the Mac's bytes
   regardless *(reasoned — corrects an earlier draft of this ADR)*.
2. **No body-duration timeout.** `response_header_timeout` covers time-to-first-header
   only, so it cannot sever a stream that has begun; what would sever a 417-second turn
   is an overall response/idle timeout, which Caddy's HTTP transport does not impose by
   default *(reasoned from Caddy transport documentation)*. The block sets
   `dial_timeout` (fail fast on a dead tunnel), may set `response_header_timeout`, and
   **deliberately omits** any body-duration timeout, with a comment in the Caddyfile
   stating that the omission is load-bearing.
3. **Connection reuse is pinned.** The transport's `keepalive` is set explicitly rather
   than inherited from defaults, so connection reuse to the tunnel survives later edits
   to the block *(reasoned)*.

The two app-side construction sites, the probe's forwarding path, and the outbound
settings fields (`cf_access_client_id`, `cf_access_client_secret`,
`slm_tunnel_base_url`) are **deleted**, not kept as fallback. The application has no
outbound Cloudflare concept at all. The `CF_ACCESS_CLIENT_ID`/`CF_ACCESS_CLIENT_SECRET`
environment variables move from the gateway service to the caddy service in compose. The
inbound JWT-verification fields (`cf_access_team_domain`, `cf_access_aud`) are untouched.

### D2 — Credential custody principle: environment vs application credentials

The test: **if this system redeploys on a different topology, does the credential
survive?**

- **Environment credentials** authenticate a deployment topology to its own plumbing.
  The CF Access pair exists only because this deployment reaches the Mac through a
  Cloudflare tunnel; a local install has no tunnel and the credential ceases to exist.
  These belong to the environment layer — Caddy and compose — and D1 moves them there.
- **Application credentials** are deployment-invariant: a provider credential, when
  configured, belongs to application capability rather than deployment topology — any
  Seshat that calls Anthropic holds the Anthropic key regardless of the box it runs on.
  The six outbound application credentials (Anthropic, OpenAI, Perplexity, Voyage,
  Linear, managed embedding token) **stay in the application**. This is a decision of
  principle, not timing — there is no "revisit later" condition, because the answer is
  not "not yet" but "wrong layer".
- The **artifact-resolve token** is inbound: the gateway verifies it on requests
  arriving from the artifact Worker. Custody-by-proxy is not even coherent for it — a
  verifier must hold the secret it verifies against. It stays with the gateway under the
  same principle (the verification duty is the application's), and it is listed here so
  the census of eight is complete rather than silently seven.

Consciously accepted trade: for the application credentials, the "enforced network
boundary" security argument is forgone. A prompt-injected agent or the sandboxed
execution tool could in principle read those keys out of the process *(reasoned — the
keys are process-resident configuration)*; that exposure remains mitigated by the cost
gate (ADR-0120 lineage), sandbox isolation, and — once wired per this ADR — the
in-process domain guard, not by a network boundary. The proxy alternative mostly
converts credential theft into credential use anyway (see Alternatives), which is why
the portability and single-custody-mode arguments win.

**The in-process domain guard is retained — and this ADR obliges wiring it in, because
today it is retained in name only.** The measured finding above (zero production
callers) means "keep the guard" would otherwise preserve a check nobody consults. The
implementation chain routes the application's outbound HTTP call paths through the
guard, so the two layers are real: the guard catches the URL before a request is
formed; Caddy catches the host at the boundary. Deliberately redundant — once both
actually exist.

### D3 — Caddy access logs are captured into Elasticsearch, in scope, not a follow-up

A Filebeat sidecar container ships the caddy container's logs to a `caddy-access-*`
index with an ILM policy from day one, using the currently supported mechanism — a
`filestream` input with the `container` parser over the Docker json-file logs, a stable
input `id`, and a persistent registry volume so recreations neither re-ingest nor drop
*(reasoned from current Filebeat documentation; the legacy `container` input type is
deprecated)*.

The owner's stated benefit of the chokepoint — one place to look when connectivity is
troubled — does not exist while the only record is a thirty-megabyte ring buffer that
resets on container recreation. Centralizing the failure surface without centralizing
the evidence would be worse than the status quo, and the evidence-capture outage
precedent above (token emit dark for three months) is exactly this failure shape.

Caddy's JSON access log records the *incoming* request's headers, not the `header_up`
values Caddy adds toward the upstream *(reasoned from Caddy log documentation)* — so the
captured log both avoids storing the injected CF secret and doubles as evidence that the
application sent no credential (used by AC-2).

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
orphan-env check *(measured: the ADR-0099 cross-config guard in `.pre-commit-config.yaml`
and CI)* the backstop for leftover `CF_ACCESS_*` variables in the gateway's environment,
and AC-4 asserts each profile *resolves* correctly rather than merely that a literal is
absent.

### Sequencing

**FRE-1142 lands first.** It instruments the inference path (usage deltas from the
inference server) and removes the estimator; this ADR's chain then changes the transport
path with the new instrument already watching. The reverse order would cut the path over
blind and take FRE-1142's baseline on a path that had just changed. The implementation
chain carries a blocked-by relation on FRE-1142.

---

## Alternatives Considered

### Option 1: Full credential custody — Caddy holds all outbound credentials

**Description:** Every outbound authenticated call traverses Caddy; per-service site
blocks inject the six outbound application credentials as well as the CF pair; the
application holds only the inbound artifact-resolve verifier secret.

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
  lives and the proxy cannot judge payloads *(reasoned)*. Custody buys audit and
  rate-bounding, far less than "the app holds no credentials" sounds like.
- **Breaks the portability the proxy was meant to buy.** Caddy is optional in a
  non-Cloudflare install, so a proxyless deployment needs app-held keys anyway → two
  custody modes, one rarely exercised (the dead-value failure mode this ADR exists to
  kill).
- **Config coupling.** Today a bad provider config breaks one provider; with custody,
  one bad Caddyfile edit breaks all upstreams at once, on a host where the Caddyfile is
  edited routinely (8 commits in three months, measured above). On this box Caddy
  already fronts all inbound traffic (measured above), so the *availability* SPOF is
  not new — but the coupling of unrelated upstreams into one file is *(reasoned)*.
- **Six migrations, not one decision.** Cloud model clients need base-URL overrides and
  some libraries resist them *(reasoned; reported per-client in FRE-1143 — each client
  is its own verification burden)*.

**Why Rejected:** The environment/application principle (D2) answers it categorically:
provider keys are deployment-invariant application credentials, so relocating them to an
optional environment component is the wrong layer regardless of security appetite.

### Option 2: Status quo plus config fix — keep app-side CF injection, just repair the dead value

**Description:** Fix `llm_base_url` per profile and keep the in-process CF-Access
injection sites.

**Pros:**
- Smallest possible diff; no new Caddy block, no sidecar.
- No new component in the inference path.

**Cons:**
- The two construction sites and the forwarding path remain independent places to
  forget or drift.
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
  away (FRE-411, measured above); the existing cloudflared container publishes this
  host outward (measured above) and shares nothing with this client-side use.
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
- Deferred capture is how the token-emit outage stayed dark for three months (measured
  above); "later" tickets for evidence have a record of not firing until an incident
  forces them *(reasoned)*.

**Why Rejected:** Owner decision 2026-08-04: capture is part of the decision, not a
follow-up. Centralizing failure without centralizing evidence is worse than the status
quo.

---

## Consequences

### Positive Consequences

- The application holds no outbound Cloudflare credentials and no tunnel topology
  knowledge; one URL differs per deployment profile and nothing else does.
- The drift-prone injection code collapses into one declarative Caddyfile block, next
  to the FRE-411 block that already proves the pattern.
- Outbound tunnel traffic gains a durable, queryable evidence trail (`caddy-access-*`)
  that survives container recreation — the first shipped log pipeline on this host,
  reusable for other containers later.
- The dead `127.0.0.1:1234` default and its failure class are removed structurally, not
  patched.
- The domain guard goes from defined-but-unconsulted to actually enforcing on the
  production call paths — this ADR converts a latent control into a real one.
- A written custody principle (environment vs application) now exists for the next
  credential question, ending per-credential relitigating.

### Negative Consequences

- SLM calls gain a proxy hop; the process's availability now includes the caddy
  container for inference (it already did for all inbound traffic, measured above).
- A new Filebeat container joins the compose file — additional memory on a host with
  ~6 GiB currently available (measured above), and one more thing to health-check.
- The forgone network boundary for application credentials: theft-from-process of the
  six outbound app keys remains possible and is mitigated in-process only.
- Because the egress block does not force `flush_interval -1`, streaming correctness
  rests on Caddy's content-type detection; AC-1 exists precisely because this is relied
  on rather than forced *(reasoned)*.
- Local Mac installs use a different call shape (direct, no CF) than the VPS (via
  Caddy) — environment parity is deliberately traded for app simplicity.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Proxy buffers or times out a long streamed turn | High | SSE auto-flush relied on + no body-duration timeout + load-bearing comment in the block; AC-1 measures first-event latency and full-duration survival on a real turn |
| Filebeat dies silently and evidence goes dark again | Medium | AC-3 asserts correlated capture across recreation; Filebeat gets a compose healthcheck; absence of fresh `caddy-access-*` docs is alertable in Kibana |
| Orphaned `CF_ACCESS_*` env vars linger on the gateway | Low | Config-guard orphan-env check (measured above) fails CI once the settings fields are gone; AC-2 scans repo and runtime |
| Caddyfile edit breaks the egress block along with inbound blocks | Medium | Egress is a separate site block; the chain **adds** `caddy validate` to CI for Caddyfile-touching changes (none exists today, measured above) |
| Guard wiring regresses a call path (new failure mode on the hot path) | Medium | Guard wiring ships behind its own ticket with tests through production wiring (AC-5); guard `off` mode remains the escape hatch |
| FRE-1142 slips and stalls this chain | Low | Sequencing is a blocked-by relation; master can re-sequence explicitly if FRE-1142 is re-scoped — the constraint is measurement hygiene, not a hard dependency |

---

## Implementation Notes

- **Caddyfile** (`config/cloud-sim/Caddyfile`): new internal egress site block — CF
  header injection from env, `dial_timeout`, explicit `keepalive`, **no**
  `flush_interval` and **no** body-duration timeout (both commented as load-bearing),
  JSON access log.
- **Compose** (`docker-compose.cloud.yml`): `CF_ACCESS_CLIENT_ID`/`SECRET` move from the
  gateway service environment to the caddy service; new `filebeat` service —
  `filestream` input + `container` parser, stable input `id`, persistent registry
  volume, healthcheck; caddy's egress listener exposed on the compose network only.
- **Application deletions**: `cf_access_client_id`, `cf_access_client_secret`,
  `slm_tunnel_base_url` fields in `settings.py`; construction logic in
  `llm_client/client.py` and `observability/slm_health/scheduler_runner.py`; the
  forwarding parameter through `observability/slm_health/probe.py`.
- **Guard wiring** (new obligation from the measured finding): route the application's
  outbound HTTP call paths through `DomainGuard.check_url` so the FRE-225 control
  actually enforces; its own ticket in the chain.
- **Config correction**: `llm_base_url` default and per-profile values per the D4
  matrix; `.env.example` updated.
- **ES**: `caddy-access-*` index template + ILM policy (monthly rollover per the
  FRE-1036 convention).
- **CI**: add `caddy validate` for changes touching the Caddyfile.
- **Testing**: unit tests for the client without injection logic; per-profile settings
  resolution tests (AC-4's instrument); the FRE-375 guard keeps tests off the tunnel; a
  live streamed-turn verification is part of the seam adjudication, not CI.
- **Sequencing**: chain blocked-by FRE-1142.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria, asserted once, by the seam ticket below (ADR-0130
D1/D2). Each can fail; a half-finished implementation fails at least one.

- **AC-1 — A long streamed inference turn survives the proxy, streaming.** A real
  generation of at least 420 seconds (longer than the longest observed turn, 417 s)
  driven through the Caddy egress path completes un-severed, **and** streams rather
  than buffers: client-observed time-to-first-SSE-event through the proxy is within
  +2 seconds of a direct-path baseline taken with the same prompt (direct call to the
  tunnel with CF headers supplied out-of-band from the host's `.env`).
  · **Check:** record client-side timestamps (request sent, first SSE event, completion)
  for both runs; confirm the proxied run in `caddy-access-*` and the route-trace record
  with matching duration. · *Fails if* the stream buffers (first event arrives late or
  only at completion), any timeout severs the ≥420 s run, or the request bypassed Caddy.
- **AC-2 — The application demonstrably sends no Cloudflare credential, anywhere.**
  · **Check:** (a) repo scan: `CF-Access-`, `CF_ACCESS_`, `cf_access_client`,
  `slm_tunnel_base_url` appear nowhere outside the Caddyfile, compose, `.env*`, docs,
  and this ADR's history — no application source match; (b) runtime: the gateway
  container's `env` shows no `CF_ACCESS_*`; (c) evidence from the wire: the
  `caddy-access-*` document for a live successful SLM call shows the incoming request
  from the gateway carried **no** `CF-Access-*` header while the upstream returned
  success — proving injection happened at Caddy, not in the app. · *Fails if* any
  application-source match survives (renamed vars and hard-coded headers are caught by
  the header-name scan), the gateway env still carries the pair, or the logged incoming
  request shows a CF header.
- **AC-3 — The evidence trail survives what used to erase it, for the egress block
  specifically.** · **Check:** send a uniquely-tagged request through the egress block
  (unique path/query marker), `docker compose up -d --force-recreate caddy`, send a
  second uniquely-tagged request; query `caddy-access-*` for **both markers** filtered
  to the egress site's logger identity. Both documents present and queryable in Kibana.
  · *Fails if* either marker is missing (pre-recreation evidence lost, or
  post-recreation ingestion dead) — regardless of what any healthcheck claims, and
  unsatisfiable by unrelated inbound Caddy traffic.
- **AC-4 — Every profile resolves to a live-correct endpoint; the dead-default class is
  closed.** · **Check:** a test loads each supported profile through the real settings
  resolver and asserts its resolved SLM endpoint (prod/dev → the internal Caddy egress
  URL; test → the FRE-375 substrate value; local-Mac → a direct SLM URL) and the absence
  of any CF field; behaviourally, the prod/VPS profile's endpoint answers the existing
  SLM health probe through the egress block, and the FRE-375 isolation guard still
  passes for the test profile. The local-Mac profile's resolution is asserted by the
  test; its live reachability is owner-verified on the Mac (not checkable from the
  VPS). Supplemental: `grep -rn "127.0.0.1:1234\|localhost:1234" src/ config/
  .env.example` returns nothing. · *Fails if* any profile resolves to an unreachable or
  wrong-layer endpoint, the health probe cannot reach the SLM through Caddy, or a
  loopback literal survives in config-bearing files.
- **AC-5 — A disallowed egress URL is actually refused on the production call path.**
  The guard is not merely retained but enforcing. · **Check:** an integration test
  drives the application's real outbound HTTP path (the production wiring, not the
  guard class directly) with allowlist mode active and a disallowed domain, and asserts
  the request is refused *before* any connection is attempted; the same path permits an
  allowlisted domain. · *Fails if* the guard remains unconsulted by the production call
  path (today's measured state), the test exercises the class directly instead of the
  wiring, or refusal only happens after a connection attempt.

**Seam ticket:** filed with the implementation chain (FRE-1143 close-out names it), due
**2026-09-01** — the earliest date all five criteria are adjudicable (chain merged +
deployed behind FRE-1142, one recreation cycle observed).

---

## References

- [FRE-1143](https://linear.app/frenchforest/issue/FRE-1143) — the commissioning ticket, with the owner-verified factual baseline
- [FRE-225](https://linear.app/frenchforest/issue/FRE-225) — in-process egress domain guard (retained and wired by D2)
- ADR-0028 — tool integration tiers; parent of the egress guard
- [FRE-411](https://linear.app/frenchforest/issue/FRE-411) — the precedent Caddy block: CF-tunnelled, path-allowlisted ES write endpoint
- [FRE-1142](https://linear.app/frenchforest/issue/FRE-1142) — inference-path instrumentation; sequencing predecessor
- ADR-0112 — configurable substrate backends; the profile mechanism the D4 matrix rides on
- ADR-0120 — cost governance; part of the retained in-process mitigation for application credentials
- ADR-0130 — two tiers of acceptance criteria; governs the seam ticket
- [Caddy `reverse_proxy` directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — streaming/auto-flush behaviour, `flush_interval` cancellation side effect, transport timeouts, keepalive
- [Filebeat `filestream` input](https://www.elastic.co/docs/reference/beats/filebeat/filebeat-input-filestream) — the supported container-log capture mechanism (legacy `container` input is deprecated)

---

## Status Updates

### 2026-08-04 - Proposed
**Changed By:** adr session (FRE-1143)
**Reason:** Authored after owner discussion settled all four decisions and the sequencing constraint. Codex review round 1 (7 blocking, 3 minor) addressed in full; round 1 also surfaced a measured finding — the FRE-225 domain guard has zero production callers — which added the guard-wiring obligation to D2 and the chain.
