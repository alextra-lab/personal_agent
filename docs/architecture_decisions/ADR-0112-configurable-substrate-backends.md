# ADR-0112: Configurable Substrate Backends — Owner-Controlled Storage by Default, Managed API Endpoints by Choice

**Status:** Proposed
**Date:** 2026-07-06
**Deciders:** Project owner
**Tags:** infrastructure, substrate, embeddings, configuration, privacy, personal-agent

**Supersedes:** ADR-0111 (Infrastructure Topology & Data-Custody Policy)
**Amends:** ADR-0105 / FRE-721 (the "always-on private embedding path" becomes a *default profile*, not a hardcoded mandate)
**Related:** ADR-0099 (config management & validation), ADR-0069 (R2 artifact substrate — reused for backups), ADR-0098 (memory substrate & lifecycle)

---

## Context

**What is the issue we're addressing?**

Seshat is a **personal agent**. The data it holds is the **owner's own interactions** — the
same class of data the owner already sends to Claude/ChatGPT every day — not a multi-user or
regulated product. The owner's actual goal, stated plainly: **take the app "off the grid"** —
own the memory / knowledge-graph layer so it is *theirs*, more private than using a frontier
chat product directly — **while keeping the usefulness of frontier models.**

ADR-0111 (this ADR supersedes it) answered a different, larger question than the one the owner
had: it built an enterprise **data-custody fortress** (deny all managed services for personal
data, HDS/BAA compliance framing, a CPU-only on-box → bigger-box → second-box hardware ladder).
For a single-user personal agent whose data already transits a frontier API, that is
**disproportionate** — and it was authored without the collaborative discussion the ADR flow
requires. The extended discussion that produced *this* ADR reframed the problem correctly.

**The distinction that actually matters: storage vs API endpoints.**

- **Storage (data at rest)** — Postgres (memory/sessions), Neo4j (knowledge graph),
  Elasticsearch (telemetry) — is the **persistent private asset** that accumulates into the
  owner's second brain. This is the part worth *owning*.
- **API endpoints (compute in transit)** — the embedder, reranker, harness SLM, and the
  frontier conversation model — are **ephemeral compute** that data passes *through*, not
  where it *rests*. The bar for these is not "own the hardware"; it is **acceptable terms:
  no training on your data + no logging / zero-or-short retention** (the *no-log* clause is
  the load-bearing one — it is what stops an ephemeral call from becoming stored third-party
  data).

**Measured substrate reality (week of `sar`, 2026-06-28 → 07-06).** Host RAM sits ~50% used
with ~10 GiB free; CPU is idle (load ~0.5/8, %steal ≈ 0). The pressure is **not global** — it
is two container caps: the **embedder (2.93 GiB, ~96%)** and **Elasticsearch (2 GiB, ~96%)**.
There is **no swap**, and **%commit peaks > 100%** several days (overcommit tail risk; one
real spike on Jul 4 during reranker testing drove available RAM to 1.72 GiB). So the "binding
constraint" is not host RAM exhaustion — it is the model stack's caps plus an unguarded
overcommit tail. **The embedder is both the #1 RAM consumer and the CPU-latency bottleneck**
(FRE-655), which makes it the natural thing to move *off* the box.

**What needs to be decided.** (1) Where storage lives. (2) The bar for API endpoints. (3) How
the harness expresses backend choice so it serves both the owner (private-by-default) and any
future operator who needs certified/managed backends. (4) The embedder/reranker choice,
grounded in documented retrieval benchmarks and decided on the real corpus. (5) The near-term
substrate fix, now that the embedder can move off-box.

---

## Decision

**Two defaults plus configuration.** Storage is owner-controlled by default; API endpoints may
be managed under acceptable no-train/no-log terms; every substrate component is selectable by
configuration (ADR-0099), private-by-default with a certified/managed profile available.

### D1 — Storage is owner-controlled by default

Postgres, Neo4j, and Elasticsearch run on infrastructure the owner controls (the current OVH
VPS). This is the persistent private layer — the "off the grid" win — and it is **not
third-partied by default**. Managed/certified storage (Elastic Cloud, managed Postgres, an
HDS/SOC2-scoped store) remains **available as a configuration profile** (D3) for an operator
who needs it, but it is never the default and is never required for the owner's own use.

### D2 — API endpoints may be managed, under acceptable terms

The embedder, reranker, harness SLM, and frontier conversation model **may** be hosted managed
endpoints, **provided the terms clear the bar: no training on your data + no logging /
zero-or-short retention.** The frontier conversation model is already the cloud path
(Anthropic) and is unchanged — that is the "frontier usefulness" the owner keeps. This bar is
the same trust the owner already extends to the chat model; it is a *terms* requirement, not a
*locality* requirement.

### D3 — Every substrate component is a configuration choice (ADR-0099)

Each substrate component — store, embedder, reranker, SLM, search/vector index — sits behind a
**clean interface** and is pointable, **per configuration profile**, at a **local/self-hosted**
backend *or* a **managed/certified** backend. The harness **does not hardcode a custody
stance**; the deployment selects it:

- **`private` profile (default):** owner-controlled storage + reasonable-terms API endpoints.
  Private where it counts, frontier smarts where they help.
- **`managed`/`certified` profile (opt-in):** any operator who needs compliance or
  state-of-the-art managed substrate (Elastic Cloud, managed Postgres, HDS/SOC2 endpoints)
  selects it by configuration — same harness, no code change.
- **`dev`/`test` profile:** local, disposable, **isolated** backends — the FRE-375 test
  substrate (Neo4j :7688 / ES :9201 / Postgres :5433), a local embedder, and managed /
  personal-data endpoints **stubbed or disabled by default** — so development and CI never touch
  prod stores, real personal data, or (unless explicitly opted in) paid managed endpoints. A
  dev environment is not a bolt-on; it is the same backend seam with a disposable profile.

This lives inside the ADR-0099 single-source config model; the profile is a config selection
validated by the existing config machinery.

**Resource isolation (not just data isolation).** The dev/test environment must be isolated
from prod in **resources**, not only in data. FRE-375 already isolates the *data* (separate
stores); it does **not** stop test/eval workloads from **contending for RAM/CPU with live
serving** on the same host — the measured Jul-4 spike (reranker testing) drove prod's available
RAM to **1.72 GiB**, a near-OOM caused by dev-on-prod contention. Therefore: the test substrate
runs **off the prod serving host** (relocated / on-demand, torn down when idle — D5), and heavy
test/eval jobs (corpus A/B, embedder/reranker benchmarking, re-embeds) run as **ephemeral
isolated jobs** (the D6 pattern), never on the box that serves live traffic.

### D4 — Embedder / reranker: open-weight SOTA spine, managed-primary with a same-model local fallback, decided by corpus A/B

**Prefer an open-weight state-of-the-art embedder as the spine**, because open weights deliver
retrieval quality *and* a genuine local fallback at once:

- A **seamless fallback requires the *same* model** managed and local — embeddings from
  different models occupy different vector spaces, so a different fallback model means a full
  re-embed and non-comparable vectors. Open-weight models can run identically as a managed
  endpoint *and* self-hosted; closed API-only models cannot.
- **Chosen spine: `Qwen3-Embedding-8B`.** It is **#1 on the MTEB multilingual leaderboard
  (~70.6), ahead of every closed API** (OpenAI ~64.6, Google ~68.3, Voyage-3-large ~65.1), and
  it is **open-weight**. It is served **managed on OVH AI Endpoints** (€0.1 / 1M input tokens,
  32k context, EU/OVH infrastructure — the owner's existing trusted, GDPR/ISO-27701 provider;
  base URL + token already in hand) **with the identical model self-hostable as a local
  fallback** (the owner confirmed they can run an 8B embedder locally). Same model → same vector
  space → failover in either direction with **no re-embed**.
- **Closed API-only models (e.g. Voyage) remain candidates but are not the default:** they
  offer **no seamless local fallback**, and on the public MTEB benchmark they trail
  Qwen3-Embedding-8B. A closed model must **win the owner's corpus A/B by a pre-registered
  margin** (declared before the run) to justify giving up both open weights and the local
  fallback. **Selecting a closed/API-only embedder as the spine explicitly forfeits the
  seamless-failover guarantee** — that profile declares `fallback: none` (an outage degrades
  recall) or `fallback: re-embed-required`, and the AC-6 same-space guarantee below applies
  **only to an open-weight-spine profile.** The open-weight spine is the default precisely
  because it keeps the fallback.
- **Retrieval quality is the ranking priority, and the choice is measured, not asserted:** a
  **corpus A/B** (the FRE-655 methodology — real queries against the real corpus, ranked by
  nDCG) selects the model and size. The reranker follows the same rule (Qwen3-Reranker or a
  local open-weight reranker; A/B-decided).

Adopting Qwen3-Embedding-8B requires a **full re-embed** of the corpus (moving from today's
local `Qwen3-Embedding-0.6B` space to the 8B space). A re-embed is required on **every**
embedder-model change; adoption is the first such event — see D6.

### D5 — Near-term substrate fix: keep stores local, add swap, reclaim the test stack

With the embedder moving to a managed endpoint (D4), the box's RAM pressure is solved **without
buying hardware**:

- **Keep Postgres / Neo4j / Elasticsearch on the VPS** (owner-controlled, D1).
- **Add a few GB of swap.** The `sar` data shows `%commit` peaking > 100% with **no swap**;
  swap absorbs the overcommit tail (the Jul-4-style spikes) for ~$0.
- **Reclaim the co-resident test stack** (~1.57 GiB) off the prod host (relocate, do not delete
  — CI/eval depend on it; `make test-infra-up` must still work). This is **resource-contention
  isolation**, not only RAM reclaim: co-resident test workloads contend with live serving (the
  Jul-4 near-OOM), so the dev/test substrate moves off the serving host / becomes on-demand (D3).
- **No bigger box, no second box, no GPU tier** — the ADR-0111 hardware ladder is dropped;
  removing the embedder is what relieves the box.
- **No second VPS for redundancy.** For a single-user personal agent, the redundancy that
  matters is **durability** (D7 backups, which land *off-box and off-provider* — a total VPS
  loss is a restore onto a fresh box, not data loss), **not availability** (zero-downtime
  failover). A second running VPS buys only zero-downtime failover — recurring cost and a
  second host to maintain, against occasional-downtime-tolerable single-user usage on the cloud
  path. Availability HA stays deferred; if uptime ever becomes critical it is a later
  topology/config change (restore onto a fresh box), not a standing second box now.

### D6 — Ephemeral GPU for the one-way-door batch job; no always-on GPU

No always-on GPU (a 24/7 L4 ≈ €540/month against an idle-CPU box is poor value). For the single
genuinely CPU-bound batch job — a **full re-embed** (a one-way door on any embedder-model
change, including the D4 adoption) — **spin up an owner-account GPU by the hour** (OVH/Scaleway
L4 ≈ €0.75/hr; the ~7.5k-entity corpus re-embeds in minutes, ≈ €2–5) and tear it down. It is
custody-compatible (owner account) and carries no standing hardware. If the embedder is the
OVH-managed endpoint, the provider does the embedding and even this batch largely disappears.

### D7 — Durability (retained from the superseded ADR-0111): restore-tested, custody-bound backups

The one decision from ADR-0111 that survives the reframe unchanged, because the owner endorsed
it and it remains correct: **personal-data stores get restore-tested backups.** Postgres
(PITR/dump), Neo4j (`neo4j-admin dump`), Elasticsearch (snapshot), **encrypted at source with
owner-held keys → R2 as transient ciphertext staging (≤ 8-day window) → owner personal storage**
as the durable resting place. Multi-node HA remains deferred (single-user scale does not need
zero-downtime failover). This is durability, not the custody fortress — losing the memory graph
is irreversible regardless of the topology decision.

---

## Alternatives Considered

### Option 1: The ADR-0111 custody fortress (deny all managed services for personal data)
**Description:** Keep the entire model + storage stack owner-hosted; forbid managed embedding /
managed DB for any personal data; scale via a CPU-only hardware ladder.
**Pros:** maximal theoretical custody; a single simple rule.
**Cons:** disproportionate for a personal agent whose data already transits a frontier chat API;
forces a hardware ladder (bigger box / second box) the owner explicitly does not want to
maintain; forecloses state-of-the-art managed substrate; conflates *storage* custody (which
matters) with *endpoint* locality (which does not, given acceptable terms).
**Why Rejected:** solves an enterprise problem the owner does not have, at real cost in money,
maintenance, and capability. This ADR supersedes it.

### Option 2: All-local — self-host everything, including an always-on 8B embedder
**Description:** Run storage *and* the full model stack (incl. an always-on Qwen3-Embedding-8B)
on owner hardware.
**Pros:** nothing transits any third party.
**Cons:** an always-on 8B embedder on the CPU VPS reintroduces exactly the RAM/CPU pressure
being solved (and worse than the 0.6B); getting SOTA locally means big hardware the owner does
not want to babysit; no cost or quality upside over the OVH-managed same-model endpoint.
**Why Rejected:** high maintenance and resource cost for no gain over managed-primary +
local-fallback of the *same* open model. (Local-8B is retained as the *fallback*, not the
always-on primary.)

### Option 3: Closed API-only embedder (Voyage) as the spine
**Description:** Adopt Voyage voyage-3-large + rerank-2.5 as the default embedder/reranker.
**Pros:** strong retrieval quality (leads on Voyage's own RTEB benchmark); fast reranker.
**Cons:** **closed/API-only → no seamless local fallback** (a different local model = different
vector space = full re-embed on any outage); **trails Qwen3-Embedding-8B on the public MTEB
leaderboard**; its **default ToS licenses your content for training unless you opt out** —
failing the no-train bar out of the box.
**Why Rejected as default:** loses the local-fallback property and the open-weight SOTA lead,
and its default terms miss the bar. Retained as a **corpus-A/B candidate** that must win by a
real margin to be selected.

### Option 4: Managed storage too (managed Postgres / Elastic Cloud) as the default
**Description:** Move the stateful stores to managed services for ops relief and SOTA scale.
**Pros:** SOTA managed substrate; zero storage ops.
**Cons:** storage is the *persistent private asset* — the thing the owner explicitly wants to
own and take off the grid; managed storage moves the accumulating second brain onto
provider-operated infra.
**Why Rejected as default:** contradicts the core goal. **Retained as an opt-in config profile
(D3)** for an operator who wants it — which is the whole point of configurable backends.

---

## Consequences

### Positive Consequences
- **The VPS RAM problem is solved by *removing the embedder*, not buying hardware** — storage
  (what matters) stays owned and the box breathes.
- **Frontier usefulness is preserved** — the cloud conversation path is unchanged; the embedder
  gets an *upgrade* to SOTA (Qwen3-Embedding-8B) at near-zero cost.
- **SOTA retrieval *and* a real local fallback, no trade-off** — the open-weight spine gives
  both; failover needs no re-embed.
- **Custody becomes a config profile, not a hardcoded policy** — the same harness serves the
  owner (private-by-default) and a compliance/SOTA-needing operator (managed/certified profile)
  without a rewrite; it slots into the ADR-0099 config model.
- **The decision is measured** — the embedder is chosen by a corpus A/B, not a leaderboard or a
  vendor's self-benchmark.

### Negative Consequences
- **An API embedder means embedding text transits a third party** — bounded by the no-train/
  no-log terms and the same trust already extended to the chat model; sensitive-data operators
  select a local or VPC/certified embedder by config profile.
- **Adopting Qwen3-Embedding-8B costs one full re-embed** (a one-way door; mitigated by the D6
  ephemeral-GPU batch or the managed endpoint doing it).
- **A same-model local fallback must be the 8B** (not today's 0.6B) to preserve the vector
  space — the fallback is heavier than the current local model, acceptable because it is a
  rarely-hit fallback, not the always-on primary.
- **Two backend profiles to maintain** (local + managed) — a modest, deliberate cost that is the
  price of configurability.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A managed endpoint's terms silently allow training/logging (fails the bar) | High | D2 terms-gate: the config records a no-train/no-log attestation per managed endpoint; a managed profile without it is flagged (AC-3). Confirm the OVH AI Endpoints DPA before adopting. |
| Fallback uses a *different* model → vector-space mismatch, recall collapse | High | D4 mandates the same model both sides; AC-6 asserts managed↔local failover preserves recall on the same index without re-embed. |
| The corpus A/B is skipped and the embedder is picked from a leaderboard | Medium | AC-4 requires a recorded nDCG A/B on the real corpus tied to the decision. |
| Reclaim/swap change destabilizes the box | Medium | Reclaim relocates (not deletes) the test stack; verify `make test-infra-up`; size swap against measured `%commit`; measure before/after (AC-5). |
| Backups exist but were never restore-tested (false confidence) | High | D7/AC-7 require a periodic restore drill; ciphertext-only in R2 within the retention window. |

---

## Implementation Notes

- **The core deliverable is a backend-selection seam** — clean interfaces for store / embedder /
  reranker / SLM behind a config profile (ADR-0099), private-by-default. Additive; no rewrite of
  callers.
- **Embedder adoption is immediately actionable** — OVH AI Endpoints `Qwen3-Embedding-8B` base
  URL + token are already in hand; the local 8B fallback is confirmed feasible on owner hardware.
- **Re-embed** the corpus once on adoption (D6 ephemeral GPU or the managed endpoint).
- **Backups (D7)** reuse the ADR-0069 R2 substrate for the encrypted staging tier.
- **Ticket reconciliation:** the ADR-0111 implementation tickets (FRE-810–815, Needs Approval)
  are superseded in part — the custody-guard (FRE-812) and hardware-ladder trigger/split
  (FRE-813/815) are obsolete; the per-service baseline (FRE-810) and backups (FRE-814) survive.
  This ADR files a corrected set (see handoff); master + owner reconcile the old tickets.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — Storage resolves to owner-controlled hosts by default.** **Check:** in the `private`
  profile, each resolved Postgres / Neo4j / Elasticsearch target matches an **allowlist of
  owner-controlled hosts** (loopback or the owner's declared host / private-IP range in config);
  validation fails a target outside the allowlist. *Fails if* a default-profile store resolves
  to any host not on the owner allowlist (e.g. a provider/managed hostname passed off as owned).
- **AC-2 — *Every* substrate component swaps by config profile — not just the embedder.**
  **Check:** for **each** pluggable component named in D3 — store(s), embedder, reranker, SLM,
  **and search/vector index** — the harness boots and serves under a `local` profile *and* a
  `managed` profile (or, where only one backend is currently wired, a config test shows a second
  profile resolves through the same interface with **no code edit**). *Fails if* any D3-listed
  component is hardcoded such that changing its backend needs a code change (an embedder-only
  seam, or one that omits the search/vector index, must fail this).
- **AC-3 — The managed-endpoint terms-gate enforces the D2 bar, not a bare boolean.** **Check:**
  a managed personal-data endpoint profile records **structured** terms — provider,
  `trains_on_data: false`, `retention: none | <=30 days`, and a source link/date — and config
  validation **refuses** a managed endpoint whose `trains_on_data` is not false or whose
  `retention` is unbounded or **exceeds 30 days** (the short-retention bound; 30 days is the
  standard abuse-monitoring window — anything longer is not "short"). *Fails if* a managed
  endpoint with training enabled, retention > 30 days / unbounded, or an empty/boolean-only
  attestation is accepted.
- **AC-4 — Embedder chosen by corpus A/B; a closed model must clear a pre-registered margin.**
  **Check:** a recorded corpus A/B (fixed real-query set, nDCG@k) exists and the selected
  embedder is its measured winner; **if a closed/API-only model is selected, its nDCG exceeds
  the best open-weight candidate by the margin declared before the run**, else the open-weight
  spine is retained. *Fails if* there is no A/B artifact, or a closed model is adopted without
  clearing the pre-registered margin (a noise-level win).
- **AC-5 — The embedder is actually off the host (its specific relief), separable from swap /
  reclaim.** **Check:** with the embedder profile pointed at the managed endpoint, **no embedder
  container runs on the host**, and host free RAM rises by **≥ the embedder's former resident
  footprint (~2.8 GiB) attributable to its removal**; swap-present and test-stack-reclaimed are
  verified as **separate** sub-checks, and `%commit` under the standard workload is **< 100%**.
  *Fails if* the embedder still runs locally (RAM freed by *its* removal ≈ 0) even when swap /
  reclaim independently improved headroom.
- **AC-6 — The local fallback is provably the same vector space (not just the same name; scoped
  to the open-weight spine).** **Check:** (a) the managed and local embedder configs **pin the
  same model identity** — exact weights revision, output dimension, normalization/pooling; and
  (b) a fixed probe set of **≥ 50 inputs** embedded through both endpoints yields **pairwise
  cosine ≥ 0.999** (same input, two endpoints), and a failover retrieval over the *existing*
  index preserves **top-k (k=10) overlap ≥ 0.95** on a fixed query set — **with no re-embed**.
  *Fails if* revision/dimension/normalization differ, probe cosine drops below 0.999, or top-k
  overlap falls below 0.95 (a different-space fallback). (Not applicable to a closed/API-only
  spine profile, which per D4 declares `fallback: none`.)
- **AC-7 — Backups restore-tested AND custody-bound (at ADR-0111's strength).** **Check:** a
  scheduled backup for PG + Neo4j (+ ES snapshot) plus a periodic **restore drill** into a
  scratch instance verifies usability (counts / sample-query match source); a fetched R2 backup
  object is **ciphertext-only** (undecryptable without the owner key, which is **not** retrievable
  from the provider tier), and the R2 prefix holds **≤ 7 daily objects, none older than 8 days**.
  *Fails if* backups are never restore-verified, an R2 object is readable plaintext or its key is
  provider-retrievable, or R2 accumulates beyond the window.
- **AC-8 — The dev/test environment does not contend with prod serving — by mechanism *and*
  outcome.** **Check:** during a representative heavy test/eval job (corpus A/B or
  embedder/reranker benchmark): **(mechanism)** no test-substrate container (`*-test` Neo4j / ES
  / Postgres) and no eval/benchmark process runs on the prod serving host — the substrate is
  off-host / on-demand and the job is an ephemeral off-host job; **(outcome)** prod host
  available RAM stays **≥ 3 GiB (the floor)** throughout the run. *Fails if* a test-substrate
  container or eval process is present on the serving host during the run, or prod available RAM
  dips below 3 GiB (a light sample or spare RAM masking on-host contention must not pass).
- **AC-9 — The dev/test profile is isolated from prod data and live paid endpoints.** **Check:**
  under the `dev`/`test` profile, resolved store targets are the **FRE-375 test substrate**
  (Neo4j :7688 / ES :9201 / Postgres :5433), **not** the prod stores; and managed / personal-data
  / paid endpoints are stubbed or disabled (a CI run makes **no live paid embedding/inference
  call**). Reuses the FRE-375 AppConfig guard (refuses prod-fingerprint URIs when
  `APP_ENV=test`). *Fails if* the dev/test profile resolves to a prod store, a real
  personal-data store, or issues a live paid endpoint call.

**Seam owner (assembled intent).** The decision holds only when the **full backend-selection
seam (AC-2, all components) + the enforced terms-gate (AC-3) + the measured embedder choice
(AC-4) + the same-space fallback (AC-6)** all land — a merged config change that boots one
profile does not prove the assembled intent. **Master asserts the seam at the integration
gate**; the ADR does not close on the last child alone.

---

## References

- Supersedes: ADR-0111 — Infrastructure Topology & Data-Custody Policy
- Amends: ADR-0105 / FRE-721 — private embedding path (now a default profile, not a mandate)
- ADR-0099 — Configuration Management & Validation (the config model this rides on) — Accepted
- ADR-0069 — Artifact Substrate (R2; reused for the D7 encrypted backup staging) — Implemented
- ADR-0098 — Memory Substrate & Lifecycle — Accepted
- FRE-809 — this ADR's umbrella ticket (owner-directed)
- FRE-655 — embedder A/B methodology (corpus, real queries, nDCG)
- OVH AI Endpoints — Qwen3-Embedding-8B: https://www.ovhcloud.com/fr/public-cloud/ai-endpoints/catalog/qwen3-embedding-8b/ (€0.1/1M tokens, 32k context, EU)
- Qwen3-Embedding (MTEB #1, open-weight): https://qwenlm.github.io/blog/qwen3-embedding/
- Embedding/reranker benchmark + terms research (2026-07-06): MTEB leaderboard; Voyage ToS training-opt-out; Jina/Cohere terms — captured in the FRE-809 discussion thread

---

## Status Updates

### 2026-07-06 - Proposed
**Changed By:** adr session (Opus), FRE-809
**Reason:** Supersedes ADR-0111 after extended owner discussion reframed the problem from an
enterprise custody fortress to a personal-agent, storage-vs-endpoint, configurable-backends
decision. Core decisions settled with the owner in-session: owner-controlled storage by default;
managed API endpoints acceptable under no-train/no-log terms; config-selectable backends
(ADR-0099); embedder = OVH-managed `Qwen3-Embedding-8B` (SOTA + open-weight + EU) with a
same-model local fallback, chosen by corpus A/B; stores stay on the VPS + swap + reclaim (no
hardware ladder); ephemeral GPU only for the re-embed; restore-tested custody-bound backups
retained from ADR-0111. **Transition rule:** status becomes **Accepted** on merge (owner-ratified
in-session); **Implemented** only when the assembled seam (AC-2 + AC-3 + AC-4 + AC-6) is proven.
