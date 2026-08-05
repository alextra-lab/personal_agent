# ADR-0132: Outbound Authenticated Egress — Caddy Terminates the Cloudflare Barrier; Application Credentials Stay in the Application

**Status:** Accepted
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
- The CF Access pair reaches **two distinct classes of upstream**, not one. FRE-1143
  said "three or more" injection sites; the full measured inventory is larger:
  1. **The Mac SLM tunnel** — headers constructed at
     `src/personal_agent/llm_client/client.py:440-448` (conditional on the target URL
     containing `slm_tunnel_base_url`),
     `src/personal_agent/observability/slm_health/scheduler_runner.py:59-61`, and
     `src/personal_agent/llm_client/provider_health.py:35-77` (a documented local
     duplicate); `observability/slm_health/probe.py` accepts and forwards handed-in
     headers.
  2. **CF Access-protected artifact origins** (the artifacts Worker, the `/lib/`
     shelf, served artifact URLs) — via the shared helper
     `src/personal_agent/service/cf_service_token.py`, consumed by
     `memory/embeddings.py:543`, `memory/reranker.py:195`,
     `service/artifacts_router.py` (artifact export, FRE-530), and
     `observability/artifact_envelope/probe.py` *(measured: `grep -rn
     cf_access_service_token_headers src/`, 2026-08-04; the helper returns an empty
     mapping when the pair is unset, so these callers spread it opportunistically)*.
  An earlier draft of this ADR under-counted this inventory; deleting the fields
  without migrating class 2 would silently break artifact export, envelope probing,
  and any CF-gated embedding/rerank endpoint *(reasoned)*.
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
- The deployment-profile axis is `AGENT_DEPLOYMENT_PROFILE ∈ {local, cloud, eval}`,
  each keyed to a compose file, with `APP_ENV=test` (FRE-375 isolation) a separate
  axis *(measured: `settings.py:58` — `Literal["local", "cloud", "eval"]`;
  `config/deployment.yaml:18-25`)*.
- The gateway container imports the **entire** host `.env` via `env_file`, explicitly
  documented as passing all variables *(measured: `docker-compose.cloud.yml:337-340`)*.
  Any custody move that leaves the raw `CF_ACCESS_*` names in `.env` therefore leaves
  them in the gateway environment too *(reasoned)*.
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
move. For the CF pair specifically the constraint is satisfied: every one of its uses is
Cloudflare-topology-specific, so a deployment without the proxy is a deployment without
the credential *(reasoned from the inventory above)*.

---

## Decision

Four parts, decided together because they only make sense together.

### D1 — Caddy terminates the outbound Cloudflare barrier — for every CF-Access-protected upstream, phased

The target state: **the application holds no outbound CF service-token concept; every
outbound call to a CF-Access-protected upstream addresses an internal Caddy egress
block, and Caddy injects the service-token headers.** (The *inbound* Cloudflare surface
— JWT verification via `cf_access_team_domain`/`cf_access_aud` and the
`Cf-Access-Jwt-Assertion` / authenticated-user-email headers in
`service/cf_access_jwt.py` and `service/auth.py` — is deliberately retained; it is the
gateway authenticating its callers, not the app calling out.) One egress site block
per upstream class, each path-scoped in the FRE-411 style, each with a JSON access log.

Delivery is phased because the two upstream classes have different risk profiles, but
the phases are both *this ADR's* obligations — the credential fields cannot be deleted
until both land:

- **Phase 1 — the SLM tunnel** (the streaming-critical path): a block fronting the Mac
  tunnel host replaces the three SLM-path construction sites
  (`llm_client/client.py`, `slm_health/scheduler_runner.py` +
  `probe.py` forwarding, `llm_client/provider_health.py`).
- **Phase 2 — the artifact origins**: a block (or blocks) fronting the CF-protected
  artifact Worker / `/lib/` shelf / served-artifact origins replaces the
  `cf_service_token.py` helper and its four consumers (embeddings, reranker, artifact
  export, envelope probe). Regression coverage for all six affected features —
  inference, provider health, embedding, reranking, artifact export, envelope probing —
  is part of this phase's tickets, not an afterthought.
- **Completion**: delete `cf_access_client_id`, `cf_access_client_secret`,
  `slm_tunnel_base_url` (including its consumer
  `config/model_loader.py:_apply_slm_tunnel_override`), and `cf_service_token.py`; the
  inbound JWT-verification fields (`cf_access_team_domain`, `cf_access_aud`) are
  untouched.

Because the gateway imports the whole `.env` (measured above), the custody move also
requires a **compose environment split**: the CF pair moves to a Caddy-only env source
(a separate env file or compose-level `environment:` entries for the caddy service) and
out of the file the gateway's `env_file` imports — otherwise the raw names remain in the
gateway process and the move is cosmetic.

Three constraints on the streaming (Phase 1) block are settled **in this decision**,
not left to implementation:

1. **Streaming must not buffer — by relying on documented behaviour, not a flag.**
   Caddy's `reverse_proxy` flushes immediately when the response `Content-Type` is
   `text/event-stream` or the content length is unknown *(reasoned from the Caddy
   `reverse_proxy` documentation)* — which SSE inference responses are. The block
   therefore sets **no** `flush_interval`: forcing `-1` is unnecessary for SSE and has a
   documented side effect (it prevents Caddy from cancelling the upstream request when
   the downstream client disconnects, which would orphan long inference generations and
   burn Mac-side compute for an abandoned turn) *(reasoned from the same
   documentation)*. Whether streaming actually survives the hop is not assumed from
   documentation — it is AC-1, measured at per-event granularity. The buffering risk,
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

### D2 — Credential custody principle: environment vs application credentials

The test: **if this system redeploys on a different topology, does the credential
survive?**

- **Environment credentials** authenticate a deployment topology to its own plumbing.
  The CF Access pair exists only because this deployment's private surfaces — the Mac
  tunnel and the artifact origins — sit behind Cloudflare Access; a deployment without
  Cloudflare has none of those barriers and the credential ceases to exist. It belongs
  to the environment layer — Caddy and compose — and D1 moves it there in both phases.
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
obligation is stated as scope, not as a vague "the paths": outbound HTTP for the
application's egress seams — LLM client, SLM health (scheduler runner, probe, provider
health), embeddings, reranker, artifact export, envelope probe, and web/search tools —
is **centralized behind one transport factory that consults `DomainGuard.check_url`
before a connection is formed**, and a static repo check (ast-grep rule set) forbids
the known bypass forms outside that factory — httpx client construction, module-level
`httpx.get/post/request` calls, **and** direct construction of the Anthropic/OpenAI SDK
clients (which own their transports; the factory supplies theirs). Stated honestly:
this is lint-grade enforcement over the enumerated forms, not a proof — a novel HTTP
library would enter through review, not past the rule *(reasoned)*. The two layers are then real: the guard catches the URL
before a request is formed; Caddy catches the host at the boundary. Deliberately
redundant — once both actually exist.

### D3 — Caddy access logs are captured into Elasticsearch, in scope, not a follow-up

A Filebeat sidecar container ships the caddy container's logs to a `caddy-access-*`
index with an index template and ILM policy from day one (monthly rollover per the
FRE-1036 convention), using the currently supported mechanism — a `filestream` input
with the `container` parser over the Docker json-file logs, a stable input `id`, and a
**persistent registry volume** so a Filebeat recreation does not re-ingest already
shipped lines *(reasoned from current Filebeat documentation; the legacy `container`
input type is deprecated)*. The delivery guarantee is stated honestly:
**at-least-once after harvest, not lossless across arbitrary recreation.** The registry
preserves read offsets, not unread content — recreating the caddy container deletes its
json-file logs, so lines Filebeat has not yet harvested in that window are gone
*(reasoned from Filebeat's documented harvesting model)*. That loss window (harvest lag,
typically sub-second) is accepted; what the decision guarantees, and AC-3 asserts, is
that **once ingested, evidence survives recreation of both containers, without
duplication** — which is precisely what the 30 MB ring buffer cannot do.

The owner's stated benefit of the chokepoint — one place to look when connectivity is
troubled — does not exist while the only record is a thirty-megabyte ring buffer that
resets on container recreation. Centralizing the failure surface without centralizing
the evidence would be worse than the status quo, and the evidence-capture outage
precedent above (token emit dark for three months) is exactly this failure shape.

Caddy's JSON access log records the *incoming* request's headers, not the `header_up`
values Caddy adds toward the upstream *(reasoned from Caddy log documentation)* — so the
captured log both avoids storing the injected CF secret and doubles as evidence that the
application sent no credential (used by AC-2).

### D4 — Per-profile endpoint correction, on the axes the resolver actually has

The trigger of this ADR was a config defect, and D1 changes what "correct" means per
deployment, so the correction is explicit scope. The matrix is expressed in the real
configuration axes — `AGENT_DEPLOYMENT_PROFILE ∈ {local, cloud, eval}` (measured above)
plus the `APP_ENV=test` / FRE-375 axis — not in invented profile names:

| Resolver input | SLM endpoint resolves to | CF concept in app config |
|---|---|---|
| `cloud` (VPS — prod and dev, per the owner's standing position) | internal Caddy egress URL | none |
| `local` (Mac install) | direct SLM server URL; no Caddy required | none |
| `eval` | the endpoint its compose file (`docker-compose.eval.yml`) declares; never the dead loopback default | none |
| `APP_ENV=test` (any profile) | an explicit test-fixture SLM URL **defined by this chain** in the FRE-375 conftest (today FRE-375 pins Postgres/Neo4j/ES targets but no SLM endpoint — *measured: `tests/conftest.py`*), taking precedence over the profile value; tests never reach the tunnel | none |

The `127.0.0.1:1234` default is **deleted**, not overridden. Enforcement is structural:
deleting the settings fields makes the CI config guard's orphan-env check *(measured:
the ADR-0099 cross-config guard in `.pre-commit-config.yaml` and CI)* the backstop for
leftover `CF_ACCESS_*` variables in the gateway's environment, and AC-4 asserts each
profile *resolves* correctly rather than merely that a literal is absent.

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
- The measured inventory — three SLM-path construction sites plus a shared helper with
  four consumers — remains that many independent places to forget or drift.
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
- The drift-prone injection code — three construction sites plus a shared helper with
  four consumers — collapses into declarative Caddyfile blocks, next to the FRE-411
  block that already proves the pattern.
- Outbound tunnel traffic gains a durable, queryable evidence trail (`caddy-access-*`)
  that survives container recreation — the first shipped log pipeline on this host,
  reusable for other containers later.
- The dead `127.0.0.1:1234` default and its failure class are removed structurally, not
  patched.
- The domain guard goes from defined-but-unconsulted to actually enforcing on the
  enumerated production egress seams, with a static check preventing silent bypass —
  this ADR converts a latent control into a real one.
- A written custody principle (environment vs application) now exists for the next
  credential question, ending per-credential relitigating.

### Negative Consequences

- SLM and artifact-origin calls gain a proxy hop; the process's availability now
  includes the caddy container for those paths (it already did for all inbound traffic,
  measured above).
- A new Filebeat container joins the compose file — additional memory on a host with
  ~6 GiB currently available (measured above), and one more thing to health-check.
- The forgone network boundary for application credentials: theft-from-process of the
  six outbound app keys remains possible and is mitigated in-process only.
- Because the egress block does not force `flush_interval -1`, streaming correctness
  rests on Caddy's content-type detection; AC-1 exists precisely because this is relied
  on rather than forced *(reasoned)*.
- Phase 2 touches six features (inference, provider health, embedding, reranking,
  artifact export, envelope probing); a migration defect there is a user-visible
  artifact or memory failure, which is why regression coverage is named in-scope.
- The compose environment split ends the "every `.env` var reaches the gateway"
  convenience (measured above) for this credential — a deliberate loss: that
  convenience is exactly what makes custody moves cosmetic.
- Local Mac installs use a different call shape (direct, no CF) than the VPS (via
  Caddy) — environment parity is deliberately traded for app simplicity.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Proxy buffers or times out a long streamed turn | High | SSE auto-flush relied on + no body-duration timeout + load-bearing comment in the block; AC-1 measures per-event cadence and full-duration survival on a real turn |
| Phase 2 silently breaks an artifact/memory feature | High | Regression checks for all six affected features are in-scope for the phase-2 tickets; AC-2's wire-level evidence covers the artifact origin too |
| Filebeat dies silently and evidence goes dark again | Medium | AC-3 asserts correlated capture across recreation of both containers; Filebeat gets a compose healthcheck; absence of fresh `caddy-access-*` docs is alertable in Kibana |
| Orphaned `CF_ACCESS_*` env vars linger on the gateway | Low | Compose env split (D1) + config-guard orphan-env check (measured above); AC-2 checks the runtime env for raw and prefixed names |
| Caddyfile edit breaks the egress block along with inbound blocks | Medium | Egress blocks are separate sites; the chain **adds** `caddy validate` to CI for Caddyfile-touching changes (none exists today, measured above) |
| Guard wiring regresses a call path (new failure mode on the hot path) | Medium | Guard wiring ships behind its own ticket with tests through production wiring (AC-5); guard `off` mode remains the escape hatch |
| FRE-1142 slips and stalls this chain | Low | Sequencing is a blocked-by relation; master can re-sequence explicitly if FRE-1142 is re-scoped — the constraint is measurement hygiene, not a hard dependency |

---

## Implementation Notes

- **Caddyfile** (`config/cloud-sim/Caddyfile`): per-upstream internal egress site
  blocks — CF header injection from env, `dial_timeout`, explicit `keepalive`, **no**
  `flush_interval` and **no** body-duration timeout on the SLM block (both commented as
  load-bearing), path scoping per upstream, JSON access log.
- **Compose** (`docker-compose.cloud.yml`): CF pair moves to a Caddy-only env source
  and **out of the file the gateway's blanket `env_file` imports** (split or
  allowlist — the gateway currently receives every `.env` var, measured above); new
  `filebeat` service — `filestream` input + `container` parser, stable input `id`,
  persistent registry volume, healthcheck; egress listeners exposed on the compose
  network only.
- **Application deletions (phased per D1)**: Phase 1 — construction logic in
  `llm_client/client.py`, `slm_health/scheduler_runner.py`,
  `llm_client/provider_health.py`; forwarding through `slm_health/probe.py`; the
  `slm_tunnel_base_url` override in `config/model_loader.py`. Phase 2 —
  `service/cf_service_token.py` and its four consumers (embeddings, reranker,
  artifacts_router export, artifact_envelope probe), with regression checks for all six
  affected features. Completion — `cf_access_client_id`, `cf_access_client_secret`,
  `slm_tunnel_base_url` fields in `settings.py`.
- **Guard wiring** (new obligation from the measured finding): one outbound transport
  factory consulting `DomainGuard.check_url`, adopted by the enumerated egress seams
  (D2); an ast-grep rule set forbidding the enumerated bypass forms (httpx client
  construction, module-level `httpx.get/post/request`, direct Anthropic/OpenAI SDK
  client construction) outside the factory; its own ticket in the chain.
- **Config correction**: `llm_base_url` default and per-profile values per the D4
  matrix; `.env.example` updated.
- **ES**: `caddy-access-*` index template + ILM policy (monthly rollover per the
  FRE-1036 convention).
- **CI**: add `caddy validate` for changes touching the Caddyfile.
- **Testing**: unit tests for the clients without injection logic; per-profile settings
  resolution tests (AC-4's instrument); the FRE-375 guard keeps tests off the tunnel; a
  live streamed-turn verification is part of the seam adjudication, not CI.
- **Sequencing**: chain blocked-by FRE-1142.

---

## Verification / Acceptance Criteria

These are the ADR's own criteria, asserted once, by the seam ticket below (ADR-0130
D1/D2). Each can fail; a half-finished implementation fails at least one.

- **AC-1 — A long streamed inference turn survives the proxy, streaming throughout.**
  A real generation of at least 420 seconds (longer than the longest observed turn,
  417 s) driven through the Caddy egress path completes un-severed and streams for its
  whole duration, not merely at its start. · **Check:** run the same prompt twice —
  direct to the tunnel (CF headers supplied out-of-band from the Caddy-only secret
  source, since D1 removes them from the gateway-visible `.env`) as baseline, and
  through the proxy — recording **every** SSE-event timestamp client-side, with a
  per-run correlation id (trace id) present in both the `caddy-access-*` document and
  the route-trace record. Pass requires all three, objectively: first event within
  +2 s of baseline; at least as many SSE events as the baseline run minus 5%; and
  maximum inter-event gap within 2× the baseline's maximum gap. · *Fails if* the
  stream buffers at the start, degrades into
  batch delivery mid-run (single early event then silence until completion), any
  timeout severs the ≥420 s run, or the correlation id is absent from the Caddy log
  (the request bypassed the proxy).
- **AC-2 — The application demonstrably sends no Cloudflare credential, and the barrier
  demonstrably still exists.** The scan targets the **outbound service-token pair
  only** — the retained inbound surface (`cf_access_team_domain`, `cf_access_aud`,
  `Cf-Access-Jwt-Assertion`, the authenticated-user-email header) is explicitly
  permitted. · **Check:** (a) repo scan: `CF-Access-Client-Id`,
  `CF-Access-Client-Secret`, `cf_access_client_id`, `cf_access_client_secret`,
  `CF_ACCESS_CLIENT_ID`, `CF_ACCESS_CLIENT_SECRET`, `slm_tunnel_base_url`,
  `cf_access_service_token_headers` appear nowhere in application source (`src/`) —
  only in the Caddyfile, compose, `.env*`, docs, and history; (b) runtime: the gateway
  container's `env` contains neither the raw client-id/secret names nor any prefixed
  alias of them; (c) **negative control,
  proving the Access policy is active rather than bypassed:** the same upstream request
  sent directly *without* CF headers is rejected at the Cloudflare edge; (d) evidence
  from the wire: the `caddy-access-*` document for a live successful call (SLM **and**
  artifact-origin) shows the incoming request from the gateway carried no `CF-Access-*`
  header while the upstream returned success — with (c), this proves injection happened
  at Caddy. · *Fails if* any application-source match survives (renamed vars and
  hard-coded headers are caught by the header-name scan), the gateway env still carries
  the pair under any name, the negative control is *not* rejected (barrier silently
  off), or the logged incoming request shows a CF header.
- **AC-3 — Ingested evidence survives what used to erase it, without replay.** (The
  guarantee under test is D3's stated one: at-least-once after harvest — not lossless
  recreation, which the mechanism cannot provide.) · **Check:** send a uniquely-tagged
  request through the egress block (unique path/query marker) and **confirm its
  document is queryable in `caddy-access-*` first**; then `docker compose up -d
  --force-recreate caddy filebeat`; then send a second uniquely-tagged request. Query
  the index filtered to the egress site's logger identity: **exactly one** document per
  marker (the confirmed pre-recreation doc still present, the post-recreation doc
  ingested), and the index's applied template carries the intended ILM policy
  (`GET caddy-access-*/_settings` shows the lifecycle name). · *Fails if* the confirmed
  pre-recreation document is gone (ES-side evidence did not survive), the
  post-recreation marker never appears (ingestion dead after recreate), either marker
  appears more than once (ephemeral registry re-ingested the backlog), or the index has
  no ILM policy attached — regardless of what any healthcheck claims, and unsatisfiable
  by unrelated inbound Caddy traffic.
- **AC-4 — Every real profile resolves to a live-correct endpoint; the dead-default
  class is closed.** · **Check:** a test loads each of `deployment_profile ∈ {local,
  cloud, eval}` and the `APP_ENV=test` axis through the real settings resolver and
  asserts the resolved SLM endpoint per the D4 matrix (cloud → the internal Caddy
  egress URL; local → a direct SLM URL; eval → its compose-declared endpoint; test →
  the chain-defined test-fixture SLM URL, asserted by exact value) and the absence of
  any outbound CF field; behaviourally, the
  cloud profile's endpoint answers the existing SLM health probe through the egress
  block, and the FRE-375 isolation guard still passes under `APP_ENV=test`. The local
  profile's resolution is asserted by the test; its live reachability is owner-verified
  on the Mac (not checkable from the VPS). Supplemental: `grep -rn
  "127.0.0.1:1234\|localhost:1234" src/ config/ .env.example` returns nothing.
  · *Fails if* any profile resolves to an unreachable or wrong-layer endpoint, the
  health probe cannot reach the SLM through Caddy, or a loopback literal survives in
  config-bearing files.
- **AC-5 — A disallowed egress URL is refused on every enumerated production seam.**
  The guard is not merely retained but enforcing, everywhere it is scoped to. ·
  **Check:** an integration test **parameterized over the enumerated egress seams**
  (LLM client, SLM health paths, embeddings, reranker, artifact export, envelope probe,
  web/search tools) drives each seam's real production wiring with allowlist mode
  active and a disallowed domain, asserting refusal *before* any connection is
  attempted, and permission for an allowlisted domain; plus the static check: the
  ast-grep rule set finds zero occurrences of the enumerated bypass forms (httpx client
  construction, module-level `httpx.get/post/request`, direct Anthropic/OpenAI SDK
  client construction) outside the transport factory. · *Fails if* any enumerated seam
  bypasses the guard (today's measured state for all of them), the test exercises the
  guard class directly instead of seam wiring, refusal happens only after a connection
  attempt, or the static scan finds any enumerated bypass form.

**Seam ticket:** filed with the implementation chain (FRE-1143 close-out names it), due
**2026-09-08** — the earliest date all five criteria are adjudicable (both phases merged
+ deployed behind FRE-1142, one recreation cycle observed).

---

## References

- [FRE-1143](https://linear.app/frenchforest/issue/FRE-1143) — the commissioning ticket, with the owner-verified factual baseline
- [FRE-225](https://linear.app/frenchforest/issue/FRE-225) — in-process egress domain guard (retained and wired by D2)
- ADR-0028 — tool integration tiers; parent of the egress guard
- [FRE-411](https://linear.app/frenchforest/issue/FRE-411) — the precedent Caddy block: CF-tunnelled, path-allowlisted ES write endpoint
- [FRE-530](https://linear.app/frenchforest/issue/FRE-530) — artifact export, a phase-2 consumer of the CF service token
- [FRE-1142](https://linear.app/frenchforest/issue/FRE-1142) — inference-path instrumentation; sequencing predecessor
- ADR-0112 — configurable substrate backends; the profile mechanism the D4 matrix rides on
- ADR-0120 — cost governance; part of the retained in-process mitigation for application credentials
- ADR-0130 — two tiers of acceptance criteria; governs the seam ticket
- [Caddy `reverse_proxy` directive](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) — streaming/auto-flush behaviour, `flush_interval` cancellation side effect, transport timeouts, keepalive
- [Filebeat `filestream` input](https://www.elastic.co/docs/reference/beats/filebeat/filebeat-input-filestream) — the supported container-log capture mechanism (legacy `container` input is deprecated)

---

## Status Updates

### 2026-08-05 - D1 implemented (both phases), with a correction to this ADR's inventory
**Changed By:** build session (FRE-1144)
**Reason:** D1 landed in one change rather than the two phases this ADR planned, because the phasing
was not separable. AC-a of the Phase-1 ticket requires the outbound CF pair to be absent from the
gateway process; but `cf_service_token.py` reads the same settings fields, so removing them without
migrating its consumers would have silently broken artifact export, the envelope probe, and the
CF-gated embedding/rerank paths. Phase 1 could not satisfy its own criterion while Phase 2 was
outstanding. FRE-1145 is therefore absorbed by FRE-1144.

**Correction to "What is true today".** This ADR's inventory buckets `memory/embeddings.py` and
`memory/reranker.py` under class 2 (artifact origins) because they call the shared
`cf_service_token.py` helper. Measured, they reach the **SLM tunnel**, not the artifacts origin: both
gate on `settings.slm_tunnel_base_url in endpoint` (`embeddings.py:541`, `reranker.py:194`) and
`reranker.py:115` builds its endpoint from that same setting. The inventory classified by *which
helper a caller invokes* rather than *which upstream it reaches*. The true split is five SLM-tunnel
consumers (`client.py`, `scheduler_runner.py`, `provider_health.py`, `embeddings.py`, `reranker.py`)
and two artifact-origin consumers (`artifacts_router.py`, `artifact_envelope/probe.py`).

**Two implementation decisions worth recording, because both are departures:**

1. **The egress listeners are bound to loopback on the host**, not left compose-network-only as the
   Implementation Notes state. Host-side tooling (eval scripts, `uv run agent`) previously reached the
   tunnel directly using CF headers from `/opt/seshat/.env`; once that credential moves to the
   Caddy-only source, an unpublished listener would leave that tooling with no route at all. A
   `127.0.0.1`-bound binding preserves the route without exposing the listener off-box, and custody is
   unaffected — the credential still lives only in Caddy.
2. **The per-profile endpoint requirement is enforced at boot, not in a model validator.**
   `settings = get_settings()` runs at import scope across the codebase, so a construction-time failure
   would brick every script, CLI entrypoint and diagnostic on a host whose `.env` predates the field —
   including the tooling needed to diagnose it. The dead-default class stays closed regardless: the
   field has no default, so an unset value cannot resolve to something unreachable.

**Still open from this ADR:** D2 (domain-guard wiring behind one transport factory) and D3 (Filebeat
shipping `caddy-access-*`, FRE-1146, which FRE-1144 blocks). The five ACs remain unadjudicated — they
belong to the seam ticket FRE-1148, not to FRE-1144.

### 2026-08-04 - Proposed
**Changed By:** adr session (FRE-1143)
**Reason:** Authored after owner discussion settled all four decisions and the sequencing constraint. Codex round 1 (7 blocking, 3 minor) surfaced a measured finding — the FRE-225 domain guard has zero production callers — adding the guard-wiring obligation. Codex round 2 (7 blocking) corrected the CF credential inventory (the pair also authenticates artifact origins via `cf_service_token.py`, making D1 two-phase), exposed the blanket `env_file` custody hole, aligned D4 to the real profile axes, and tightened all five ACs to be unfakeable. Codex round 3 (4 blocking, 2 minor) narrowed the target state and AC-2 scan to the outbound service-token pair (the inbound JWT surface is retained), restated D3/AC-3 honestly as at-least-once-after-harvest, defined the test-axis SLM fixture value, broadened AC-5's static rule set to the enumerated bypass forms, and added the `model_loader.py` consumer to the deletion inventory.
