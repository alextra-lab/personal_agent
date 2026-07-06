# ADR-0111: Infrastructure Topology & Data-Custody Policy (CPU-only single-box baseline; owner-controlled personal-data plane)

**Status:** Superseded by [ADR-0112](ADR-0112-configurable-substrate-backends.md) (2026-07-06)
**Date:** 2026-07-06
**Deciders:** Project owner
**Tags:** infrastructure, topology, capacity, data-custody, threat-model, ovh, csirt, cost

> **Superseded (2026-07-06).** Extended owner discussion reframed this decision. Seshat is a
> personal agent whose data already transits a frontier chat API; the enterprise custody
> fortress here (deny all managed services, HDS/BAA framing, a CPU-only hardware ladder) was
> disproportionate, and this ADR was drafted without the required collaborative discussion. The
> correct decision — owner-controlled *storage* by default, managed *API endpoints* under
> no-train/no-log terms, configurable backends, an open-weight SOTA embedder (OVH-managed
> Qwen3-Embedding-8B with a same-model local fallback) — is **[ADR-0112](ADR-0112-configurable-substrate-backends.md)**.
> The one decision retained from here is durability (restore-tested, custody-bound backups),
> carried into ADR-0112 §D7.

**Related:** ADR-0105 (isolated System graph; private embedding path), ADR-0098 (memory substrate & lifecycle), ADR-0074 (observability / substrate guard pattern), ADR-0069 (artifact substrate)

---

## Context

**What is the issue we're addressing?**

Seshat runs as ~13 co-tenant Docker containers on a single OVH VPS (the `cloud-sim-*`
compose project). This ADR decides three separable, strategic questions that together gate
**every future scaling spend** — capacity/topology, data-custody, and redundancy — so the
owner has a decision-ready artifact rather than making capacity, trust-boundary, and
data-loss calls reactively, one purchase at a time. None of the three
block the current pipeline; all three shape what the next dollar and the next byte
of personal data are allowed to do.

**Measured starting point (live, 2026-07-06, this session).**

- Host: **8 vCPU (Intel Haswell-gen; AVX2 + FMA + F16C, no AVX-512), no GPU. 22 GiB RAM,
  ~10 GiB available, load average 0.72 on 8 cores.** CPU is mostly idle; **RAM headroom is
  the binding constraint**, not core count.
- The pressure is **concentrated, not global.** Two containers sit pinned near their caps:
  **Elasticsearch at 96.6% of its 2 GiB cap** and the **embedder (`cloud-sim-embeddings`)
  at 96.5% of its 2.93 GiB cap**. Reranker (568 MiB / 2.5 GiB) and Neo4j (839 MiB / 1.5 GiB)
  have headroom; **Postgres is tiny (66 MiB / 512 MiB — stock defaults**, right-sized by the
  parked FRE-718).
- A **duplicate test stack** (`seshat-elasticsearch-test` 932 MiB + `seshat-neo4j-test`
  587 MiB + `seshat-postgres-test` 50 MiB ≈ **1.57 GiB**) is co-resident on the prod host
  and reclaimable.
- Co-tenants sharing the box: Postgres, Neo4j, Elasticsearch, Kibana, embedder (:8503),
  reranker (:8504), the SLM (llamacpp :8502), gateway, PWA, Caddy, SearXNG, Redis,
  cloudflared.

**Fixed constraint (owner).** **No GPU.** The inference tier stays CPU-only. Topology options
are limited to on-box optimization, a vertically larger CPU/RAM host, or a second CPU host
for the model-serving plane. GPU acceleration is explicitly out of scope.

**What is at stake — the data.** Seshat holds the owner's **private memory + knowledge graph,
session content, and (planned) medical records.** The owner is a SOC/CSIRT practitioner; the
custody posture of this data is a first-class design concern, not an afterthought. The
architecture already fixes the embedding path to the **always-on private VPS path**
(ADR-0105 / FRE-721 — never a third-party API and never a laptop/Mac tunnel).

**Forces at play.**

- **Cost** — single-user, no revenue; every recurring dollar is the owner's. HA/redundancy
  sized for a service with users is overkill here.
- **Custody** — private + medical data on infra the owner does not administer is a trust
  downgrade that no ops convenience justifies for this data class.
- **Availability** — the VPS is always-on and reliably reachable; a home/laptop machine is
  not (sleep, residential uplink, travel). The synchronous hot path (embed-on-write,
  recall-time rerank/generate) depends on always-on reachability.
- **Ops burden** — one person operates everything; a topology that adds an inter-box network
  and a second host to patch is a real, recurring cost, not free horizontal scale.
- **Durability & redundancy** — the stateful stores are **distributed-by-design** systems
  (Elasticsearch especially) currently run **single-node**, i.e. in a knowingly degraded mode
  with **no replica and no failover**. This forces a distinction that must not be blurred:
  **durability** (never *lose* the data — a backup concern) is separate from **availability**
  (survive a node dying without downtime — a multi-node concern). For irreplaceable personal +
  medical data, durability is non-negotiable; availability/HA is the part that is debatable at
  single-user scale. Critically, **a replica or standby on the *same host/disk* shares the
  failure domain and protects against nothing — real redundancy requires separate boxes**, so
  it is a distinct driver for the multi-box topology, not something the single box can provide.

**What needs to be decided.** (1) A **capacity/topology growth path with a measurable
trigger** under the CPU-only constraint. (2) A **durable data-custody policy** — an explicit
allow/deny rule for managed services (OVH managed Postgres/DBaaS, OVH managed embedding /
AI Endpoints) that survives future temptation, stated so it cannot be quietly eroded one
convenient offload at a time. (3) A **redundancy posture** — what durability (backups) is
mandatory now versus what availability (multi-node HA) is deferred, per store, and where
backups of personal data may legally rest under the custody rule.

---

## Decision

Adopt a **single-box, CPU-only baseline** with **trigger-gated escalation**, and govern
where personal data may live with a **data-class custody rule** that keeps the personal-data
plane on owner-controlled infrastructure and denies managed/multi-tenant services for it.

### D1 — Topology: on-box first, then trigger-gated escalation (CPU-only)

The box is **not globally starved** — CPU is idle (load 0.72/8) and the constraint is a
**concentrated RAM-cap pressure** on two containers. The growth path is therefore ordered
cheapest-and-least-invasive first:

- **Step 0 — Baseline: a single owner-controlled CPU-only OVH host.** No GPU. This is the
  status quo and remains the default.
- **Step 1 — On-box optimization (near-term, ~$0).** Reclaim the co-resident test stack
  (~1.57 GiB), raise the embedder RAM cap from the freed headroom, and apply FRE-718
  Postgres tuning. This is expected to resolve the current pressure without any hardware
  spend. **Do this first; measure before and after.**
- **Step 2 — Vertical scale-up (on trigger).** Move to a **larger single OVH host — a bigger
  VPS *or* an OVH bare-metal dedicated server.** At the RAM ceiling, **bare metal is
  preferred**: it is single-tenant physical hardware, so it both lifts the RAM ceiling and
  is a **custody upgrade** (no shared hypervisor, no noisy-neighbour CPU steal — relevant on
  this Haswell VPS) at a predictable fixed monthly price.
- **Step 3 — Horizontal split (on trigger, if vertical is exhausted or blast-radius argues
  for it).** Split the **stateless model-serving plane (embedder + reranker + SLM)** onto a
  **second owner-controlled OVH host (VPS or bare metal) in the owner's own account**, over a
  **private, encrypted inter-box link (OVH vRack and/or WireGuard)**. The **stateful stores
  (Postgres, Neo4j, Elasticsearch) stay on the primary host.** The split boundary and its
  channel-security requirement are specified in **D4**.

The **home / unified-memory machine** the owner raised (a large-unified-memory Mac or home
server) is **deferred to future-work** (see Consequences) — it is owner-controlled and thus
custody-eligible, but it fails the availability requirement for the synchronous hot path, so
its only legitimate role is async/batch/offline compute, revisited if and when acquired.

### D2 — Growth trigger (RAM-floor + sustained saturation)

Escalation from Step 1 to Step 2/3 is a **fired condition, not a guess.** After on-box
reclaim is complete, add hardware when **any** of the following holds on a **sustained** basis
(evaluated over ≥ 7 days from the `sar`/host-metric series, so a transient spike does not
trip it):

- **host available RAM < 3 GiB** for more than a cumulative **24 h per rolling week**, OR
- the **embedder sustained ≥ 90% of its cap AND** its p95 embed latency breaches the recall
  latency budget, OR
- **Elasticsearch heap pressure** forces a cap raise for which there is **no free host RAM**.

Until a trigger fires, the answer is **on-box only** — no speculative hardware. The trigger
must be **instrumented and observable** (AC-4), not left as prose in this ADR.

### D3 — Data-custody policy: the data-class rule

**Classify every store and every compute step by whether it touches personal data; the
custody rule is stated by *data class*, not by service name.**

- **Personal-data plane** — memory (Neo4j KG; Postgres memory), sessions/messages, planned
  medical records, **embeddings/vectors derived from personal text** (a lossy but partially
  reconstructable projection of the plaintext, produced by reading personal input), the
  **embedder/reranker/SLM compute whenever it operates over personal text**, the isolated
  System graph (`sysgraph`, derived from the owner's own usage — treated as personal by
  conservative default), and telemetry/traces that may embed personal content.
- **Non-personal plane** — content provably free of personal data: public web-search snippets
  before personalization, container images, configuration, source code, and aggregate
  cost/token counters carrying no content.

**The rule:**

The rule separates **two distinct axes** — *who administers the box* and *what data class it
holds* — so they are never conflated:

1. **Owner-administered infrastructure is allowed for personal data.** A VPS or bare-metal
   server the owner administers **in their own account** may hold the personal-data plane —
   including a **second owned box reached over a private encrypted link** (the custody boundary
   is **tenancy + administrative control**, not the physical box). Tenancy nuance does not
   further constrain this path: an owner-administered box *is* the trusted zone.
2. **Managed / multi-tenant services are DENIED for the personal-data plane** — this includes
   **OVH managed Postgres / DBaaS**, **OVH managed embedding / AI Endpoints**, and any
   third-party inference or embedding API. These place personal data (or personal-text
   compute) on a provider control plane the owner does not administer — provider staff access,
   provider key management, provider backups, plus a network hop across a trust boundary on
   every hot-path call.
3. **A managed service is a candidate ONLY for a store/compute proven to hold *zero* personal
   data**, and even then **only single-tenant in the owner's own account** (this tenancy
   constraint applies to *managed services*, not to the owner-administered path of rule 1).
   Because Postgres, Neo4j, and Elasticsearch each co-mingle personal content today, **no
   current store qualifies** — the managed-service door is closed for Seshat as it stands.

*Boundary in one line:* **owner-administered box → OK for personal data; managed service → OK
only for provably-non-personal data, single-tenant in the owner's account; everything else →
deny.**

**Explicit determinations (the two the owner asked about):**

- **OVH managed Postgres (DBaaS) → DENIED.** All current Postgres content is personal
  (memory, sessions, `sysgraph`). DBaaS would move the most sensitive stores onto
  provider-operated infra and add a per-recall network hop — a custody downgrade and a
  hot-path latency cost — to relieve an ops burden that barely exists (Postgres is 66 MiB on
  stock defaults). Note the scope of "overkill": it is the managed *availability/HA* that is
  unjustified at single-user scale — **durability is still mandatory** and is met under D5/D6
  by custody-bound, restore-tested backups, not by DBaaS.
- **OVH managed embedding / AI Endpoints → DENIED for the personal path.** Embedding the
  owner's memory/KG/medical text on a multi-tenant API is personal-data compute across an
  unadministered trust boundary — the worst-case egress. This **reaffirms ADR-0105 / FRE-721**
  (fixed always-on private embedding path); it does not reopen it.

### D4 — Horizontal-split boundary & inter-box channel security

When Step 3 executes, the split is **stateless model-serving plane on the second box;
stateful stores on the primary.** Because the embedder/reranker/SLM operate over personal
text, personal data now crosses the inter-box link. That is permitted **only** because both
boxes are owner-controlled, **and only over a private, encrypted channel**: the second box's
service endpoints must resolve to a **private address (vRack / WireGuard)** and carry
**personal text encrypted in transit** — never a public interface, never plaintext
(AC-5). No personal-data *store* moves to the second box under this ADR.

### D5 — Redundancy: durability-first now, availability (multi-node HA) deferred

**Durability is mandatory now; availability/HA is deferred behind a separate failure-domain
trigger and weighed per store.** Losing the memory graph or medical records is irreversible;
zero-downtime failover is a convenience that single-user scale does not yet justify.

- **Durability (backups) — required now, for every personal-data store.** Postgres via
  WAL archiving / PITR; Neo4j via scheduled `neo4j-admin dump` (or volume snapshot);
  Elasticsearch via snapshot. Backups are **custody-bound** (see D6) and **restore-tested**,
  not fire-and-forget — an unrestored backup is false confidence, so a periodic restore drill
  is part of the deliverable (AC-7).
- **Availability (multi-node HA) — deferred, a distinct multi-box driver.** Real redundancy
  needs **separate failure domains (separate boxes)** and multiplies the binding RAM
  constraint (a 3-node ES cluster ≈ 3× RAM), so it cannot be delivered on the single box and
  is not served by the D2 capacity trigger. It is deferred until a **failure-domain trigger**
  (a node loss actually causing unacceptable downtime, or medical-grade availability becoming a
  stated requirement) and then weighed **per store**:
  - **Postgres** — streaming replication + failover (e.g. Patroni) onto a second owned box is
    feasible when one exists; until then, PITR backups cover the durability need.
  - **Elasticsearch** — native multi-node, but it holds **reconstructable telemetry** (logs /
    traces / insights / captures), *not* the system of record, so **snapshot-for-durability is
    sufficient** and full multi-node is the lowest-priority redundancy spend.
  - **Neo4j** — clustering is **Neo4j Enterprise-only**; the deployment runs **Community**
    (single-instance, offline backup only). **Decision: stay on Community + scheduled,
    custody-bound, restore-tested backups.** Enterprise is surfaced but not adopted: it is a
    **quote-based commercial subscription (no small fixed fee; enterprise-tier)**, and its
    managed alternative (AuraDB) is multi-tenant and **custody-disqualified** by D3. For a
    single-user private KG, Enterprise clustering is very likely cost-disproportionate — the
    ADR documents what it would buy (causal clustering + online backup) so the owner can
    request a quote *if* KG availability ever becomes critical, but the standing decision is
    Community + backup.

### D6 — Backup topology: encrypted-at-source → R2 staging (ciphertext) → owner personal storage

Backups of personal data are subject to the same custody rule as live data: **a backup at
rest on third-party infra is personal-data egress unless it is client-side encrypted with
owner-held keys** (then the provider holds only ciphertext). The adopted topology is a
**staged pipeline**:

1. **Encrypt at source** — the backup is encrypted **before it leaves the host**, with
   owner-held keys. Plaintext personal data never leaves owner-controlled infra.
2. **R2 as transient ciphertext staging** — the encrypted blob is pushed to R2 (reusing the
   ADR-0069 substrate) for **immediate off-site protection**. R2 holds **only ciphertext** and
   **only transiently** — the **rotation window is at most 7 daily backups and no object older
   than 8 days** — never the plaintext and never the permanent archive.
3. **Rotate down to owner personal storage** — the durable resting place is the owner's own
   storage (owner-controlled); R2 copies are pruned as they rotate down (within the window
   above), so the third-party tier is a staging buffer, not a residency.

**Key custody:** the encryption keys are generated, held, and rotated **only under owner
control, outside the provider account** — R2 (or any third-party tier) **never** stores the
decryption key. A provider with the ciphertext must never also have the means to read it.

This gives off-site disaster-recovery immediacy **without** parking personal data (even
ciphertext) permanently on a provider — and it is verifiable: no R2 backup object is ever
readable plaintext, and R2 does not accumulate beyond its window (AC-8).

---

## Alternatives Considered

### Option 1: OVH managed Postgres (DBaaS) for ops relief
**Description:** Move Postgres (memory, sessions, `sysgraph`) to OVH's managed database
service for automated HA, backups, and patching.
**Pros:**
- Backups, failover, and version patching leave the owner's plate.
- Provider-operated durability guarantees.
**Cons:**
- Personal + medical data on a provider control plane (staff access, provider key management)
  — a custody downgrade for the most sensitive data.
- A network hop on **every recall query** — hot-path latency the loopback path does not pay.
- HA/redundancy is overkill for a single-user system; ops burden being solved is near-zero
  (66 MiB, stock defaults).
**Why Rejected:** trades a real custody downgrade for a barely-existent ops problem. The
durability that DBaaS would provide is genuinely wanted — but D5/D6 obtain it via
custody-bound, restore-tested backups without the egress. Contradicts D3.

### Option 2: OVH managed embedding / AI Endpoints (or any third-party embedding/inference API)
**Description:** Offload the RAM-heavy embedder (and possibly rerank/generation) to a managed
API, freeing local RAM and enabling larger models.
**Pros:**
- Removes the single largest RAM consumer from the box; access to bigger models without local
  compute.
**Cons:**
- Multi-tenant API processing the owner's personal text = maximal personal-data egress across
  an unadministered boundary.
- Breaks the ADR-0105 / FRE-721 always-on private embedding path; adds third-party
  availability + per-call latency as a hot-path dependency.
**Why Rejected:** it is precisely the personal-data-compute-across-an-unadministered-boundary
the custody rule forbids. Contradicts D3.

### Option 3: Home / unified-memory machine as the primary personal-data + inference host
**Description:** Host the primary memory/KG and heavy inference on a large-unified-memory
machine at home, with the VPS as an always-on edge/cache in front.
**Pros:**
- Large unified RAM for a one-time cost; owner-controlled; can run much bigger models than the
  CPU VPS.
**Cons:**
- Not always-on (sleep, residential uplink, travel) — fails the availability requirement for
  the synchronous hot path (embed-on-write, recall-time rerank/generate).
- Makes the core dependent on a residential connection; introduces cache-coherence and
  home-unreachable failure modes.
- ADR-0105 / FRE-721 already barred the laptop/Mac tunnel from the embedding path for this
  reason.
**Why Rejected (as primary):** availability. **Deferred, not dismissed** — retained as a
future async-only adjunct (bulk re-embed for model migration, offline eval, consolidation, a
bigger-model quality tier), where its non-always-on nature is acceptable.

### Option 4: Do nothing — stay single-box at current caps
**Description:** Keep the status quo and react when something breaks.
**Pros:** zero effort now.
**Cons:** the embedder and ES are already pinned at ~96% of their caps; a model upgrade or
medical-records ingestion will breach headroom; the failure mode is a hard OOM, not graceful
degradation.
**Why Rejected:** the pressure is measured and imminent; a defined trigger and the ~$0 on-box
reclaim are cheap insurance against a reactive firefight.

### Option 5: Vertical-only — a bigger box, never a second one
**Description:** Always solve growth by moving to a larger single host; never split.
**Pros:** simplest operationally — one box, loopback only, no inter-box network.
**Cons:** one box has a RAM ceiling even in bare metal; concentrates all blast radius on a
single host and a single failure domain.
**Why Rejected (as an absolute):** vertical is the *preferred* next step (D1 Step 2), but
foreclosing the horizontal split entirely removes a legitimate blast-radius and
ceiling-relief option. The split stays available behind the D2 trigger.

### Option 6: Full multi-node HA now (cluster ES / Postgres / Neo4j from day one)
**Description:** Run the distributed stores as real multi-node clusters immediately —
2–3-node ES, a Postgres primary+standby with failover, Neo4j Enterprise clustering.
**Pros:**
- Zero-downtime survival of a single node failure; the stores run in their intended mode.
**Cons:**
- Multiplies the binding RAM constraint (≈ N×) and forces the multi-box topology
  immediately — the opposite of the on-box-first path.
- Neo4j clustering requires a paid Enterprise subscription; recurring license cost for a
  single user.
- Solves availability, which single-user scale does not yet need, while durability (the real
  need) is met far more cheaply by backups.
**Why Rejected (as *now*):** premature and cost-disproportionate; deferred behind the D5
failure-domain trigger and weighed per store rather than adopted wholesale.

### Option 7: Single-node with no redundancy at all (no backups, no HA)
**Description:** Accept the current single-node stores as-is with no backup strategy.
**Pros:** zero effort.
**Cons:** a disk fault, corruption, or a bad migration **irreversibly loses** the memory
graph, sessions, and medical records — there is no recovery path.
**Why Rejected:** irreversible personal-data loss is the one outcome no cost saving justifies;
D5 makes custody-bound, restore-tested backups mandatory.

---

## Consequences

### Positive Consequences

- **A ~$0 near-term path** — on-box reclaim + cap bump + FRE-718 resolves the measured
  pressure with no hardware spend.
- **A durable custody rule** stated by data class, so it survives future convenience
  temptations and is enforceable in code (AC-2), not just prose.
- **Bare metal reframed as a custody + capacity dual-win** — the vertical step upgrades trust
  posture (single-tenant, no steal) rather than merely buying RAM.
- **The escalation trigger removes guesswork** — "time to add a box" becomes an observable
  fired condition, and the instrumentation for it closes part of the known
  substrate-observability gap.
- **CSIRT-grade boundary is documented** — the personal-data plane and its trust boundary are
  explicit, auditable, and testable.
- **Durability is separated from availability and made mandatory** — irreplaceable data gets a
  restore-tested backup now; expensive HA is deferred to when a real failure-domain need
  appears, not bought speculatively.
- **The staged backup topology gives off-site DR without permanent third-party residency** —
  R2 as encrypted transient staging, owner storage as the durable home, reusing existing
  substrate.

### Negative Consequences

- **On-box reclaim is a one-time ops task** (stop/relocate the test stack off the prod host,
  re-tune caps) with a small risk window.
- **A second box adds a private-network + inter-box-TLS surface** to operate and monitor —
  horizontal scale is not free.
- **Deferring the home box** leaves a cheaper-compute option unused for now.
- **The custody rule permanently forecloses convenient managed offloads** for the
  personal-data plane — an intentional trade of ops convenience for custody.
- **Backups add recurring ops** — a backup + restore-drill schedule and key management for the
  encrypted staging pipeline are a standing (if small) burden.
- **Single-node availability is accepted, knowingly** — until a failure-domain trigger fires, a
  node loss means downtime-until-restore, and Neo4j Community has no online backup (a bounded
  RPO gap between dumps).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| On-box reclaim removes the test stack that CI/eval depends on | Medium | Relocate the test stack (its own host/compose or on-demand bring-up), don't just delete it; verify `make test-infra-up` still works post-reclaim |
| Raising the embedder cap over-commits RAM and triggers OOM elsewhere | Medium | Bump against the measured freed headroom (AC-3 floor), not blindly; keep a RAM floor in the trigger (D2) |
| The custody rule is documented but not enforced, so a future change quietly wires a managed URI | High | Enforce in code: a config/startup guard rejects managed/third-party hostnames on personal-data connections (AC-2), mirroring the FRE-375 substrate guard |
| Horizontal split leaks personal text over the inter-box link in plaintext or on a public interface | High | D4 mandates private (vRack/WireGuard) + encrypted-in-transit; AC-5 asserts it before the split is considered done |
| The growth trigger never fires because it is not instrumented, so escalation is reactive | Medium | AC-4 requires an emitted, alarmable signal for **all three** D2 branches (RAM floor, embedder saturation + latency, ES heap-pressure/no-free-RAM) |
| Backups exist but are never restore-tested (false confidence — the classic backup failure) | High | AC-7 requires a periodic restore drill into a scratch instance; a backup that has never been restored does not count as durable |
| An encrypted backup leaks as plaintext, or R2 accumulates personal ciphertext indefinitely | High | D6 encrypts at source (owner-held keys) before any upload; AC-8 asserts no R2 backup object is readable plaintext and R2 does not retain beyond its rotation window |
| Neo4j Community RPO gap (data written between dumps is lost on a node failure) | Medium | Set the dump cadence to the acceptable RPO; surface Enterprise (online backup) as the escalation if the gap becomes unacceptable |

---

## Implementation Notes

- **The policy itself is a governance + doc artifact plus a few small, additive code
  deliverables** — no `src/` behaviour change is required to *decide* topology or custody. The
  code seams are the custody guard (AC-2), the trigger emitter for all three branches (AC-4),
  and the backup + restore-drill pipeline (AC-7/AC-8); each is small and additive. The reclaim,
  cap bump, and baseline (AC-3/AC-6) are ops/measurement tasks, not `src/` changes.
- **Measurement-first.** The per-service footprint baseline (AC-6) precedes the reclaim so
  before/after is provable, not asserted.
- **Reuse existing instrumentation.** `sar` (2-min sampling, 31-day history) and `docker
  stats` already exist for RAM/CPU; ES already ingests `metrics.sampled`. The custody guard
  reuses the FRE-375 / AppConfig validation pattern (raise on a disallowed URI).
- **Backups reuse the R2 substrate (ADR-0069)** for the encrypted staging tier; encryption is
  client-side with owner-held keys, and the rotation to owner personal storage is a scheduled
  prune. This is adjacent to — but does not resolve — the missing ILM/retention policy in the
  substrate-observability gap thread; retention/lifecycle stays a related concern, referenced
  not decided here.
- **Cross-refs to honour:** FRE-718 (Postgres tuning — parked; referenced, not recreated),
  FRE-721 (private embedding path — reaffirmed), the substrate-observability/data-lifecycle
  gap thread (AC-4/AC-6 close part of it; backups touch the lifecycle half).
- **Tickets (filed Needs Approval, sequenced):** per-service footprint baseline → reclaim test
  stack + embedder cap bump → custody guard → growth-trigger instrumentation → custody-bound
  backup + restore-drill (PG + Neo4j + ES snapshot) with the R2-staging→personal-storage
  rotation → (deferred, on trigger) horizontal-split design. Sequencing in the handoff comment.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

- **AC-1 — The personal-data plane physically resolves to owner-controlled hosts.**
  **Check:** enumerate the gateway's resolved connection targets — `AGENT_DATABASE_URL`
  (Postgres), the Neo4j bolt URI, the Elasticsearch URL, and the embedder/reranker/SLM base
  URLs — and assert every one is a loopback or an owner-controlled OVH host (owner's account /
  vRack private IP). *Fails if* any personal-data connection string resolves to a managed-service
  or third-party endpoint (e.g. a `*.dbaas.ovh.*`, an AI-Endpoints host, or any non-owner API).
- **AC-2 — The managed-service deny is enforced in code across *every* personal-data connection
  type, not only documented.**
  **Check:** a **parameterized deny/allow matrix** test drives each personal-data connection —
  Postgres, Neo4j, Elasticsearch, **and** each model endpoint (embedder / reranker / SLM) —
  with representative managed/third-party host patterns (`*.dbaas.ovh.*`, an AI-Endpoints host,
  a generic third-party API host) and asserts startup **raises** for every one, while an
  owner-controlled/loopback/vRack host is **accepted** (mirrors the FRE-375 / AppConfig
  substrate guard). *Fails if* any single personal-data connection class silently accepts a
  managed/third-party URI — a guard that blocks only the DB but not the model endpoints (or
  vice-versa) must fail this AC.
- **AC-3 — On-box reclaim produced real headroom under load, not at idle.**
  **Check:** while driving the **standard recall/embed smoke workload** (the existing eval/A-B
  driver) for **≥ 10 minutes**, the embedder's **p95 container memory stays < 85% of its (new)
  cap** AND host available RAM (`free`) **stays ≥ the D2 floor (3 GiB)** throughout the window.
  *Fails if* the embedder p95 is ≥ 90% of cap, or available RAM dips below the floor during the
  workload, or the measurement is taken at idle rather than under the named workload (a no-op
  reclaim would pass an idle reading but fail under load).
- **AC-4 — Every D2 trigger branch is an emitted, alarmable signal (not just the RAM one).**
  **Check:** all three D2 conditions emit an observable metric/alarm from the `sar`/host-metric
  series — (a) host available RAM below the floor for the sustained window, (b) embedder ≥ 90%
  of cap **and** p95 embed latency over the recall budget, (c) ES heap pressure forcing a cap
  raise with no free host RAM — and each is fired against a **synthetic breach** and confirmed
  to trip. *Fails if* any D2 branch has no emitter, or a synthetic breach of it does not raise
  an alarm — escalation on that branch would then be a guess, not a fired condition.
- **AC-5 — (Conditional, gated on Step 3 executing) Inter-box personal text is private +
  encrypted, provably, on every path.**
  **Check:** (i) config enforces transport security — model endpoints are rejected if `http://`
  / plaintext and the second-box endpoints resolve to a private (vRack/WireGuard) address with
  WireGuard or TLS/mTLS **required**, not optional; and (ii) a packet capture on the inter-box
  interface (private path included, not only a public one) shows the model-call payloads are
  **encrypted ciphertext, never plaintext personal text**. *Fails if* a plaintext/`http://`
  model endpoint is accepted, the endpoint resolves to a public IP, or a capture on *any* path
  (public or private vRack) reveals plaintext personal text. (Not applicable until the
  horizontal split is triggered.)
- **AC-6 — A durable per-service footprint baseline exists (measurement-first).**
  **Check:** a per-container memory/CPU **series** is captured and stored (ES / `sar`), not a
  one-shot `docker stats`, giving the trigger a reference and the reclaim a before/after.
  *Fails if* the only footprint data is ad-hoc terminal output with no persisted per-service
  series.
- **AC-7 — Personal-data backups exist AND are restore-tested (durability, not just a file).**
  **Check:** a scheduled backup runs for Postgres and Neo4j (plus an ES snapshot), and a
  **periodic restore drill** loads the latest backup into a scratch instance and verifies it is
  usable (e.g. row/node counts and a sample query match the source). *Fails if* backups are
  produced but never restore-verified — an unrestored backup is false confidence, the classic
  backup failure this AC exists to catch.
- **AC-8 — Backups are custody-compliant: R2 holds only ciphertext, within the retention
  window.** (Required now — D5/D6 make backups + R2 staging mandatory, not a deferred step.)
  **Check:** fetch a backup object from the R2 staging tier and confirm it is **not readable
  plaintext** (undecryptable without the owner-held key, which is not present in R2 or the
  provider account), AND confirm the backup prefix holds **≤ 7 daily objects and none older
  than 8 days** (older objects pruned as they rotate to owner personal storage). *Fails if* any
  R2 backup object is readable plaintext, the key is retrievable from the provider tier, or R2
  retains backups beyond the 8-day window (permanent third-party residency). If R2 is not the
  chosen staging tier, an equivalent off-site custody-compliant target must satisfy the same
  ciphertext-only + bounded-retention invariant.

**Seam owner (assembled intent).** The ADR's intent holds only when the enforcement,
observability, durability, and backup-custody seams **all** land: **AC-2 (custody guard in
code)**, **AC-4 (all trigger branches emitted)**, **AC-7 (restore-tested backups)**, AND
**AC-8 (backups custody-compliant)**. The last child merging (e.g. the embedder cap bump, AC-3)
does **not** prove the assembled decision: a cap bump with no guard, no trigger, and no
restore-tested custody-compliant backup satisfies AC-3 while leaving the policy unenforced,
escalation blind, and the data undefended or leaking. **Master asserts the seam at the
integration gate** — the ADR does not close until AC-2, AC-4, AC-7, and AC-8 are all proven.
**Only AC-5 is genuinely conditional** (gated on the horizontal split in D1 Step 3 actually
executing).

---

## References

- ADR-0105 — Convergent Self-Improvement Pipeline & Isolated System Graph (private embedding path; `sysgraph` isolation) — Accepted
- ADR-0098 — Memory Substrate & Lifecycle (personal-data stores) — Accepted
- ADR-0074 — Observability / joinability probe & substrate-guard pattern — Accepted
- ADR-0069 — Artifact Substrate (R2; reused for the D6 encrypted backup-staging tier) — Implemented
- FRE-809 — this ADR's umbrella ticket (owner-directed, 2026-07-05)
- FRE-721 — ADR-0105 T7, generation-time read-before-emit; reaffirms the fixed private embedding path
- FRE-718 — ADR-0105 T6, Postgres parameter tuning (RAM-aware) — parked; referenced by D1 Step 1
- Substrate-observability & data-lifecycle gap thread (owner-flagged forward work; AC-4/AC-6 close part of it)
- Live host measurement, 2026-07-06 (this session): 8 vCPU Haswell, 22 GiB / ~10 GiB free, load 0.72; ES 96.6% of 2 GiB cap, embedder 96.5% of 2.93 GiB cap, test stack ~1.57 GiB reclaimable

---

## Status Updates

### 2026-07-06 - Proposed
**Changed By:** adr session (Opus), FRE-809
**Reason:** Owner-directed strategic ADR. Core decisions (data-class custody rule; on-box-first
topology with trigger-gated escalation; managed PG + managed embedding denied for the
personal-data plane; second owned box + private link permitted; home box deferred; OVH options
span VPS + bare metal) were settled with the owner in-session (2026-07-06). A redundancy
dimension was added in the same session at the owner's prompting: durability-first (mandatory
restore-tested backups) with availability/multi-node HA deferred behind a failure-domain
trigger; Neo4j stays Community + scheduled backup (Enterprise surfaced, not adopted); backups
follow an encrypt-at-source → R2 ciphertext staging → owner personal storage topology.
**Transition rule:** status becomes **Accepted** when master merges the ADR PR (the core
decisions are owner-ratified in-session, so acceptance is a gate formality); it becomes
**Implemented** only when the assembled-intent seam (AC-2, AC-4, AC-7, AC-8) is proven. See
the FRE-809 handoff comment.
