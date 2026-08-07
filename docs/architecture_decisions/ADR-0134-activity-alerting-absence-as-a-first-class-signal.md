# ADR-0134: Activity Alerting — Absence as a First-Class Signal, on Platform-Native Alerting

**Status:** Proposed
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
  and persist result documents (`observability/joinability/sink.py`); the **disk-usage threshold** runs
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

**FRE-1072** (ADR-0129 B7) brings Tempo and Grafana and retires Kibana. Grafana OSS unified alerting
carries no licence gate and has a first-class **No Data** alert state, so it is the destination for the
full rule set — **planned and ticketed, not hypothetical.** It is also not close: the ADR-0129 chain is
at B2 (FRE-1065 In Progress), with B3–B8 approved behind it, putting FRE-1072 roughly five sequenced
tickets out. That distance, against a failure class that already ran six days undetected, is what
makes the staging in D2a a real decision rather than a formality.

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
47.6 % of the oracle. A pure no-data rule **would not have fired on FRE-1051.** Any claim that
absence detection catches the measured failure is false unless the condition also covers **shortfall
against the family's own trailing baseline**. D4 rule 1 is therefore specified as *volume falls
materially below its own recent baseline, including to zero* — one condition spanning both, needing no
oracle and no twin store, because the baseline is the family's own history.

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

The contract lands now; the rules land in two stages, and **the criterion for which stage a rule falls
into is whether it requires a new investigation surface** (D3):

- **Now, on Kibana** — rules whose investigation target needs **no new dashboard**: the data view and
  the underlying data already exist, so the target is a **saved Discover query**, authorable in
  minutes and discarded without loss. Rules 1 and 2 qualify, and they are the two that bear on
  FRE-1051's failure.
- **On Grafana, with FRE-1072** — the full set, including every rule that needs a **new dashboard**
  built to satisfy D3.

**This does not weaken D3, and the distinction is exact.** D3 forbids a rule shipping without a
resolvable investigation target; it does not require that target to pre-exist the ticket. The saved
Discover queries for rules 1 and 2 do **not** exist today — the only committed monitor saved search is
probe-specific — so authoring them is **part of** each rule's own delivery, and D3 is satisfied because
the rule and its target ship together. What D2a stages on is the **cost and durability of the target**:
a saved query is minutes and disposable, a dashboard is the expensive artifact, and **no dashboard is
built twice.** The contract itself (D1, D3, D4, D5) names conditions, disciplines and targets — never a
rule syntax — so it survives the migration untouched.

**One open question, now measurable rather than assumed, with a single stated contingency.** If the
`basic` connector set proves to contain nothing that leaves the box — index and server-log connectors
only — then a Kibana alert is a log line, which is the failure this ADR exists to end, and **the Kibana
stage is abandoned outright: rules 1 and 2 wait for FRE-1072 with the rest.** No half-measure, no
notification routed to a platform that does not exist yet. **Establishing which connectors this licence
exposes is therefore the first implementation ticket and gates the whole Kibana stage.** It is small:
set the key, restart, enumerate.

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
| 1 | An `agent-logs` family's volume falls materially below its own trailing baseline, **including to zero**, while the system is active | absence + shortfall | Both total stoppage *and* the partial 48–83 % loss actually measured in FRE-1051 — with no oracle, no twin store and no delivery floor | Saved Discover query on the family, scoped to the affected window | **now** |
| 2 | A scheduled probe stops writing its result document | absence | A dead probe — the meta-alert that keeps every other rule honest | Saved Discover query on that probe's result index | **now** (joinability, SLM health only — see prerequisite) |
| 3 | A probe result reports red | threshold | Joinability orphans, delivery breach, SLM health down — data that already exists and nothing reads | The failing probe's detail panel | FRE-1072 |
| 4 | Spend rate anomaly against the `api_costs` ledger | threshold | Runaway or misattributed cost, on the one substrate with append-only ground truth | Cost surface over Postgres, scoped to the window and model/role | FRE-1072 |
| 5 | Disk or cluster pressure | threshold | The `~10 GiB` box, with a recorded history of index-count and shard pathologies | Cluster/lifecycle surface | FRE-1072 |
| 6 | User-facing turn-failure rate | threshold | Breakage the owner would otherwise discover by hitting it | Turn/error surface, scoped to the window | FRE-1072 |

**Thresholds, windows and baselines are not set here.** Each rule's implementation ticket defines its
own, and they are that ticket's acceptance criteria. This ADR fixes the *conditions* and the
disciplines; a rule tuned so weakly that it can never fire fails AC-6's positive control, which is
where that is caught.

**Two failure modes rule 1 must survive, both of which would otherwise defeat it silently.**

- **The false-alarm mode.** Without the "*while the system is active*" qualifier it fires every quiet
  night, and a rule that cries wolf nightly gets muted — leaving the system *worse* than with no rule,
  because the mute looks like coverage.
- **The false-negative mode, and it is the subtler one.** *The activity witness must be drawn from a
  substrate other than the one being watched.* If "the system is active" is inferred from `agent-logs`
  itself, then the missing sink that silences the family silences its activity witness at the same
  instant — the rule concludes the system was idle and stays quiet **exactly when it is needed**.
  Acceptable witnesses are independent of the log path: the `api_costs` row rate in Postgres, or the
  request rate in the Caddy access logs Filebeat already ships. This is the same independence property
  that makes an oracle an oracle, applied to the qualifier rather than the measurement.

**Prerequisite for rule 2, stated because it is a real gap and not a detail.** Rule 2 presumes a probe
writes a result document on a known interval. Only **two of four do**: joinability persists via
`observability/joinability/sink.py` and SLM health has its own family, and the `BrainstemScheduler`
runs only those two (`brainstem/scheduler.py:1075`, `:1102`). **The cache-erosion monitor and the
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

  **Three parameters of it are deliberately left to its implementation ticket and must be decided
  there, not defaulted:** the external evaluator, the deadline after which a missed ping alarms, and
  the recipient. Calling a dead-man's switch "fail-safe by design" proves nothing on its own — an
  unconfigured evaluator is silent in exactly the way the mechanism exists to prevent, so its ticket
  carries a **positive-control** criterion: a deliberately withheld ping must produce an alarm.

### D6 — ADR-0090 gains one done-bar clause and loses one stale open decision

Three edits to ADR-0090, and no fourth corner:

1. **D6 done-bar gains one clause, stated at ADR-0090's own grain.** ADR-0090's D6 binds a new or
   changed **field, family, or dashboard**, so the clause must bind at that grain too — a family-level
   "some event landed" would prove nothing about a changed *field* and nothing at all about a
   dashboard-only change, **recreating the very grain mismatch used to decline the fourth corner.** The
   clause is therefore:

   > For a new or changed **field or family**: a document carrying **that field**, emitted by **the
   > changed code path**, is shown to have landed in the index with the expected type — verified once,
   > at delivery, by whoever ships it. For a **dashboard-only** change the clause does not apply; the
   > existing mapping↔dashboard reconciliation (ADR-0090 D5) already covers it.

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

- **The failure class that took six days and was found by accident becomes detectable** — and detectable
  without an oracle, a twin store or a delivery SLO.
- **Three existing instruments acquire an output immediately** — the joinability probe, SLM health probe
  and disk threshold, which already run and already produce a verdict nothing reads. The cache-erosion
  monitor and delivery-ratio probe need scheduling and a result document first (rule 2's prerequisite),
  so their coverage is a consequence of that follow-on ticket, not of this ADR alone.
- **The circularity is broken** — findings about telemetry loss stop being filed exclusively into the
  telemetry system whose completeness is in question.
- **No new application subsystem.** D2 keeps the transport where it is already solved; the only code
  this ADR admits is D5's heartbeat.
- **Alerts stay actionable by construction** (D3) — the link requirement is what keeps the set from
  growing into noise, because a condition with nowhere to look fails the bar and becomes a panel.
- **ADR-0090 is left coherent** — one done-bar clause and one stale open decision struck, rather than a
  fourth corner whose grain does not fit its inventory.

### Negative Consequences

- **Two rules are authored twice.** Rules 1 and 2 land on Kibana and are re-authored on Grafana at
  FRE-1072. Accepted deliberately (D2a): the discarded artifact is a rule definition, not a dashboard,
  and the alternative is leaving FRE-1051's failure class undetected for five sequenced tickets.
- **Alerting spans two platforms during the interval**, which is a real if temporary split-brain: two
  places to look for a rule, and a migration step that must not silently drop one.
- **One blocking unknown remains** — whether the `basic` connector set can deliver anything out of the
  box. If it cannot, the Kibana stage is worthless and everything waits for Grafana after all. This is
  established first, before rules are authored, precisely so it cannot be discovered late.
- **Enabling Kibana alerting requires a secret and a restart.** The encryption key must come from the
  environment rather than committed config, and it is a live-service change — master's call, not a
  worker's.
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
| The `basic` connector set turns out to deliver nothing out-of-box, reproducing "the alert is a log line" | Medium | Established as the **first** implementation step, before any rule is authored; if confirmed, D2a's single contingency applies — the Kibana stage is abandoned and rules 1–2 wait for FRE-1072 with the rest |
| Rule 1's activity witness is read from `agent-logs`, so a missing sink silences the witness and the family together and the rule stays quiet | High | D4 requires the witness to come from an independent substrate (`api_costs` rows, Caddy request rate); AC-1's check induces exactly this condition |
| Rules 1 and 2 are authored on Kibana and thrown away at FRE-1072 | Low | Bounded by D2a's split: only rules needing *no new surface* land early, so no dashboard is built twice |
| The Kibana stage is forgotten at migration, leaving rules 1–2 behind on a retired platform | Medium | FRE-1072's own ticket carries the port as an explicit obligation; the seam ticket's AC-2 walk would catch an unresolvable link |
| Partial loss in an oracle-less family stays invisible (absence ≠ completeness) | Medium | Stated as a known limit in Consequences; the FRE-1051 probe remains available as an on-suspicion diagnostic |
| Alert rules drift between the live platform and git | Medium | Rules are version-controlled per D2, inheriting ADR-0090 D3's export discipline |
| The alert set grows until it is ignored | Medium | D3's investigation-link test is the gate; a condition with no target becomes a panel instead |
| The dead-man's switch is itself unmonitored and dies quietly | Low | Its absence *is* its alarm — it fails safe by design |

---

## Implementation Notes

- **First step, and it gates everything else:** set `xpack.encryptedSavedObjects.encryptionKey` for
  Kibana, restart, then enumerate `/api/actions/connector_types` and record which connectors this
  `basic` licence actually exposes. The key is a **secret** — it comes from the environment, never from
  committed config (the config-guard pre-commit hook enforces this) — and the restart is a live-service
  change, so it is master's to authorize.
- **Staged against FRE-1072** (D2a). Rules 1 and 2 land on Kibana now because their investigation
  targets already exist; the rest land on Grafana with FRE-1072, along with the surfaces D3 requires.
  D1/D3/D4/D5 carry over unchanged because none of them names a rule syntax.
- **FRE-1072's ticket must carry the port of rules 1–2** as an explicit obligation, or the Kibana stage
  is orphaned on a retired platform.
- **Must not collide with FRE-1055 / FRE-1056**, in flight in `build1`, which bind the Elasticsearch
  sink correctly and harden the handler. This ADR consumes their outcome and re-decides none of it.
- **Files touched by the D6 amendment:** `docs/architecture_decisions/ADR-0090-telemetry-surface-contract.md`
  (D6 done-bar clause; strike the field-registry open decision).
- **Prerequisite ticket for rule 2's full coverage:** schedule the cache-erosion monitor and the
  delivery-ratio probe, and give each a persisted result document. Today neither is scheduled
  (`brainstem/scheduler.py` runs joinability and SLM health only) and neither writes a result doc —
  only `observability/joinability/sink.py` does. AC-7's false-negative cross-check depends on the
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

- **AC-1** — **Every** family in the committed covered set alerts out-of-band on **both** total stoppage
  **and** material shortfall, and does so even when the log path itself is the thing that broke.
  · **Check:** for each covered family, in a live window with the system active, induce (a) total
  stoppage and (b) a ~50 % shortfall against baseline, by unbinding or throttling its producer; confirm
  a notification arrives out-of-band for each. Then induce a whole-log-path stoppage — the condition
  under which an `agent-logs`-derived activity witness would itself go silent — and confirm the rule
  still fires. · *Fails if* any covered family alerts on neither; **if a ~50 % shortfall passes silently
  while only total stoppage alerts** (that is the FRE-1051 case, and a no-data-only rule fails here);
  if the notification lands only in an index document or server log; if the rule goes quiet because its
  activity witness died with the stream it was watching; or if the covered set was made passable by
  shrinking it rather than by alerting.

- **AC-2** — Every rule's investigation link lands on **the evidence that triggered it**, not merely on
  a page that loads. · **Check:** from an *actual* firing of each rule, follow the link; the surface
  must show the documents — or the documented absence — that satisfied that rule's condition, for the
  triggering window and entity. · *Fails if* the link resolves but is scoped to a different window or
  entity, shows nothing bearing on the condition, or is an unscoped home dashboard. **HTTP 200 is not a
  pass.**

- **AC-3** — **Every** scheduled, result-persisting probe is covered by liveness detection.
  · **Check:** enumerate the probes that are scheduled *and* write a result document; stop each in turn
  and wait its interval plus margin; confirm one alert per probe. · *Fails if* any such probe can stop
  without an alert, **or if coverage was achieved by shrinking the denominator** — leaving a probe
  unscheduled or unpersisted so it falls outside the set rather than alerting on it.

- **AC-4** — Loss of the alerting path is reported from outside that path. · **Check:** in controlled
  windows, three separate inductions — stop Elasticsearch; stop the rule-evaluating platform
  (Kibana/Grafana); and withhold the heartbeat while everything else stays up. · *Fails if* **any** of
  the three produces no alarm, or if the evaluator, deadline and recipient are unconfigured — an
  unconfigured dead-man's switch is silent in exactly the way the mechanism exists to prevent, so its
  silence proves nothing.

- **AC-5** — ADR-0090's amended done-bar discriminates a landing path from a non-landing one, at field
  grain. · **Check:** a matched pair. Introduce a new field on an existing family from a process that
  binds no sink and run the ship-time check; then introduce the same field from the service path and
  run it again. · *Fails if* the first **passes** — whether because the emit call exists in source, or
  because other documents of the same family landed while the changed field never did — or if the
  second **fails**. A check that cannot tell the two apart verifies nothing.

- **AC-6** — Rule 1 is quiet through genuine idle **and** fires when the condition is genuinely present.
  · **Check:** over a stated multi-night no-traffic window, count rule-1 firings (expect zero); within
  the same period, induce the condition once and confirm it fires. · *Fails if* it fires on ordinary
  quiet hours, **or if it does not fire on the induced condition** — the paired positive control is
  what excludes a disabled, mis-tuned or never-evaluated rule, which would otherwise pass the quiet
  half trivially.

- **AC-7** — Over 30 days of live operation, every firing was worth the interruption **and** nothing
  that should have fired stayed silent. · **Check:** two halves. (a) Each firing has an owner
  disposition — acted on, or the rule amended. (b) Run the delivery-ratio probe over the same 30 days
  and cross-check: any family it reports as breaching that produced **no** corresponding rule-1 firing
  is a missed incident. · *Fails if* any rule fired repeatedly and was neither acted on nor changed; if
  the probe finds a breach the alert set missed; **or if no rule fired at all and no induced control was
  recorded** — zero firings is not evidence of health, it is the shape of a dead rule set.
  *(Assembled, long-horizon and owner-involving — permitted for an ADR's own criteria under ADR-0130 D1.)*

**Seam ticket:** **FRE-1185** — *ADR-0134 SEAM — adjudicate the activity-alerting criteria*.
**Due date: 2026-11-30.**

The date is **gated by FRE-1072, not only by the observation windows.** The seam holds *all* seven
criteria, and four of the six rules (AC-2's full walk, AC-4's platform induction, AC-7's cross-check
across the whole set) cannot be adjudicated until the Grafana stage lands — FRE-1072 currently sits
roughly five sequenced tickets behind the ADR-0129 chain head. On top of that, AC-6 needs a
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
- ADR-0129 — OpenTelemetry Instrumentation and Trace Visibility; its B7 phase (FRE-1072) brings Grafana and retires Kibana. Status: Accepted.
- ADR-0133 — The Typed Emit Envelope for the Residual Log Corpus; declined the field registry that ADR-0090 still lists as an open decision. Status: Proposed.
- ADR-0132 — Egress chain (Caddy → Filebeat → DomainGuard); the deployed Filebeat and its persistent filestream registry referenced in Alternatives Option 2. Status: Accepted.
- ADR-0088 — Execution-topology emission seam; the emission complement to ADR-0090's surface contract. Status: Accepted.
- ADR-0130 — Acceptance-criteria hierarchy; D1/D2 govern the seam ticket above. Status: Accepted.
- ADR-0074 — End-to-end traceability; the joinability probe referenced in D4 rule 3. Status: Accepted.
- Linear FRE-1058 — this ADR's ticket (ADR-0090's fourth corner), Observability Foundation.
- Linear FRE-1051 — the delivery measurement, the corrected diagnosis, and the probe. Done 2026-07-31.
- Linear FRE-1055 / FRE-1056 — Elasticsearch handler hardening and sink binding; in flight, fix the measured cause.
- Linear FRE-1072 — ADR-0129 B7, Tempo + Grafana + Kibana retirement; the destination for the full rule set under D2a, and the ticket that must carry the port of rules 1–2.
- Linear FRE-533 — the 1023-row three-way reconciliation inventory whose grain is discussed in Context.
- Linear FRE-1039 — Grafana over Postgres for aggregate cost and ledger truth; D4 rule 4's surface.
- Code: `src/personal_agent/telemetry/lifecycle_manager.py:183,206` — the alert that is a log line.
- Code: `src/personal_agent/config/settings.py:1537` — `disk_usage_alert_percent`, the only alert threshold.
- Code: `src/personal_agent/service/app.py:754` — the single conditional `add_elasticsearch_handler` call site.
- Code: `src/personal_agent/telemetry/logger.py:252-254` — the unconditional `RotatingFileHandler` attachment inside `configure_logging` (`:217`), referenced in Option 2.
- Code: `src/personal_agent/observability/joinability/sink.py` — the only probe result-document writer; `src/personal_agent/brainstem/scheduler.py:1075,1102` — the only two scheduled probes (rule 2's prerequisite).
- Code: `src/personal_agent/transport/agui/transport.py:596` — the connected-client event push, which is not an out-of-band alert transport.
- Code: `src/personal_agent/observability/delivery_ratio/` + `scripts/monitors/delivery_ratio_monitor.py` — FRE-1051's probe, demoted to on-suspicion diagnostic by D6.
- Config: `docker-compose.cloud.yml:160` — the Kibana service, with no `xpack.encryptedSavedObjects.encryptionKey` set; `docker/kibana/kibana.yml` is the mounted config it would go in.
- External: [Kibana — configure alerting (`xpack.encryptedSavedObjects.encryptionKey` is required for alerting and actions)](https://www.elastic.co/guide/en/kibana/current/alerting-setup.html)
- External: [Grafana unified alerting — No Data and Error handling](https://grafana.com/docs/grafana/latest/alerting/fundamentals/alert-rules/state-and-health/)
- External: [Elastic subscriptions — connector availability by tier](https://www.elastic.co/subscriptions) (consulted 2026-08-07; does not state the tier boundary for action connectors, which is why D2 makes it a measurement)

---

## Status Updates

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
