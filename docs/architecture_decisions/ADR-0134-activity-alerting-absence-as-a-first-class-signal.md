# ADR-0134: Activity Alerting — Absence as a First-Class Signal, on Platform-Native Alerting

**Status:** Proposed; **D2a's Kibana stage abandoned 2026-08-08** — the ADR's own stated contingency fired on FRE-1187's measurement (see Status Updates). The full rule set lands on Grafana.
**Date:** 2026-08-07
**Deciders:** Project owner (FRE-1058, owner-directed 2026-08-07)
**Tags:** observability, alerting, absence-detection, telemetry, grafana, elasticsearch

---

## Context

### What is being decided

FRE-1058 asked for a **fourth corner** on ADR-0090's telemetry surface contract — *delivery*, alongside
emit, mapping and dashboard — on the premise that events are emitted and lost in transit and nothing
asks whether they arrived.

The discussion that produced this ADR rejected that framing and arrived somewhere else. The missing
thing is not a corner of the surface contract. It is an **output** for the instruments that already
exist, and the signal class none of them carries is **absence**. This ADR decides what is alerted on,
what discipline every alert obeys, and where alerting lives. It leaves ADR-0090 with a single
one-clause amendment (D6) rather than a fourth corner.

### The instruments exist; the egress does not

Verified against this branch, 2026-08-07:

- Five instruments are built and committed, but they are **not equivalent**, and the difference matters
  to D4: the **joinability probe** and the **SLM health probe** are scheduled by `BrainstemScheduler`
  and persist result documents (`observability/joinability/sink.py`, `observability/slm_health/sink.py`);
  the **disk-usage threshold** runs
  inside the lifecycle manager; and the **cache-erosion monitor** and the **delivery-ratio probe** are
  **manual CLIs that persist no result document at all**. The instrument FRE-1051 built to detect
  telemetry loss is neither scheduled nor persisted, so nothing could alert on *its* absence either.
- `disk_usage_alert_percent` (`src/personal_agent/config/settings.py:1537`) is the **only** alert
  threshold in settings. `src/personal_agent/telemetry/lifecycle_manager.py:183` computes
  `alert = used_pct >= settings.disk_usage_alert_percent`, and `:201-206` emits
  `log.warning(LIFECYCLE_DISK_ALERT, …)`. **The alert is a log line.** It notifies nobody.
- There is **no out-of-band notification path in the repository** — no SMTP, no push, no webhook, no
  PWA web push. A search of `src/` for `ntfy`, `pushover`, `telegram`, `slack`, `smtplib` and
  `aiosmtplib` returns nothing. (The AG-UI transport does push events to an *already-connected* client,
  `transport/agui/transport.py:596` — but a channel that requires the owner to be watching is not an
  alert transport, and is the opposite of what an absence alarm needs.)
- `BrainstemScheduler` already runs periodic probes in-process — joinability hourly, SLM health every
  300 s, domain-guard warming. **The periodic execution home exists; it has no outbox.**

So every finding this system produces is written into Elasticsearch and waits for a human to go
looking. For the delivery probe that is circular: it writes its findings *about telemetry loss* into
the telemetry system whose completeness it is measuring.

### The measured failure, and why nothing caught it

FRE-1051 measured **404 `api_cost_recorded` events emitted and never indexed** over 2026-07-23..28 —
82.6 % loss on the 23rd, 47.8 % on the 26th, 52.4 % on the 27th, and zero on the other three days. The
shape was episodic and whole-process, not a sampling rate.

**The diagnosis was corrected during that work, and the correction is what shapes this ADR.** Neither
the unreferenced-task hypothesis nor the shutdown-flush hypothesis measured any loss. The mechanism was
that `add_elasticsearch_handler` has exactly **one** production call site —
`src/personal_agent/service/app.py:754` — and even there it is conditional on the connection
succeeding. A process running outside the FastAPI lifespan binds no sink and ships nothing, silently.
(Still true on this branch: `gateway/app.py:148` constructs an `ElasticsearchHandler` but harvests only
its client for *queries*, never attaching it as a log sink.)

Two facts about that incident matter more than its cause:

1. It ran for at least six days.
2. It was found **weeks later, by accident**, by an explore session reasoning about an unrelated
   question — not by anything watching.

Nothing was watching because there is nothing for anything to watch *with*.

### Why this is not ADR-0090's fourth corner

ADR-0090's three corners share properties that absence does not:

| | emit / mapping / dashboard | absence |
|---|---|---|
| Artifact under audit | committed files in git | production behaviour over time |
| Where it is checked | hermetic CI, no live stack (ADR-0090 D5 floor) | a live window |
| Grain | `(field, family)` — FRE-533's 1023 rows | `(family, window)` |

FRE-533's inventory is keyed `family,field,live_type,…,emit_sites,dashboard_refs` — one row per field.
Delivery cannot become "a fourth column" on it without writing a per-window measurement into a per-field
row. The word *corner* imported a shape that does not fit the obligation.

What ADR-0090 does need from this work is one line in its D6 done-bar. That is decided in **D6** below.

### The platform we have, and why its alerting is off

Verified 2026-08-07, and the single most consequential fact in this ADR:

- Elasticsearch and Kibana are both **8.19.0**; the licence is **`basic`** (`GET /_license`).
- Kibana's `/api/actions/connector_types` returns **HTTP 500**. The cause is **not** licensing. Kibana's
  own startup log states it plainly:

  ```
  [WARN][plugins.encryptedSavedObjects] Saved objects encryption key is not set.
  [WARN][plugins.actions]  APIs are disabled because the Encrypted Saved Objects plugin is
                           missing encryption key.
  [WARN][plugins.alerting] APIs are disabled because the Encrypted Saved Objects plugin is
                           missing encryption key.
  ```

  **Kibana's alerting and actions APIs have been disabled since deployment by a missing
  `xpack.encryptedSavedObjects.encryptionKey`.** Our Kibana service (`docker-compose.cloud.yml:160`)
  sets two environment variables and mounts `docker/kibana/kibana.yml`; the key is in neither. The
  `stackConnectors` plugin loads normally and is waiting behind the disabled API.

This matters because it means **the alerting capability was never absent — it was switched off**, and
the first draft of this ADR wrongly concluded from the 500 that Kibana could not notify, and deferred
the whole decision to Grafana. That is the error this section exists to prevent repeating: a broken
instrument was read as a negative result.

**What remains genuinely unknown** is whether the `basic` tier's connector set includes anything that
leaves the box. Elastic's subscriptions page lists "Server Log and Index" separately from the action
connectors (email, webhook, Slack, PagerDuty, …) without stating the tier boundary. That question is
**now cheaply answerable** — set the key, restart Kibana, enumerate the API — and it is the first
implementation step rather than an assumption baked into the decision.

**FRE-1072** (ADR-0129 B7) brings Tempo and Grafana. Grafana OSS unified alerting
carries no licence gate and has a first-class **No Data** alert state, so it is the destination for the
full rule set — **planned and ticketed, not hypothetical.** It is also not close: the ADR-0129 chain is
at B2 (FRE-1065 In Progress), with B3–B8 approved behind it, putting FRE-1072 roughly five sequenced
tickets out. That distance, against a failure class that already ran six days undetected, is what
makes the staging in D2a a real decision rather than a formality.

*(**Corrected 2026-08-08, FRE-1213.** This sentence read "brings Tempo and Grafana **and retires
Kibana**". That was never true of FRE-1072 — ADR-0129's 2026-08-07 amendment explicitly retained
Kibana, and its retirement is now separately sequenced as FRE-1214 under the FRE-1203 Grafana
migration program. FRE-1202 recorded the drift. The distance argument above is left as written
because it is what the staging decision rested on at authoring time; what became of that staging is
recorded in D2a and in the Status Updates, not by rewriting the reasoning that produced it.)*

### Scope boundary

This ADR owns **what is alerted on, and the discipline every alert obeys**. It does *not* own:

- **The transport** — notifier, retry, deduplication, grouping, silences. Those are the platform's,
  deliberately (D2).
- **The log-delivery mechanism.** FRE-1055 (handler survives threads and shutdown) and FRE-1056 (bind
  the sink in the standalone gateway, drain before exit) are in flight and fix the measured cause. This
  ADR does not re-decide them and must not duplicate them.
- **ADR-0090's emit / mapping / dashboard corners**, beyond the single amendment in D6.

---

## Decision

### D1 — Absence is a first-class alert condition, and shortfall is part of it

Almost every instrument in this system answers *is this value bad?*. Only the delivery-ratio probe has
a notion of *this proved nothing* (`UNVERIFIABLE`), and it is not scheduled. Nothing answers **did this
stop happening?** — and absence is invisible to threshold alerting by construction: **a threshold rule
over a metric that has stopped arriving does not fire, because it has nothing left to evaluate.**
Silence and health produce identical evidence.

Absence is therefore configured **explicitly**, as a rule whose no-data outcome is a *firing* state
rather than a quiet one. Where the platform offers this natively — Grafana's `No Data` state, or a
Kibana rule with an explicit no-data action — it is used rather than reconstructed as a
threshold-of-zero, which is fragile and inverts the same way. **A rule authored without deciding its
no-data behaviour has decided it by default, and the default is silence.**

**Absence alone is not enough, and this correction is load-bearing.** The incident that motivates this
ADR was *not* a total stoppage. On the three bad days the family still flowed — at 17.4 %, 52.2 % and
47.6 % of the oracle. A pure no-data rule **would not have fired on FRE-1051.**

**And shortfall cannot be detected from the family's own history alone.** A family's volume tracks
traffic, so "half as many `api_cost_recorded` documents as yesterday" is equally consistent with *half
the events were lost* and *half as many requests arrived*. Distinguishing them requires relating the
family's volume to an **independent measure of the activity that should have produced it** — which is a
ratio against a denominator, i.e. the oracle relationship in weaker form. Claiming otherwise would be
the design's central dishonesty, so it is stated instead:

**Rule 1 therefore has two branches with honestly different coverage:**

| Branch | Condition | Denominator needed | Coverage |
|---|---|---|---|
| **Stoppage** | No documents for the family in the window while the system is active | An activity **witness** (binary: was anything happening) | Families that **declare an expected emission cadence** — see below |
| **Shortfall** | Documents per unit of independent activity falls materially | An activity **denominator** correlated with the family's expected volume | Only families that **have** one — declared per family |

**Stoppage does not apply to every family, and assuming it did would manufacture false alarms.** Many
families are *conditional or rare by design* — an error event, a rollback, a rare branch — and their
silence is health, not failure. A stoppage rule over them alerts on a working system, which is the
muting path D4 warns about. **Each family therefore declares one of three dispositions:** *cadence*
(expected to emit whenever the witness shows activity — stoppage applies), *correlated* (cadence plus a
denominator — stoppage and shortfall both apply), or *conditional* (emission is event-driven, so
neither branch applies and the family is explicitly out of scope). The declaration is the committed
artifact AC-1 reconciles; a family with **no** disposition is the failure, not a family with the
*conditional* one.

The denominator is weaker and cheaper than FRE-1051's twin-store oracle: it needs a *correlated
activity measure*, not a per-event twin. For `api_cost_recorded` — the family that actually failed —
the denominator is the `api_costs` row rate that already exists. For families with no such measure,
**only the stoppage branch applies, and that limit is declared per family rather than left implied.**
This is less than the first draft claimed and it is what the evidence supports.

This generalises FRE-1051's own rule — `UNVERIFIABLE` is a verdict, never a silent pass — from one
probe's output to the alerting layer as a whole.

### D2 — Alerting is platform configuration, not application code

**We do not build a notifier, a log shipper, a queue, or delivery guarantees.** Every one of those was
considered and rejected (see Alternatives). The stack already provides the whole of it, and the project
is a small research harness, not an enterprise platform.

Consequences that bind:

- **No notification code lands in `src/personal_agent/`.** No SMTP client, no push integration, no
  webhook poster, no alert-routing logic. D5's heartbeat is the single, stated exception.
- **Alert rules and contact points are version-controlled configuration**, exported to the repository
  and re-importable — the same discipline ADR-0090 D3 applies to dashboards. A rule that exists only in
  a live UI is drift.

**Grafana is the destination, and it is planned rather than hypothetical** — FRE-1072 (ADR-0129 B7).
The full rule set lands there, with its native `No Data` state and unlicensed contact points.

**But the chain is at B2 and FRE-1072 is roughly five sequenced tickets out, so waiting for all of it
is not free.** The detection gap this ADR exists to close stays open for the whole interval, and the
failure it is meant to catch has already run for six days undetected once.

### D2a — Staged delivery, split by a rule that decides itself

> **ABANDONED 2026-08-08 — by this decision's own stated contingency, not by supersession.** D2a wrote
> the condition under which its Kibana stage should be dropped: *"If the `basic` connector set proves
> to contain nothing that leaves the box … the Kibana stage is abandoned outright: rules 1 and 2 wait
> for FRE-1072 with the rest."* FRE-1187 set the encryption key, restarted Kibana and enumerated
> `/api/actions/connector_types`: **29 connector types, of which exactly two are enabled under this
> `basic` licence — `.index` and `.server-log` — and neither leaves the box.** Every connector that
> does requires at least a gold licence. **The contingency fired as designed.** The whole rule set
> lands on Grafana; nothing was authored on Kibana and nothing needs porting off it. The staging
> reasoning below is left standing because it is *why* the measurement was commissioned as the first
> implementation step rather than assumed — and commissioning it is what turned a guess into a
> verdict. Read it as history with a known outcome, not as live instruction.

The contract lands now; the rules land in two stages, and **the criterion for which stage a rule falls
into is whether it requires a new investigation surface** (D3):

**The staging predicate is the cost of the rule's investigation target**, and it is applied
consistently below — not "does the target already exist," which is false for every rule (see D3).

- **Now, on Kibana** — rule 2, whose target is a **saved Discover query** on a probe's result index:
  minutes to author, discarded without loss. And rule 1, which is the exception and is named as one: it
  needs a **minimal purpose-built surface** (family volume, its denominator, the witness) because a
  single Discover query cannot show the evidence AC-2 demands. **That surface is the one artifact this
  ADR knowingly builds twice**, accepted because rule 1 is the only rule that addresses the motivating
  incident and deferring it defeats the point of staging at all.
- **On Grafana, with FRE-1072** — rules 3–6, each of which needs a full new dashboard.

**This does not weaken D3, and the distinction is exact.** D3 forbids a rule *shipping* without a
resolvable investigation target; it does not require the target to pre-date the ticket. **No target for
rules 1 or 2 exists today** — the only committed monitor saved search is probe-specific — so authoring
each is **part of that rule's own delivery**, and D3 is satisfied because rule and target ship
together. The contract itself (D1, D3, D4, D5) names conditions, disciplines and targets — never a rule
syntax — so it survives the migration untouched.

**One open question, now measurable rather than assumed, with a single stated contingency.** If the
`basic` connector set proves to contain nothing that leaves the box — index and server-log connectors
only — then a Kibana alert is a log line, which is the failure this ADR exists to end, and **the Kibana
stage is abandoned outright: rules 1 and 2 wait for FRE-1072 with the rest.** No half-measure, no
notification routed to a platform that does not exist yet. **Establishing which connectors this licence
exposes is therefore the first implementation ticket and gates the whole Kibana stage.** It is small:
set the key, restart, enumerate.

**That ticket must answer two questions, not one.** Connector availability proves Kibana can *notify*;
it does not prove Kibana can *express* rule 1 — a dynamic trailing baseline combined with a separately
sourced activity denominator is a more demanding rule than a static threshold. If the available
`basic`-tier rule types cannot express it, rule 1's **shortfall branch** moves to Grafana while its
**stoppage branch** — a plain no-data condition any rule type can express — still lands now.

Until that verdict is in, no conclusion drawn from log counts is entitled to assume completeness. The
partial mitigation meanwhile is that FRE-1055/1056 remove the known failure's *cause* while its
*detection* is being built — a mitigation, not coverage.

### D3 — An alert must take the owner somewhere to investigate

**Every alert rule carries a deep link to the surface where its condition is investigated, scoped to
the triggering time window and entity.** An alert that says only *something is wrong* is not an alert;
it is an interruption that transfers the whole investigation onto the reader at the worst moment.

Two things follow, and the second is the load-bearing one:

- A rule with no investigation target is **not an alert — it is a dashboard panel**, and belongs there
  instead. This is the test that keeps the set small.
- **The alert set is coupled to the dashboard set, deliberately: a rule cannot be authored before its
  investigation surface exists.** That ordering constraint is the point. It is what prevents shipping
  six rules that each name a problem and offer nowhere to look at it.

### D4 — The alert set

Six conditions. Each names what it catches and where it lands the owner to investigate. The set is
deliberately short: an alert learned-and-ignored is worse than no alert, because it also discredits its
neighbours. **Stage** is assigned by D2a's rule — *now* iff the investigation target already exists or
is a saved Discover query, *FRE-1072* iff a new surface must be built for it.

| # | Condition | Class | Catches | Investigation target | Stage |
|---|---|---|---|---|---|
| 1 | **Stoppage:** no documents for a *cadence* or *correlated* family while the witness shows activity. **Shortfall:** documents per unit of independent activity falls materially (*correlated* families only) | absence + shortfall | Stoppage on every family declaring a cadence; the partial 48–83 % loss measured in FRE-1051 on `api_cost_recorded`, whose denominator already exists | Minimal purpose-built surface: family volume, its denominator, and the witness on one screen | **stoppage now; shortfall now iff the platform can express it, else FRE-1072** (D2a) |
| 2 | A scheduled probe stops writing its result document | absence | A dead probe — the meta-alert that keeps every other rule honest | Saved Discover query on that probe's result index | **now** (joinability, SLM health only — see prerequisite) |

**Staging for rules 1–2 is decided in D2a and restated there in full; where this column and D2a
disagree, D2a governs.** **Since 2026-08-08 the Stage column has one value throughout: Grafana.**
D2a's contingency fired, so rules 1 and 2 join 3–6 there; every *"now, on Kibana"* in this table and
the paragraphs around it describes a stage that was never entered.
| 3 | A probe result reports red | threshold | Joinability orphans, delivery breach, SLM health down — data that already exists and nothing reads | The failing probe's detail panel | FRE-1072 |
| 4 | Spend rate anomaly against the `api_costs` ledger | threshold | Runaway or misattributed cost, on the one substrate with append-only ground truth | Cost surface over Postgres, scoped to the window and model/role | FRE-1072 |
| 5 | Disk or cluster pressure | threshold | The `~10 GiB` box, with a recorded history of index-count and shard pathologies | Cluster/lifecycle surface | FRE-1072 |
| 6 | User-facing turn-failure rate | threshold | Breakage the owner would otherwise discover by hitting it | Turn/error surface, scoped to the window | FRE-1072 |

**Thresholds, windows and baselines are not set here.** Each rule's implementation ticket defines its
own, and they are that ticket's acceptance criteria. This ADR fixes the *conditions* and the
disciplines. **Every rule's ticket carries a positive control** — a stated induction of its condition
that must produce a firing — because a rule tuned so weakly it can never fire is otherwise
indistinguishable from a rule that is simply quiet. AC-6 asserts this for rule 1, which is the only
rule whose *quiet* behaviour is also load-bearing; for rules 2–6 the control lives on their own
tickets, where the threshold it tests is also defined.

**Two failure modes rule 1 must survive, both of which would otherwise defeat it silently.**

- **The false-alarm mode.** Without the "*while the system is active*" qualifier it fires every quiet
  night, and a rule that cries wolf nightly gets muted — leaving the system *worse* than with no rule,
  because the mute looks like coverage.
- **The false-negative mode, and it is the subtler one.** *The witness and the denominator must reach
  the index by an **emission path independent of the one being watched** — independence of path, not of
  storage.* If "the system is active" is inferred from a family shipped by the in-process Elasticsearch
  handler, the missing sink that silences the family silences its witness at the same instant, the rule
  concludes the system was idle, and it stays quiet **exactly when it is needed**.

  Two witnesses satisfy this and are reachable **from a Kibana rule**, which matters for the staging in
  D2a: the **Caddy access-log request rate**, which reaches Elasticsearch through Filebeat's own
  tailing-and-registry path and so cannot be silenced by an unbound in-process handler; and, for the
  `api_cost_recorded` denominator, the `api_costs` row rate — noting Kibana queries only Elasticsearch,
  so using it from the Kibana stage requires that rate to be present in Elasticsearch by an independent
  path, and **if it is not, the shortfall branch waits for Grafana while the stoppage branch still
  lands.** Same-storage is fine; same-path is not.

**Prerequisite for rule 2, stated because it is a real gap and not a detail.** Rule 2 presumes a probe
writes a result document on a known interval. Only **two of four do**: joinability persists via
`observability/joinability/sink.py` and SLM health via `observability/slm_health/sink.py`, and the
`BrainstemScheduler` runs only those two (`brainstem/scheduler.py:1075`, `:1102`). **The cache-erosion monitor and the
delivery-ratio probe are manual CLIs that write no result document at all** — so nothing can alert on
their absence, and the instrument FRE-1051 built to detect telemetry loss is itself neither scheduled
nor persisted. Bringing those two under rule 2 requires scheduling them *and* giving them a result
document; that is its own implementation ticket, and until it lands rule 2's coverage is two probes,
not four.

### D5 — Silence must not read as green

An alerting layer that fires only on bad news makes *no alert* and *the alerting path is broken*
indistinguishable — reproducing FRE-1051's exact conflation one level up, where it would be
correspondingly harder to notice. Two mechanisms close it, and they are separate because they fail
differently:

- **Probe liveness, in-platform** — D4 rule 2. Where a probe writes a result document on a known
  interval, the absence of that document is itself an alert (D1). This catches an instrument dying
  while the platform is healthy. **It covers only the probes that are scheduled and persisted** — two
  of four today, per rule 2's prerequisite above.
- **A dead-man's switch, out-of-platform** — an Elasticsearch-backed rule cannot report that
  Elasticsearch is down, and a Grafana rule cannot report that Grafana is down. This one check is
  therefore a periodic **outward** ping whose *absence* alarms, evaluated somewhere that shares **no
  failure domain** with this stack. It is the single exception to D2's "no application code," and it is
  small by construction: a heartbeat, not a monitor.

  **The heartbeat is emitted through the same contact point the alerts use**, not out of a side
  channel. This is deliberate and it is what makes one cheap mechanism cover two failures: a dead stack
  and a *dead notification path*. An external stack-liveness check that pings independently would stay
  green while every alert sat trapped behind a broken connector — the "alert is a log line" failure
  wearing a different hat. Routing the heartbeat through the real path makes its arrival an **end-to-end
  receipt**: if the owner stops receiving heartbeats, either the stack is down or the thing that would
  have told them is.

  **Three parameters of it are deliberately left to its implementation ticket and must be decided
  there, not defaulted:** the external evaluator, the deadline after which a missed ping alarms, and
  the recipient. Calling a dead-man's switch "fail-safe by design" proves nothing on its own — an
  unconfigured evaluator is silent in exactly the way the mechanism exists to prevent, so its ticket
  carries a **positive-control** criterion: a deliberately withheld ping must produce an alarm.

### D6 — ADR-0090 is to be amended: one done-bar clause added, one stale open decision struck

**These are edits ADR-0090 has not yet received.** They are decided here and applied by their own
implementation ticket; until that ticket merges, ADR-0090's D6 is unchanged and AC-5 is not yet
adjudicable. Three edits, and no fourth corner:

1. **D6 done-bar gains one clause, stated at ADR-0090's own grain.** ADR-0090's D6 binds a new or
   changed **field, family, or dashboard**, so the clause must bind at that grain too — a family-level
   "some event landed" would prove nothing about a changed *field* and nothing at all about a
   dashboard-only change, **recreating the very grain mismatch used to decline the fourth corner.** The
   clause is therefore:

   > For a new or changed field or family: **for each changed field, and from each changed emit path**,
   > a document carrying that field is shown to have landed in the index with the expected type —
   > verified once, at delivery, by whoever ships it. For a **dashboard-only** change the clause does
   > not apply; the existing mapping↔dashboard reconciliation (ADR-0090 D5) already covers it.

   The per-field, per-path quantifier is deliberate: "a document carrying the field" in the singular
   lets a new family pass on one landed field while its siblings never arrive, and lets one working
   producer vouch for a second that binds no sink.

   Not a ratio, not an oracle, not a floor, not a standing job. This is the "verify shape and context
   when the functionality is delivered" half.

   **Its stated limit:** this proves *the shipped path* lands. It cannot prove a *second* producer of
   the same family binds a sink — which is precisely how FRE-1051 happened. That residue is rule 1's
   job, not the done-bar's, and the two are complementary rather than redundant.
2. **The stale open decision is struck.** ADR-0090's open-decisions list still carries the field
   registry as unsettled; **ADR-0133 explicitly declined it.** Citing it as open is drift.
3. **No delivery corner is added.** Its production half is D4 rule 1 of this ADR; its ship-time half is
   edit 1 above. The delivery-ratio probe FRE-1051 built remains as a **diagnostic invoked on
   suspicion**, not a standing gate, and its `0.99` floor is FRE-1051's operational default rather than
   a contract obligation — this ADR sets no delivery SLO.

---

## Alternatives Considered

### Option 1: At-least-once delivery semantics in the logging path

**Description:** Make the log path durable — in-process spool, acknowledgement propagated back from the
Elasticsearch bulk response, retry with backoff, replay de-duplication.

**Pros:** Would make delivery a guarantee rather than an observation; no loss to detect because none
occurs.

**Cons:** Every log call acquires a durability cost. Introduces a spool to size, drain, corrupt and
recover. Replay requires document-level idempotency the schema does not have.

**Why Rejected:** Owner-rejected as disproportionate — *"a heavy ask for a logging platform"* — and
correctly so for a small research harness. It is also aimed at the wrong failure: the measured loss was
never in transit, so no acknowledgement scheme would have prevented it. A process that binds no sink
has nothing to acknowledge.

### Option 2: Ship logs via Filebeat, reusing the deployed shipper

**Description:** Stop shipping from in-process and let a shipper tail the durable file instead.
`telemetry/logger.py:252-254` (inside `configure_logging`, `:217`) already attaches a
`RotatingFileHandler` to `current.jsonl` at `INFO` and above
**unconditionally**, and ADR-0132 / FRE-1146 already deploy Filebeat with a persistent filestream
registry (`docker-compose.cloud.yml:454`, `filebeat_registry_cloud`) that survives restarts. That is
at-least-once machinery, already built and running, currently shipping only Caddy access logs.

**Pros:** At-least-once essentially free — the durable buffer is a file written anyway, so the shipper
only tracks an offset. Structurally dissolves the measured failure: a process that binds no ES handler
still writes its file, so "someone forgot to attach the sink" stops being a failure mode because the
sink becomes the filesystem.

**Cons:** Adds tail latency, a shipper config, a registry volume and disk-retention policy. Replaces a
transport that FRE-1055/1056 are actively hardening, wasting that in-flight work.

**Why Rejected:** Owner-rejected — *"we are not building a kafka pipeline."* Genuinely the most
technically attractive rejected option, and recorded in full so a future reader does not re-derive it:
it is a transport migration, and this ADR's problem is that findings have no *output*, not that the
transport is wrong. Revisit only if delivery loss recurs **after** FRE-1055/1056 land.

### Option 3: Build a notifier in the application (ntfy / email / webhook)

**Description:** Add an outbound notification module and wire the existing instruments to it via
`BrainstemScheduler`.

**Pros:** Works today, on the current stack, with no dependency on FRE-1072. Small first version.

**Cons:** We would own transport, retry, deduplication, grouping, silences, rate-limiting and an
escalation model — all of which Grafana provides free. A hand-rolled notifier with no deduplication
becomes noise on its first flapping condition.

**Why Rejected:** Owner-rejected — *"we are not going to develop readily available functionality… we
will use the functionality of these platforms."* Correct: this is the single clearest case of
re-implementing a solved problem, and the cost is not the first version but every subsequent one.

### Option 4: A delivery-ratio SLO as a standing gate

**Description:** Promote FRE-1051's probe to a standing obligation — per-family delivery measured
against an independent oracle, breaching below a 0.99 floor.

**Pros:** Directly measures the thing that failed; the instrument already exists and already treats
`UNVERIFIABLE` as a first-class verdict.

**Cons:** Only `api_costs` has a validated 1:1 oracle. FRE-1051 explicitly refused to wire the others
because `turn.model_call_completed` runs 2:1 against the ledger and *"would have reported 200 percent
over-delivery every day forever and taught everyone to ignore the monitor."* Nearly every family would
report `UNVERIFIABLE` indefinitely. And a delivery *floor* is an SLO, which invites exactly the
machinery Option 1 was rejected for.

**Why Rejected:** An obligation that is structurally unverifiable for almost every subject is not an
obligation. Absence detection (D1) catches the same failure without needing a twin store for any
family.

### Option 5: Wait for Grafana — defer all alerting to FRE-1072

**Description:** Make Grafana the alerting home and author nothing until FRE-1072 (ADR-0129 B7)
delivers it. This was **the first draft of this ADR's decision**, and it is recorded here as a rejected
alternative because the reasoning that produced it was wrong in an instructive way.

**Pros:** Grafana OSS alerting is unlicensed, has a native `No Data` state, and includes grouping,
deduplication and silences. Rules authored there are not thrown away when Kibana is retired.

**Cons:** Grafana **does not exist yet** and FRE-1072 is `Approved` but not started, so this defers all
detection by an unbounded interval — during which the failure class this ADR exists to catch remains
exactly as invisible as it was when it cost six days of telemetry and was found by accident.

**Outcome, recorded 2026-08-08 because it is uncomfortable and therefore worth stating plainly: this
rejected option is what happened.** D2a's contingency fired on FRE-1187's measurement, so the whole
rule set waits for Grafana after all. **The rejection was not wrong, and this is not a retroactive
reversal.** Option 5 was rejected for *reasoning from a broken instrument* — inferring a licence limit
from an HTTP 500 that was actually a missing encryption key. The staged alternative forced that
instrument to be diagnosed before anything was built on its verdict; the diagnosis then showed the
Kibana stage genuinely could not deliver, for a reason nobody had established. **Arriving at the same
destination by measurement rather than by assumption is the difference this ADR was written to
defend**, and the cost of getting there was one small ticket.

**Why Rejected:** Rejected as the *whole* answer, not as the destination — Grafana remains where the
full set lands (D2). Owner-rejected — *"Grafana does not exist yet."* The draft reached this position
by misreading a broken instrument as a negative result: Kibana's `connector_types` returned HTTP 500,
the draft inferred a licence limitation, and it deferred the entire decision to unbuilt infrastructure.
The 500 was a **missing encryption key** (Context, above) — alerting we already own, switched off by
one unset config value. Deferring everything to a platform five sequenced tickets away, while the
available one sat disabled by a typo-sized omission, holds the detection gap open for no gain;
**diagnose the instrument before accepting its verdict.**

### Option 6: Author the full set on Kibana now and port everything to Grafana later

**Description:** Do not stage. Build all six rules and every investigation surface D3 requires on
Kibana immediately, then rebuild them on Grafana when FRE-1072 lands.

**Pros:** Fastest possible closure of the detection gap; no rule waits on the ADR-0129 chain.

**Cons:** Maximises rework in the expensive place. A Kibana *rule* is cheap to discard, but the
**dashboards built to satisfy D3 are the costly artifact**, and this option builds every one of them
twice on a platform FRE-1072 deletes.

**Why Rejected:** Owner-chose the staged split (D2a) instead. The rework is concentrated in surfaces
rather than rules, so staging by "does this rule need a new surface" captures nearly all of the
benefit at a fraction of the waste.

---

## Consequences

### Positive Consequences

- **The failure class that took six days and was found by accident becomes detectable** — total
  stoppage on every family that declares a cadence, needing only an activity witness; and the *partial*
  loss actually measured on `api_cost_recorded`, using the `api_costs` denominator that already exists.
  **Not without a denominator** — partial loss on a family that has none stays undetected, and that
  limit is declared per family rather than implied.
- **Two instruments acquire an output immediately, and only their *liveness* does** — joinability and
  SLM health are scheduled and persisted, so rule 2 covers them in the Kibana stage. Their *red
  verdicts* are rule 3 and the disk threshold is rule 5, both of which wait for FRE-1072; the
  cache-erosion monitor and delivery-ratio probe need scheduling and a result document first. Stated
  precisely because "five instruments acquire an output" would overstate what this ADR delivers when.
- **The circularity is broken** — findings about telemetry loss stop being filed exclusively into the
  telemetry system whose completeness is in question.
- **No new application subsystem.** D2 keeps the transport where it is already solved; the only code
  this ADR admits is D5's heartbeat.
- **Alerts stay actionable by construction** (D3) — the link requirement is what keeps the set from
  growing into noise, because a condition with nowhere to look fails the bar and becomes a panel.
- **ADR-0090 is left coherent** — one done-bar clause and one stale open decision struck, rather than a
  fourth corner whose grain does not fit its inventory.

### Negative Consequences

- ~~**Two rules are authored twice.**~~ **Never materialised (2026-08-08).** D2a's contingency fired
  before any rule was authored, so rules 1 and 2 land once, on Grafana. Recorded as unrealised rather
  than deleted: this was an accepted cost of a staging decision, and it is worth knowing that staging
  cost nothing in the end because the gating measurement was taken first.
- ~~**Alerting spans two platforms during the interval.**~~ **Never materialised (2026-08-08)** — the
  interval had zero length. There is one alerting platform and no migration step to drop a rule at.
- ~~**One blocking unknown remains**~~ — **resolved 2026-08-07 by FRE-1187, negatively.** The `basic`
  connector set delivers nothing out of the box: 29 connector types, only `.index` and `.server-log`
  enabled, neither leaving the box. This was established first, before rules were authored, precisely
  so it could not be discovered late — and that sequencing is what made the answer cheap.
- **Enabling Kibana alerting required a secret and a restart** — done under FRE-1187 to *take* the
  measurement, not to ship a rule. Retained as a live-service cost that was actually paid; the
  apparatus it created (`.env.kibana` and its custody) is dead weight once Kibana is retired, and
  FRE-1214 removes it.
- **Everything now waits for Grafana** — the outcome rejected Option 5 was written to avoid. It is not
  a design failure but a measured one: the detection gap this ADR exists to close stays open until
  FRE-1072 lands, and no interim coverage is available on the platform we already own.
- **Rule 1's "system is active" qualifier is genuinely hard** and under-specified here, in *both*
  directions: too loose and it false-alarms nightly until it is muted; sourced from the wrong substrate
  and it goes silent precisely when the log path breaks (D4). It is the single hardest thing this ADR
  hands to implementation.
- **Rule 1 depends on a trailing baseline, so it is blind during its own warm-up** and to a loss that
  has persisted long enough to *become* the baseline. A slow, sustained degradation is the residual
  case neither rule 1 nor the ship-time check covers.
- **The dead-man's switch needs somewhere outside this stack to live** — a dependency on infrastructure
  not otherwise required, however small.
- **Alert rules become another version-controlled surface** with an export discipline, inheriting
  exactly the git-vs-live drift problem ADR-0090 D3 already documents for dashboards.
- **The delivery-ratio probe is demoted** from would-be standing gate to on-suspicion diagnostic — but
  it is not discarded: AC-7 uses it as the ground truth for detecting *missed* incidents, which is the
  only false-negative check the design has. That makes scheduling it (rule 2's prerequisite) load-bearing
  for AC-7, not merely tidy.
- **Rule 1 detects shortfall against a family's own history, not against truth.** It is not a
  completeness guarantee: it catches a *change* in delivery, so a family that has always under-delivered
  looks healthy. Only an independent oracle can answer completeness, and only one family has one.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|------|----------|------------|
| Rule 1 false-alarms on idle periods and gets muted; the mute then reads as coverage | High | The activity qualifier is rule 1's core deliverable, not a detail; its ticket's criteria assert quiet-period behaviour explicitly |
| ~~The `basic` connector set turns out to deliver nothing out-of-box, reproducing "the alert is a log line"~~ **— OCCURRED, and the mitigation worked (FRE-1187, 2026-08-07)** | Medium | Established as the **first** implementation step, before any rule was authored; confirmed, so D2a's single contingency applied — the Kibana stage is abandoned and rules 1–2 wait for FRE-1072 with the rest. The risk landed and cost nothing, which is what a mitigation is for |
| Rule 1's activity witness reaches the index by the same emission path it is watching, so a missing sink silences witness and family together and the rule stays quiet | High | D4 requires an independent **emission path** (Caddy-via-Filebeat, not the in-process handler); AC-1 induces exactly this condition |
| Rule 1's shortfall branch false-alarms on ordinary traffic change — deployment, weekday/weekend, request-mix shift — because a binary "active" witness does not normalize volume | High | The shortfall branch requires a *correlated denominator*, not a binary witness (D1); families without one get the stoppage branch only, declared. AC-6 phase A tests quiet behaviour, but tuning against traffic seasonality is rule 1's ticket's own work |
| Rule 1 is blind to a loss that persists long enough to become its own baseline, and during warm-up | Medium | Accepted and stated; the delivery-ratio probe remains the on-suspicion instrument for absolute completeness on the one family with an oracle |
| ~~Rules 1 and 2 are authored on Kibana and thrown away at FRE-1072~~ **— cannot occur (2026-08-08)** | Low | Moot: nothing was authored on Kibana. Retained as a record that the bound (D2a's split) was never tested |
| ~~The Kibana stage is forgotten at migration, leaving rules 1–2 behind on a retired platform~~ **— cannot occur (2026-08-08)** | Medium | Moot: there is nothing on Kibana to leave behind, and no port obligation for FRE-1072's ticket to carry. **The stated mitigation is therefore withdrawn rather than satisfied** — if the stage had been entered, this row's protection would still be untested |
| Partial loss in an oracle-less family stays invisible (absence ≠ completeness) | Medium | Stated as a known limit in Consequences; the FRE-1051 probe remains available as an on-suspicion diagnostic |
| Alert rules drift between the live platform and git | Medium | Rules are version-controlled per D2, inheriting ADR-0090 D3's export discipline |
| The alert set grows until it is ignored | Medium | D3's investigation-link test is the gate; a condition with no target becomes a panel instead |
| The dead-man's switch is itself unmonitored and dies quietly | Low | Its absence *is* its alarm — it fails safe by design |

---

## Implementation Notes

- ~~**First step, and it gates everything else:**~~ **DONE — FRE-1187, merged 2026-08-07.** The key was
  set from a host-local environment file (never committed; the config-guard pre-commit hook enforces
  this), Kibana was restarted under owner authorization, and `/api/actions/connector_types` was
  enumerated: **29 connector types, `.index` and `.server-log` the only two enabled under this `basic`
  licence, neither leaving the box.** The verdict is recorded in
  `docs/research/FRE-1187-kibana-alerting-connector-verdict.md`. **This gated everything else, and it
  closed the gate.**
- ~~**Staged against FRE-1072** (D2a)~~ — **the staging is void (2026-08-08).** All six rules land on
  Grafana with FRE-1072, each with its own dashboard. **No rule is authored on Kibana**, so the "one
  artifact knowingly built twice" is built once. No investigation target for any rule exists today;
  each is authored as part of its own rule's delivery (D3). D1/D3/D4/D5 carry over unchanged because
  none of them names a rule syntax — which is exactly why the platform change costs the contract
  nothing.
- ~~**FRE-1072's ticket must carry the port of rules 1–2**~~ — **withdrawn (2026-08-08).** There is
  nothing on Kibana to port. The obligation is replaced by a simpler one: FRE-1072 (or whichever
  ticket authors the rules) delivers **all six**, not four.
- **Rule 1's `api_costs` denominator has no path into Elasticsearch, confirmed live by FRE-1187** — a
  live index-catalog check found no matching index. On Kibana this was a blocker for the shortfall
  branch (Kibana queries only Elasticsearch). **On Grafana it dissolves**: the Postgres datasource
  reaches `api_costs` directly, so the denominator needs no ES projection at all. Recorded because
  this is a real simplification the platform change hands to rule 1's implementation, not merely a
  relocation of it.
- **Must not collide with FRE-1055 / FRE-1056**, in flight in `build1`, which bind the Elasticsearch
  sink correctly and harden the handler. This ADR consumes their outcome and re-decides none of it.
- **Files touched by the D6 amendment:** `docs/architecture_decisions/ADR-0090-telemetry-surface-contract.md`
  (D6 done-bar clause; strike the field-registry open decision).
- **Prerequisite ticket for rule 2's full coverage:** schedule the cache-erosion monitor and the
  delivery-ratio probe, and give each a persisted result document. Today neither is scheduled
  (`brainstem/scheduler.py` runs joinability and SLM health only) and neither writes a result doc —
  only joinability and SLM health do, via their own `sink.py` modules. AC-7's false-negative cross-check depends on the
  delivery-ratio half of this, so it is load-bearing rather than housekeeping.
- **Existing instruments to wire, not rebuild:** `scripts/monitors/joinability_probe.py`,
  `scripts/monitors/cache_erosion_monitor.py`, `scripts/monitors/delivery_ratio_monitor.py`,
  `src/personal_agent/telemetry/lifecycle_manager.py` (disk threshold), the SLM health probe.
- **Note for whoever schedules probes:** `delivery_ratio_monitor.py` has no cron entry, no CI job and
  no Makefile target, unlike `joinability_probe` and `cache_erosion_monitor` which both have Makefile
  targets. It is invoked by documentation only.
- **Kibana defect worth its own ticket:** `/api/actions/connector_types` returns HTTP 500 on the live
  8.19.0 instance. Filed separately; it blocks confirming Option 5's premise either way.

---

## Verification / Acceptance Criteria

**How will we know this decision actually delivered — not just merged?**

**How the destructive checks are run, once, for all of them.** Inductions are performed on the
**isolated test substrate** (FRE-375: ES `:9201`, Postgres `:5433`) against a **dedicated synthetic
family** with a controllable producer, never by sabotaging production. Where a criterion names the
*covered set*, that set is the **committed per-family declaration** required by D1's shortfall table —
reconciled against the families actually present in `agent-logs-*`, so it cannot be quietly narrowed;
an undeclared family present in the index is itself a failure.

- **AC-1** — **Every** family in the declared covered set alerts out-of-band on stoppage, **every family
  declared shortfall-capable** also alerts on shortfall, and neither goes quiet when the log path is
  what broke. · **Check:** on the test substrate, for each covered family, induce (a) total stoppage and
  (b) — for shortfall-capable families — a **47.6 % shortfall**, the *smallest* loss actually measured
  in FRE-1051, holding the independent denominator constant. Confirm an out-of-band notification for
  each. Then induce a whole-log-path stoppage and confirm the rule still fires.

  Three further checks bind the **real** configurations, because a synthetic family proves only that the
  mechanism works: (i) reconcile the declaration against the live family list; (ii) for each *cadence*
  and *correlated* family, confirm its configured query, denominator and witness each resolve and return
  a non-empty series over a recent healthy window — a rule whose denominator query is broken is
  permanently quiet and otherwise indistinguishable from health; (iii) **backtest the shortfall branch
  against FRE-1051 itself** — given the recorded per-day counts for 2026-07-23..28 (1303 oracle rows
  against 899 indexed documents, reproduced by the committed delivery-ratio tests), the configured rule
  must flag the 23rd, 26th and 27th and **not** flag the 24th, 25th and 28th. · *Fails if* any covered family alerts on neither; **if the
  47.6 % induction passes silently** — a threshold tuned to catch only catastrophic loss misses the real
  incident, and a no-data-only implementation fails here outright; if the notification lands only in an
  index document or server log; if the rule goes quiet because its witness died with the stream it was
  watching; if a family present in `agent-logs-*` appears in no declaration row; if any real family's
  denominator or witness query returns nothing over a healthy window; **or if the FRE-1051 backtest
  misses any of the three bad days or flags any of the three clean ones** — a rule that cannot
  retrodict the incident it was built for has not been shown to detect it.

- **AC-2** — Every rule's investigation link lands on **the evidence that triggered it**, not merely on
  a page that loads. · **Check:** for each rule, from a firing — natural, or induced on the test
  substrate — follow the link; the surface must show the data that satisfied that rule's condition for
  the triggering window and entity. For rule 1 that means **all three series**: family volume, its
  denominator, and the witness. · *Fails if* the link resolves but is scoped to a different window or
  entity, omits any series the condition depends on, or is an unscoped home dashboard. **HTTP 200 is not
  a pass**, and a single-series view of a ratio-based rule is not a pass.

- **AC-3** — **Every** probe in the committed probe inventory is covered by liveness detection, and the
  inventory accounts for all five. · **Check:** the inventory names all five instruments — joinability,
  SLM health, cache erosion, delivery ratio, disk usage — each marked *covered* or *not covered with a
  stated reason*. For each covered probe, stop it and confirm an alert **within its own declared
  interval plus its own declared margin**, both of which the inventory states. · *Fails if* any covered
  probe can stop without an alert; if an alert arrives later than the declared bound; if any of the five
  is absent from the inventory; **or if a probe is marked "not covered" for no reason other than that it
  was left unscheduled** — that is shrinking the denominator rather than closing the gap, and the
  delivery-ratio probe is the specific case at risk, since AC-7 depends on it running.

- **AC-4** — Loss of the alerting path is reported from outside that path — **including loss of the
  notification path itself.** · **Check:** on the test substrate, four separate inductions — stop
  Elasticsearch; stop the rule-evaluating platform; break the outbound connector/contact point while
  rules keep firing; and withhold the heartbeat while everything else stays up. · *Fails if* **any** of
  the four produces no alarm. The third is the one an external stack-liveness check passes while every
  application alert stays trapped in-platform, which is the "alert is a log line" failure wearing a
  different hat. Also *fails if* the evaluator, deadline and recipient are unconfigured — an
  unconfigured dead-man's switch is silent in exactly the way the mechanism exists to prevent.

- **AC-5** — ADR-0090's amended done-bar discriminates a landing path from a non-landing one, at field
  grain. · **Check:** first, ADR-0090 must actually carry the D6 clause — *this ADR does not amend it;
  its own ticket does*, and until that merges AC-5 is inconclusive, never green. Then a matched pair on
  the test substrate: add a field to the synthetic family from a producer that binds **no** sink and
  apply the amended done-bar as written; then add the same field from a producer that **does** bind one
  and apply it again. Record both outcomes. · *Fails if* the first **passes** — whether because the emit
  call exists in source, or because *other* documents of the same family landed while the changed field
  never did — or if the second **fails**. A check that cannot tell the two apart verifies nothing, and
  a criterion adjudicated against an unamended ADR-0090 verifies nothing either.

- **AC-6** — Rule 1 is quiet through genuine idle **and** both its branches fire when their conditions
  are genuinely present. · **Check:** two clearly separated phases, recorded as such. *Phase A* — a
  stated multi-night no-traffic window with no induction; count rule-1 firings (expect zero).
  *Phase B* — on the test substrate, induce **stoppage** and, separately, **shortfall**, naming the
  family used; confirm a firing for each. · *Fails if* it fires during phase A, **or if either phase-B
  branch does not fire.** Testing only the stoppage branch would let a no-data-only implementation with
  a broken or absent shortfall branch pass — the exact defect round 1 of review found in the design
  itself.

- **AC-7** — Over 30 days of live operation, firings were **true** rather than merely handled, and the
  one family that can be independently checked was not missed. · **Check:** three parts. (a) Each firing
  is classified **true positive** (a real condition existed) or **false positive** (it did not), with
  the evidence; a rule whose firings are majority false positives fails, *whether or not* they were
  attended to — "acted on" records diligence, not correctness. (b) Run the delivery-ratio probe over the
  same 30 days **for `api_cost_recorded`, the only family with a validated oracle**, and compare against
  rule 1's firings **at rule 1's own configured threshold**, not the probe's `0.99` floor — the two use
  different semantics and comparing them directly would reject a correct implementation. Any breach
  beyond rule 1's threshold with no corresponding firing is a missed incident. (c) If **no** condition
  occurred naturally in 30 days, that is a pass **only** on evidence the set is live: every rule's
  recorded positive control (AC-6 phase B for rule 1, each rule's own ticket for 2–6) plus continuous
  heartbeat receipt across the window. · *Fails if* any rule's firings are majority false positives; if
  the cross-check finds a miss; **or if the window was quiet and the liveness evidence in (c) is
  incomplete** — a quiet month is either a healthy system or a dead rule set, and only (c) tells them
  apart.

  **Stated limit, so the verdict is not over-read:** this false-negative check covers **one family and
  one rule**. The probe's three other declared families are unwired for want of a validated join
  (`observability/delivery_ratio/collect.py:56`), and rules 2–6 have no independent ground truth at all.
  A green AC-7 means *the one thing that can be checked was not missed* — not that nothing was missed.
  *(Assembled, long-horizon and owner-involving — permitted for an ADR's own criteria under ADR-0130 D1.)*

**Seam ticket:** **FRE-1185** — *ADR-0134 SEAM — adjudicate the activity-alerting criteria*.
**Due date: 2026-11-30.**

The date is **gated by FRE-1072, not only by the observation windows** — and **more strongly so since
2026-08-08**, when D2a's contingency fired. The seam holds *all* seven criteria, and where four of the
six rules previously waited on Grafana, **now all six do**: no rule lands on Kibana, so AC-1's
per-family inductions and AC-3's probe-liveness coverage join AC-2's full walk, AC-4's platform
induction and AC-7's cross-check in waiting for the Grafana stage. FRE-1072 sat roughly five sequenced
tickets behind the ADR-0129 chain head when this date was set. **The date does not move on this
change** — it was already set by the FRE-1072 dependency plus AC-7's 30-day window — but its rationale
is now stronger rather than weaker, and adjudicating any part of the set early has become even less
meaningful. On top of that, AC-6 needs a
multi-night idle window and AC-7 needs 30 days of live operation *after* the full set is running.
2026-11-30 is the earliest plausible date on that chain and is an estimate, not a measurement: **if
FRE-1072 lands materially earlier or later, master resets this date rather than adjudicating early
against an incomplete set.** Adjudicating a subset would produce inconclusive verdicts on the rest,
which is the outcome the single-seam rule exists to avoid.

Filed parked (`Backlog`, no `stream:` label); master activates it at the first advance-dispatch on or
after the due date, and an `adr` session adjudicates it. This ADR does not close because its last child
merged.

---

## References

- ADR-0090 — Telemetry Surface Contract (emit ↔ mapping ↔ dashboard); amended by D6 above. Status: Accepted.
- ADR-0129 — OpenTelemetry Instrumentation and Trace Visibility; its B7 phase (FRE-1072) brings Tempo and Grafana. Kibana's retirement is **not** FRE-1072's — it is directed by ADR-0129 D6's 2026-08-08 amendment and delivered by FRE-1214 under the FRE-1203 program. Status: Accepted.
- ADR-0133 — The Typed Emit Envelope for the Residual Log Corpus; declined the field registry that ADR-0090 still lists as an open decision. Status: Proposed.
- ADR-0132 — Egress chain (Caddy → Filebeat → DomainGuard); the deployed Filebeat and its persistent filestream registry referenced in Alternatives Option 2. Status: Accepted.
- ADR-0088 — Execution-topology emission seam; the emission complement to ADR-0090's surface contract. Status: Accepted.
- ADR-0130 — Acceptance-criteria hierarchy; D1/D2 govern the seam ticket above. Status: Accepted.
- ADR-0074 — End-to-end traceability; the joinability probe referenced in D4 rule 3. Status: Accepted.
- Linear FRE-1058 — this ADR's ticket (ADR-0090's fourth corner), Observability Foundation.
- Linear FRE-1051 — the delivery measurement, the corrected diagnosis, and the probe. Done 2026-07-31.
- Linear FRE-1055 / FRE-1056 — Elasticsearch handler hardening and sink binding; in flight, fix the measured cause.
- Linear FRE-1072 — ADR-0129 B7, Tempo + Grafana; the destination for the full rule set. It does **not** retire Kibana and no longer carries a port obligation for rules 1–2, since D2a's Kibana stage was abandoned before any rule was authored.
- Linear FRE-1187 — ADR-0134's first implementation step: set the encryption key, restart, enumerate. Its measurement (29 connectors, two enabled, none leaving the box) is what fired D2a's contingency. Done 2026-08-07.
- Linear FRE-1214 / FRE-1203 — the Kibana retirement ticket and the Grafana migration program that owns it; the reason nothing in this ADR should be built on Kibana.
- Linear FRE-1202 — the recorded drift that line 121 and the two references above still said FRE-1072 "retires Kibana"; resolved by this amendment.
- `docs/research/FRE-1187-kibana-alerting-connector-verdict.md` — the committed per-connector inventory with licence state; the evidence D2a's contingency fired on.
- Linear FRE-533 — the 1023-row three-way reconciliation inventory whose grain is discussed in Context.
- Linear FRE-1039 — Grafana over Postgres for aggregate cost and ledger truth; D4 rule 4's surface.
- Code: `src/personal_agent/telemetry/lifecycle_manager.py:183,206` — the alert that is a log line.
- Code: `src/personal_agent/config/settings.py:1537` — `disk_usage_alert_percent`, the only alert threshold.
- Code: `src/personal_agent/service/app.py:754` — the single conditional `add_elasticsearch_handler` call site.
- Code: `src/personal_agent/telemetry/logger.py:252-254` — the unconditional `RotatingFileHandler` attachment inside `configure_logging` (`:217`), referenced in Option 2.
- Code: `src/personal_agent/observability/joinability/sink.py` and `src/personal_agent/observability/slm_health/sink.py` — the only two probe result-document writers; `src/personal_agent/brainstem/scheduler.py:1075,1102` — the only two scheduled probes (rule 2's prerequisite).
- Code: `src/personal_agent/transport/agui/transport.py:596` — the connected-client event push, which is not an out-of-band alert transport.
- Code: `src/personal_agent/observability/delivery_ratio/` + `scripts/monitors/delivery_ratio_monitor.py` — FRE-1051's probe, demoted to on-suspicion diagnostic by D6.
- Config: `docker-compose.cloud.yml:160` — the Kibana service, with no `xpack.encryptedSavedObjects.encryptionKey` set; `docker/kibana/kibana.yml` is the mounted config it would go in.
- External: [Kibana — configure alerting (`xpack.encryptedSavedObjects.encryptionKey` is required for alerting and actions)](https://www.elastic.co/guide/en/kibana/current/alerting-setup.html)
- External: [Grafana unified alerting — No Data and Error handling](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rules/state-and-health/)
- External: [Elastic subscriptions — connector availability by tier](https://www.elastic.co/subscriptions) (consulted 2026-08-07; does not state the tier boundary for action connectors, which is why D2 makes it a measurement)

---

## Status Updates

### 2026-08-08 — D2a's Kibana stage abandoned: the contingency fired

**Changed By:** `adr` session (FRE-1213).
**Reason:** Two separate things landed on this ADR, and they are recorded separately because only one
of them is a change of mind.

**First — D2a's stated contingency fired, which is this ADR working rather than being overtaken.**
D2a wrote the condition itself: *"If the `basic` connector set proves to contain nothing that leaves
the box … the Kibana stage is abandoned outright: rules 1 and 2 wait for FRE-1072 with the rest."*
FRE-1187 — commissioned by this ADR as its first implementation step precisely so the question would
be measured rather than assumed — set the encryption key, restarted Kibana, and enumerated
`/api/actions/connector_types`: **29 connector types, exactly two enabled under this `basic` licence
(`.index` and `.server-log`), neither of which leaves the box.** Every connector that does requires
at least a gold licence. The recorded verdict is *abandon the Kibana stage*.

**No decision was reversed here.** The staging predicate, the contract (D1, D3, D4, D5) and the alert
set are untouched; none of them names a rule syntax, which is exactly why the platform change costs
the contract nothing. What changed is which branch of a two-branch decision the evidence selected.

**The consequences are recorded as unrealised rather than deleted.** Two Negative Consequences ("two
rules are authored twice", "alerting spans two platforms") and two risk rows never materialised,
because the gating measurement was taken *before* any rule was authored. They are struck through and
annotated rather than removed: a design that priced a cost, then avoided paying it by sequencing the
measurement first, is worth being able to read later. **One mitigation is withdrawn rather than
satisfied** — the "Kibana stage is forgotten at migration" row's protection was never tested, and
recording it as satisfied would overclaim.

**Rejected Option 5 is what happened, and it is labelled as such.** The full rule set now waits for
Grafana — the outcome Option 5 proposed and this ADR rejected. The rejection still stands and the
reason is the distinction worth keeping: Option 5 was rejected for *reasoning from a broken
instrument*, inferring a licence limitation from an HTTP 500 that was really a missing encryption
key. Reaching the same destination by measurement rather than by assumption, at the cost of one small
ticket, is the difference this ADR was written to defend.

**One real simplification comes with the platform change.** FRE-1187 confirmed live that the
`api_costs` denominator has no path into Elasticsearch. On Kibana that blocked rule 1's shortfall
branch outright, since Kibana queries only Elasticsearch. On Grafana it dissolves — the Postgres
datasource reads `api_costs` directly — so rule 1 lands whole rather than split across branches.

**Second — a drift correction that predates all of this (FRE-1202).** Line 121 and two References
stated that FRE-1072 *"retires Kibana"*. That was never true of FRE-1072: ADR-0129's 2026-08-07
amendment explicitly retained Kibana, and its retirement is now directed by that ADR's 2026-08-08
amendment and delivered separately by FRE-1214 under the FRE-1203 Grafana migration program.
Corrected in all three places. **The Context paragraph's distance-to-Grafana reasoning is left as
written** — it is what the staging decision rested on at authoring time, and rewriting the reasoning
to match its outcome would destroy the record of why the measurement was commissioned at all.

**What this does not change.** The seam ticket (FRE-1185) and its 2026-11-30 due date stand; the date
was already gated by FRE-1072 plus AC-7's 30-day window. Its rationale is *strengthened* — where four
of six rules previously waited on Grafana, now all six do — which makes early adjudication less
meaningful, not more.

### 2026-08-07 - Proposed
**Changed By:** `adr` session (FRE-1058)
**Reason:** Authored after owner-led design discussion. The ticket's premise — add a fourth *delivery*
corner to ADR-0090 — was rejected in discussion on two grounds: FRE-1051's corrected diagnosis showed
the loss was never in transit, and absence does not share the grain, artifact or check-location of
ADR-0090's three corners. Four successively lighter designs (at-least-once semantics, a Filebeat
transport migration, an in-house notifier, a delivery SLO) were each raised and rejected by the owner
as disproportionate for a small research harness, converging on platform-native alerting with absence
as the missing signal class.

**Correction made before this ADR was opened, recorded because the error is the instructive part.** The
first draft made Grafana the alerting home and stated that no actionable alerting was possible until
FRE-1072 landed. That rested on reading Kibana's HTTP 500 from `/api/actions/connector_types` as a
`basic`-licence limitation. The owner's challenge — *"Grafana does not exist yet"* — prompted an actual
diagnosis, which found the API disabled by a **missing `xpack.encryptedSavedObjects.encryptionKey`**,
not by licensing. **A broken instrument had been accepted as a negative result** — the same failure
mode FRE-1051 exists to end, committed inside the ADR written to address it.

The second draft then over-corrected, treating Grafana as incidental and Kibana as the platform. The
owner's *"Grafana is planned, there are tickets for it"* settled it: Grafana is the destination, the
distance to it is real, and the answer is **staged delivery (D2a)** — owner-chosen from three options,
splitting on whether a rule needs a new investigation surface, so that the artifact thrown away at
migration is a rule definition and never a dashboard.

### 2026-08-07 - Revised after Codex review round 1

**Changed By:** `adr` session (FRE-1058)
**Reason:** Adversarial review returned findings that invalidated part of the decision. The material
ones, and what changed:

- **Rule 1 would not have caught FRE-1051.** The measured incident was *partial* loss — the family
  still flowed at 17–52 % of the oracle on the bad days — so a pure no-data rule would have stayed
  quiet, while the ADR simultaneously claimed it caught "FRE-1051's exact failure" and admitted
  elsewhere that partial loss stayed invisible. D1 and D4 rule 1 now specify **shortfall against the
  family's own trailing baseline, including to zero.**
- **The activity qualifier could silence the rule.** If "the system is active" is read from
  `agent-logs`, a missing sink kills the witness and the family together, suppressing the alert exactly
  when needed. D4 now requires an **independent substrate** for the witness; AC-1 induces the case.
- **Rule 2 presumed coverage that does not exist.** Only joinability and SLM health are scheduled and
  persist a result document; the cache-erosion monitor and the delivery-ratio probe are manual CLIs
  writing nothing — so the instrument FRE-1051 built cannot itself be alerted on. Stated as a
  prerequisite rather than assumed away.
- **D6's amendment recreated the grain mismatch used to decline the fourth corner** — a family-level
  "some event landed" proves nothing about a changed *field*. Re-scoped to field grain, with
  dashboard-only changes explicitly excluded.
- **Every acceptance criterion was rewritten.** Six of seven were satisfiable by a broken
  implementation: single-subject checks that a one-family or one-probe implementation passed, a link
  check that HTTP 200 satisfied, a quiet-period check a disabled rule passed, and a noise check that
  passed vacuously when nothing fired. They now carry full-set scope, paired positive controls, and in
  AC-7 a false-negative cross-check against the delivery-ratio probe.
- **The seam's due date rationale was false** — it claimed no platform dependency while four rules need
  FRE-1072. Moved to 2026-11-30 with the dependency stated.

### 2026-08-07 - Revised after Codex review round 2

**Changed By:** `adr` session (FRE-1058)
**Reason:** Round 2 found that round 1's central fix did not hold, plus several fixes that were
cosmetic. The material ones:

- **Shortfall cannot be detected from a family's own history.** Volume tracks traffic, so "half as many
  documents" is equally consistent with *half the events were lost* and *half as many requests
  arrived*. Telling them apart needs an independent denominator — the oracle relationship in weaker
  form, which round 1 claimed to avoid. **D1 now states two branches with honestly different coverage:**
  stoppage for every family, shortfall only for families with a declared correlated denominator. This is
  less than the previous draft promised.
- **"Independent substrate" was the wrong requirement** and made the Kibana stage infeasible — Kibana
  queries only Elasticsearch. What independence actually requires is a different **emission path**;
  Caddy-via-Filebeat qualifies and is Kibana-reachable.
- **ADR-0090 was never actually amended.** D6 said it "gains" the clause while the commit touched only
  this ADR, leaving AC-5 with nothing authoritative to adjudicate. D6 now states the edits are applied
  by their own ticket, and AC-5 is inconclusive until it merges.
- **Rule 1's investigation target cannot be a saved Discover query** — AC-2 demands volume, denominator
  and witness together. Rule 1 gets a minimal three-series surface, named as the one artifact knowingly
  built twice, rather than pretending the staging is free.
- **The criteria demanded production sabotage.** All inductions now run on the FRE-375 test substrate
  against a synthetic family. AC-1's shortfall induction is pinned to **47.6 %** — the smallest loss
  actually measured — so a threshold tuned to catch only catastrophic loss fails.
- **AC-7's cross-check was narrower than claimed**: the probe has a validated oracle for one family, and
  its breach semantics (`0.99`) differ from rule 1's threshold. Both stated; the criterion now says what
  it does *not* cover.
- Corrected: SLM health has its own `sink.py` (two persisting probes, not one); "three instruments
  acquire an output" overstated what lands in the Kibana stage.

### 2026-08-07 - Revised after Codex review round 3 (final round)

**Changed By:** `adr` session (FRE-1058)
**Reason:** Six blocking items, all accepted:

- **"Stoppage for every family" would have alerted on healthy silence.** Conditional and rare families
  — an error event, a rollback — are silent when working. D1 now requires each family to declare
  *cadence*, *correlated* or *conditional*, and only the first two get a stoppage rule. A family with
  **no** declaration is the failure; a family declared *conditional* is not.
- **D2a and D4 disagreed on rule 1's staging.** D4's table now carries the split explicitly (stoppage
  now; shortfall now only if the platform can express it) and states that D2a governs on conflict.
- **Residual "without an oracle" claims struck**, including in Positive Consequences, which still
  advertised what D1 had already retracted.
- **AC-1 tested only a synthetic family**, so a real family's broken denominator or witness query would
  pass. It now also reconciles the declaration, requires every real family's queries to return data over
  a healthy window, and **backtests the shortfall branch against FRE-1051's own recorded counts** — the
  rule must flag the 23rd/26th/27th and not the 24th/25th/28th.
- **AC-4 demanded a connector-failure alarm that D5 never provided.** D5 now routes the heartbeat
  *through the alert contact point*, making its arrival an end-to-end receipt: one mechanism covers a
  dead stack and a dead notification path.
- **AC-7 accepted false positives as long as they were handled, and failed a genuinely quiet month.**
  Firings are now classified true or false positive, and a zero-incident window passes on recorded
  positive controls plus continuous heartbeat receipt.
