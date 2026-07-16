# ADR-0120: Cost governance — visibility + consent (supersedes ADR-0065)

**Status:** Proposed
**Date:** 2026-07-16
**Deciders:** Owner (architect), cc-adrs (Opus); design settled with the owner in cc-explore2
**Tags:** cost, governance, observability, human-in-the-loop, alerting, config

---

## Context

ADR-0065 (Cost Check Gate) enforces spend with **hard, layered dollar caps** and an atomic
reserve/commit/refund primitive. The enforcement model has a structural flaw: **a dollar ceiling
cannot tell "using it a lot" from "it's broken."** It punishes both identically — by breaking the
process. That is exactly the ADR-0065 incident: a $10 weekly cap tripped → every call `raise`d →
empty PWA turns. The gate meant to protect spend instead broke the product on *normal* heavy use.

Three further facts, verified against the code, drive the redesign:
- **Three metered vendors are completely off the books.** OVH embedding (€, token-priced), Voyage
  reranker ($, token-priced — and recall fans it out over the whole candidate set, a silent
  multiplier), and **Perplexity** (`tools/perplexity.py` — a *direct* paid REST tool, not SearXNG;
  registered and active; the API returns token `usage` so it is trivially instrumentable). None has
  an `api_costs` row. **You cannot alert on, or gate, a vendor you don't measure.**
- **Artifact building and vision ingestion bill invisibly** inside `main_inference`.
- The model **conflates two orthogonal things**: *denial semantics* (who to protect from
  starvation) with *accounting* (which vendor am I exposed to). Provenance note: `entity_extraction`
  → OpenAI was a deliberate 2026-03-28 pricing decision (~93% cost cut), not an invariant — so the
  reserved-floor/priority logic should key on denial-semantics, not on which provider a role sits on.

**What needs to be decided:** a cost-governance model that protects against a *runaway bug* without
breaking *normal use*, makes all spend *visible*, and keeps the owner the sole authority over hard
stops.

---

## Decision

**Replace enforcement with visibility + consent: forward-by-default, pause-and-alert as the only
automatic reflex, and the owner as sole resume/kill authority.** Supersedes ADR-0065's hard-cap
enforcement; keeps its `reserve()`/`commit()`/`refund()` primitive (re-applied at *job* scope for
Section C).

**1. Kill hard, process-breaking dollar caps.** No absolute dollar ceiling denies a call anywhere.
Enforcement → visibility + consent.

**2. Separate the two failure modes.** *Runaway/anomaly* (a bug, a loop, a fan-out storm —
unbounded, fast, unintended) vs *sustained normal-but-expensive use* (you're just using it a lot —
bounded by intent). Normal → **never stop**; indicate and let the owner consent (cards) or review
after (alerts). Runaway → an automatic **reflex, but a soft one**, keyed on **shape** ("8× normal
rate", "N reservations for one trace in M seconds"), never on an absolute cap.

**3. Two verbs, deliberately split.**
- **Pause** = reversible, self-healing throttle. **Automation may do this.** For a background
  nack-role, pause = "finish the current item, then hold; resume later" — deferred, never lost.
- **Stop / kill** = terminal. **Owner-only.** Automation's authority ends at "slow down and ask."

**4. Pause is the safety primitive** that makes "no hard limit" safe at 3am with no human watching:
it holds spend flat until a human answers, through whichever channel reaches them. It is safe
because the pause boundary is **between items** and both datastores commit **per-item** (the
consolidator already does this — `second_brain/consolidator.py:159,220`), so a pause never catches a
half-written Neo4j/Postgres transaction; Redis Streams makes it cleaner (stop consuming → unacked
messages redeliver).

**5. Frontend = approval cards** (reuses the shipped `request_tool_approval` primitive,
`transport/agui/transport.py:375` — the cost card is the same primitive with a different payload,
not new infrastructure). Forward-by-default: below threshold, silent forward; above it, a card;
forward unless the owner stops it. **Threshold = anomaly-adaptive with a fixed bootstrap** — ship a
fixed starting value per Section-C job type (a cold start has no baseline to be "relative" to), and
let it become *relative-to-your-normal* as baseline data accrues; the fixed value remains the
cold-start default and the floor. (Owner-settled: not fixed-*vs*-anomaly — anomaly-adaptive always
needs a starting threshold.)

**6. Backend = pause + alert.** Anomaly on *shape* → **pause** the offending role (reversible) →
**alert the owner** → the owner is the **only** authority that resumes or kills. Automation may
*slow*, never *terminate*.

**7. Out-of-band alerting, because the socket isn't always there.** The WebSocket is
**session-scoped** (`service/ws_ticket.py`) and pushes to a *connected* PWA; with no active socket
it degrades to a `connection_lost` default (`transport/agui/transport.py:164`) — and the 3am runaway
is exactly the no-socket case. So: **PWA card = in-band** (connected); **Signal (`signal-cli`) on
the VPS = out-of-band** (asleep), **email/SMTP the dumb-but-reliable fallback**; **pause buys time
for either to reach the owner.** Signal is on-brand with the custody/privacy posture (ADR-0111).
Seshat has **no outbound owner-notification channel today** — this is net-new. The payload is
**actionable**: the owner's reply (resume / keep-paused / kill) actuates back into the pause-state.

**8. Three management tiers — gate/alert at the granularity of a unit of work a human initiates,
not per model call.**
- **Section A — Substrate / "keeping the lights on"** (pinned backend models, invisible nack
  denial): *KG writers* (entity_extraction, captains_log, insights, promotion, freshness) +
  *Retrieval substrate* (embedding→OVH, reranker+fallback→Voyage). **Manage as per-vendor pool
  alert thresholds** (the ~4 pools you're actually invoiced at — Anthropic-bg, OpenAI-bg, OVH,
  Voyage) **+ per-role visibility** (telemetry in the config UI). No per-role *walls* — the only
  firewall that matters is A-vs-B, which preemption handles; per-role walls are the maintenance
  burden that made the old hard caps brittle. *(Owner-ruled: this recommendation.)*
- **Section B — Interactive / the live path** (primary + sub_agent, skill_routing, vision-on-turn;
  visible `raise` denial): **the live turn always wins** (priority preemption); **never paused**;
  gets an **approval card** when a spend needs consent.
- **Section C — Discretionary heavy jobs** (artifact building, deep research, study/one-shot
  ingest, Perplexity): a human kicks off one unit that spawns many calls. **Gate at the job
  envelope (the "purchase-order" model):** reserve the whole *estimated* budget up front (one PO),
  let sub-calls settle against it — the existing `reserve()`/`commit()`/`refund()` primitive at
  *job* scope. **Ask-me-first** over the (bootstrap→adaptive) threshold: estimate → owner confirms →
  run. Determinism is what makes this honest — a fixed fan-out (K searches × M tokens) is
  pre-budgetable; an LLM-router deciding dynamically is not, which is why deep research must be a
  **deterministic workflow**, gated at the envelope.

**9. Step zero (mandatory prerequisite).** Instrument **OVH embedding + Voyage reranker +
Perplexity** into `api_costs`. *(Owner-ruled: onboard Perplexity — it's an active paid tool and its
API returns usage, so onboarding is cheap; revisit retirement only when deep-research ships.)*
Nothing else is real until spend is visible.

**The honest accountant's caveat (owner accepted, eyes open):** "no hard limit" trades a small
*guaranteed* annoyance (occasional false-deny) for a small *probability* of a large bill from a bug
caught late. The pause bounds the *rate* of loss, not loss-to-zero — some money is spent between the
trip and the owner's response. For a single-user research system with known, modest vendors this is
the right trade — but it *is* a trade, and it is only as good as the measurement under it (→ §9).

---

## Alternatives Considered

### Option 1: Keep ADR-0065's hard dollar caps (status quo)
**Description:** Layered per-role/global dollar ceilings that deny (`raise`/`nack`) on breach.
**Pros:** simple, certain, loss-bounded to the cap.
**Cons:** conflates normal-heavy-use with a bug and breaks the process for both — the ADR-0065
incident (cap trip → empty PWA turns). A crude, certain tool that is wrong about *which* failure it
caught.
**Why Rejected:** the incident is the proof; certainty of the wrong behaviour is not a virtue.

### Option 2: Higher / per-vendor hard caps
**Description:** Same enforcement, bigger or vendor-pool ceilings.
**Pros:** fewer false trips than tight per-role caps.
**Cons:** still process-breaking on a trip; still can't tell normal from broken; just moves the
cliff.
**Why Rejected:** it treats the symptom (cap too low), not the flaw (enforcement conflates the two
failures).

### Option 3: Pure visibility — alert only, no automatic pause
**Description:** Meter everything, alert the owner, never auto-pause.
**Pros:** zero false-deny; nothing automated ever slows real work.
**Cons:** the 3am runaway spends **unbounded** until the owner wakes — the alert without the pause
doesn't bound the rate of loss.
**Why Rejected:** the pause is precisely what makes "no hard limit" safe with no human watching; a
reversible soft reflex costs little and bounds the worst case.

### Option 4: An LLM-in-the-loop cost router / dynamic budgeter
**Description:** A model decides per-call whether to spend.
**Pros:** flexible.
**Cons:** non-deterministic and latency-adding in the hot path (per the 2026-07-16 routing survey);
a cost decision must be cheap, deterministic, and auditable.
**Why Rejected:** wrong tool — deterministic shape-detection + owner consent is the SOTA pattern.

---

## Consequences

### Positive Consequences
- **No process-breaking denials** — normal heavy use never trips an empty turn.
- **All spend visible** (T0) — the three off-books vendors, plus artifact/vision split out of
  `main_inference` (T5).
- **Owner-consent model** — cards in-band, Signal out-of-band, owner sole resume/kill.
- **Pause is reversible and safe** (between-items, per-item commit) — a soft reflex, not a break.
- The three-tier grouping maps to **how spend actually happens** (unit-of-work, not per-call), and
  feeds the ADR-0119 config UI cost surface.

### Negative Consequences
- **Net-new build**: an outbound Signal channel (Seshat has none today), the pause-signal wiring,
  the anomaly detector, and the currency plumbing.
- **The false-trip/missed-trip tuning problem is real** — the price of trading a crude-but-certain
  cap for shape-detection.
- **Pause is cooperative and per-consumer today** — only the consolidator honors it; a pausability
  audit is required before the backend reflex is safe system-wide.
- **A bug caught late spends between trip and response** — the caveat above; bounded in *rate*, not
  to zero.
- **Currency:** OVH bills EUR while the ledger is `*_usd` — new plumbing (conversion vs native
  column).

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| A runaway spends unbounded before the owner responds | High | Pause bounds the *rate*; out-of-band Signal reaches the owner asleep; email fallback if the Signal bridge is down |
| A false anomaly trip pauses real work | Medium | Pause is reversible + self-healing (nack-roles retry); bootstrap fixed floor + owner review; tuning is an explicit open question (§Open) |
| A pause corrupts a half-written record | High | Pause boundary is *between items*, both datastores commit per-item, Redis redelivers unacked — asserted by AC-3; gated on the pausability audit (T1) |
| Automation over-reaches to a hard stop | High | Two-verb split enforced in code: no automated resume/kill path exists (AC-4) |
| Onboarding OVH mis-converts EUR→USD | Medium | Currency decision is part of T0 (conversion step vs native column); AC-1 asserts correct cost |

---

## Implementation Notes

**Foundation-first sequencing (T0 is a hard prerequisite):**
- **T0 — Instrument OVH + Voyage + Perplexity into `api_costs`** (+ the EUR/USD currency decision).
  Parse token usage from each vendor's response (OVH/Voyage token-priced; Perplexity returns
  OpenAI-compatible `usage`); record cost rows. *Nothing else is real until spend is visible.*
- **T1 — Pausability audit** across background consumers (entity_extraction, captains_log, insights,
  promotion, freshness): per-role safe between-items check-point + one shared pause signal (a Redis
  key per role); confirm each loop polls/honors it.
- **T2 — Anomaly → pause reflex** on the VPS: wire the existing insights cost-spike signal
  (FRE-870/629) — or a purpose-built rate detector — to the pause signal; owner-only resume/kill.
- **T3 — Out-of-band alerting:** `signal-cli` on the VPS + email fallback; an actionable payload
  (resume / keep-paused / kill) that actuates back to the pause-state. **(Assembled seam.)**
- **T4 — Cost approval cards:** extend `request_tool_approval` to a cost-consent payload; the
  Section-C bootstrap→adaptive threshold.
- **T5 — Split `artifact_builder` + vision out of `main_inference`** for visibility (coordinate with
  ADR-0118/0119, which already extract `artifact_builder`).
- **T6 — Config-UI cost surface:** the three-tier grouping rendered in the ADR-0119 config
  interface — per-vendor pool thresholds + per-role telemetry.
- **Deep research** is a separate feature project (deterministic workflow); its cost model = Section
  C job-envelope; its own ADR references this one.

**Dependencies:** ADR-0065 (superseded — keep its reserve/commit/refund primitive), ADR-0075
(WebSocket transport — the card channel), ADR-0111 (custody/privacy — Signal on-brand), ADR-0118/0119
(artifact_builder extraction + the config cost surface), FRE-870/629 (cost-spike anomaly, already
firing).

**Testing:** vendor-cost instrumentation (T0) with a live recall turn + a `perplexity_query`;
pause-safety (no partial write, resume without loss); the no-hard-cap replay of the ADR-0065
incident; the no-socket → Signal → reply-actuates round-trip.

---

## Verification / Acceptance Criteria

- **AC-1 — Every metered vendor is on the books.** *Check:* after a recall-heavy turn (fires
  embedding→OVH + reranker→Voyage) and a `perplexity_query`, `api_costs` has a row per vendor with a
  **nonzero, correctly-converted** cost (OVH EUR→USD). *Fails if* any of the three still has no row.
- **AC-2 — No dollar ceiling denies a normal turn.** *Check:* replay the ADR-0065 incident load (a
  turn that tripped the old $10 weekly cap); it **forwards** — no `raise`, no empty PWA turn. *Fails
  if* any call is denied on an absolute dollar cap.
- **AC-3 — A pause is reversible and loses nothing.** *Check:* trigger a pause on a background role
  mid-loop; assert no half-written Neo4j/Postgres record, the in-flight item is not lost (Redis
  redelivers / resumes on the next item), and spend is flat while paused. *Fails if* a pause corrupts
  or drops an item, or spend continues while paused.
- **AC-4 — Automation pauses but never terminates.** *Check:* the reflex pauses a role; assert there
  is **no** automated resume-or-kill code path — only an owner action (PWA/Signal) resumes or kills.
  *Fails if* automation resumes or kills on its own.
- **AC-5 — Out-of-band reaches the owner and actuates.** *Check:* a pause with **no active
  WebSocket** sends a Signal alert (email if the bridge is down); the owner's reply
  (resume/keep-paused/kill) drives the pause-state. *Fails if* the no-socket case sends nothing, or
  the reply does not actuate the state.
- **AC-6 — Consent gates a heavy job; trivia forwards silently.** *Check:* a Section-C job over the
  threshold shows an approval card and does **not** run until the owner approves; a below-threshold
  call forwards with no card. *Fails if* a heavy job runs without consent, or a card fires on trivial
  calls.
- **AC-7 — Section-A: per-vendor thresholds + per-role visibility (no per-role walls).** *Check:* a
  recall-heavy load raises the **Voyage pool** telemetry and the per-role breakdown is visible in the
  config UI; a **per-vendor** threshold breach alerts. *Fails if* a per-role wall denies/pauses on a
  per-role cap, or per-role telemetry is missing.
- **AC-8 — Section-C jobs reserve at the envelope, not per call.** *Check:* an artifact/deep-research
  job makes **one** up-front reservation for the estimated budget; sub-calls settle
  (commit/refund) against that envelope. *Fails if* each sub-call takes its own reservation / hits a
  separate cap.

**Seam owner:** the **assembled safety loop** — anomaly → pause → out-of-band alert → owner
resume/kill — holds only once T1+T2+T3 land together; owned by **T3** (the alerting round-trip is
where the loop first closes end-to-end). The ADR does not close when T0 (visibility) merges; it
closes when the loop is proven (AC-3/4/5) and no dollar cap denies (AC-2). Master asserts these at
the gate.

---

## References

- ADR-0065 — Cost Check Gate (**superseded by this ADR**; its reserve/commit/refund primitive is kept)
- ADR-0075 — WebSocket transport (the in-band approval-card channel)
- ADR-0111 — data custody / privacy posture (Signal is on-brand; self-hosted, no third party)
- ADR-0118 / ADR-0119 — artifact_builder extraction + the config-UI cost surface (T5/T6 coordinate here)
- `docs/research/2026-07-16-cost-governance-rework-adr-0065.md` — the design note this ADR authors from
- `docs/research/2026-07-16-model-routing-sota-survey.md` — why deep research is a deterministic workflow, not an LLM router
- Code: `cost_gate/{gate,policy,types}.py` · `config/governance/budget.yaml` · `second_brain/consolidator.py:159,220` (pause pattern) · `transport/agui/transport.py:164,375` (approval card + connection_lost) · `service/ws_ticket.py` (session-scoped WS) · `memory/{embeddings,reranker}.py` + `tools/perplexity.py` (untracked vendors)
- FRE-870, FRE-629 — cost-spike anomaly patterns already firing
- Pricing: Voyage <https://docs.voyageai.com/docs/pricing> · OVH <https://www.ovhcloud.com/en/public-cloud/ai-endpoints/catalog/>

---

## Open Questions (carried from the design note §7 — to resolve in build, not blocking authoring)

- **Pausability audit specifics (T1):** the safe between-items check-point per consumer and the one
  shared pause signal.
- **Anomaly-detector tuning (T2):** what *shape* trips the reflex; reuse the insights cost-spike
  pipeline (FRE-870/629) vs a purpose-built rate detector; false-trip vs missed-trip calibration.
- **Currency handling (T0):** a conversion step vs a native-currency ledger column for OVH's EUR.
- **Signal resume/kill round-trip (T3):** delivery guarantee, dedupe, and how a phone reply
  actuates back into the VPS pause-state.

*(Resolved this session, recorded above: Section-A granularity = per-vendor thresholds + per-role
visibility; Perplexity = onboard; approval-card threshold = anomaly-adaptive with a fixed
bootstrap.)*

---

## Status Updates

### 2026-07-16 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal, superseding ADR-0065. Authored from the owner-settled cc-explore2
design note (governance shift: hard-cap *enforcement* → *visibility + consent*; pause+alert as the
only automatic reflex; owner sole resume/kill; T0 = instrument the three off-books vendors). Owner
rulings folded in: Section-A = per-vendor thresholds + per-role visibility; Perplexity = onboard
(verified a direct paid tool, not SearXNG, and cheaply instrumentable); approval-card threshold =
anomaly-adaptive with a fixed bootstrap (anomaly-relative still needs a starting value). §7 open
questions carried forward. On merge, master should flip ADR-0065 to Superseded.
