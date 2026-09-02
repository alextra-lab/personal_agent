# FRE-1354 — restart the self-improvement promotion loop

Ticket: [FRE-1354](https://linear.app/frenchforest/issue/FRE-1354) · Tier-1:Opus · stream:build2
ADRs: ADR-0030 (Captain's Log dedup + promotion) · ADR-0040 (Linear async feedback channel) ·
ADR-0105 (self-improvement pipeline, D4/D6/D7/D9/D10) · ADR-0125 D1 (`source` required)

## Measured starting state (verified in-session, 2026-09-02, read-only)

| Fact | Instrument | Observed |
| -- | -- | -- |
| Live proposals | `sysgraph.proposal` on `cloud-sim-postgres` | 28 rows, 19 with `seen_count >= 3` |
| Top proposal | same | `4d254cb53508e2a2`, `statistical_detector`/`performance`/`llm_client`, `seen_count=167`, created 2026-07-07 |
| Promotion linkage | `sysgraph.promoted_to` / `sysgraph.ticket` | 0 / 0 |
| Group grain | `GROUP BY source, category, scope` | 28 groups, 1 proposal each — the (source, category, scope) dedup is working |
| Open `agent-filed` | live Linear GraphQL, `state.type nin (completed, canceled, duplicate)` | **4** (FRE-359/360/361/362) |
| Open `Improvement` | same | **0** (all 14 are Done or Canceled) |
| `IssueFilter.description.containsIgnoreCase` | live Linear GraphQL probe | **supported** — returns matches |

Two findings the ticket did not have, both load-bearing:

**F1 — the fingerprint dedup has never been able to match.**
`_existing_linear_issue_for_fingerprint` (promotion.py:529) passes `query=fingerprint` to
`list_issues`, which maps `query` → `title: {containsIgnoreCase: …}` (linear_client.py:457).
The fingerprint lives in the **description**, never the title, so the lookup returns the empty
list on every call and the pipeline always takes the create branch. This is why 2026-06-26
produced nine tickets rather than one.

**F2 — the 2026-06-26 flood was six *distinct* fingerprints of one idea.**
FRE-623…628 all carry the title prefix `[performance] Address insight pattern: Reflection
proposes c…` and six different fingerprints (`4d254cb5…`, `044d870d…`, `6b203fb9…`,
`891b2ef1…`, `99483fd9…`, `7a878c1e…`). Fingerprint dedup could never have collapsed them —
only the (source, category, scope) grouping in `read_before_emit` can, and it now does.
So AC-7's "admit at most one" is delivered by the grouping, provided the surviving entry
carries the **canonical row's** fingerprint rather than this sighting's.

**F3 — FRE-623 carries fingerprint `4d254cb53508e2a2`**, i.e. the exact identity of the live
167-count proposal, and it is `Canceled` (non-archived). Per the FRE-620 disposition rule
documented on `_existing_linear_issue_for_fingerprint`, a cancelled ticket is a permanent
tombstone: once the fingerprint lookup works, that proposal must **link to FRE-623, not create
a new ticket**. See "AC-1, stated precisely" below.

## Scope (5 bullets)

1. **Wall 1** — a reinforced proposal whose corroboration crosses the promotion bar keeps its
   `proposed_change` (stamped with the canonical row's `seen_count`/`fingerprint`/`first_seen`)
   instead of being erased. Applied to all six read-before-emit producers, not just reflection.
2. **Wall 2** — retire `issue_budget_threshold`; gate on ≤10 open **Seshat-created** tickets.
3. **Wall 3** — the configured promotion project is resolved and fails loudly; default set to a
   project that exists.
4. **AC-3/AC-6** — the refusal lands on the existing Grafana funnel panel; one marker label on
   both creation paths.
5. **F1** — fix the fingerprint dedup so the cap and the dedup actually compose (AC-7).

Out of scope, per the ticket: the read-before-emit *decision* logic itself. The branch
conditions are untouched; the result object is widened and what the caller does afterwards
changes.

## AC-1, stated precisely (flagged for master)

AC-1 asks that the live `seen_count: 167` proposal be shown "producing a Linear issue". With
F3 established, producing a **new** ticket for it would resurrect an idea the owner explicitly
cancelled (FRE-623), which is the behaviour FRE-620's tombstone rule exists to prevent. The
deliverable therefore demonstrates the broken step — *the corroborated proposal now reaches
promotion* — on the real 167-count identity, and both of its correct outcomes:

- **AC-1a** — the real `4d254cb53508e2a2` / 167 proposal reaches the promotion decision and
  **links to FRE-623**, no duplicate filed. (Today it never reaches the decision at all.)
- **AC-1b** — a real, above-bar, reinforced proposal with **no** tombstone —
  `ca5b48205324ad16`, `reflection`/`performance`/`orchestrator`, `seen_count=54`, whose
  fingerprint appears on no Linear issue — **creates** a ticket.

Neither is a synthetic first-sighting; both run the reinforced path that has been dead for
eight weeks.

## Design

### D1 — the read-before-emit result carries corroboration

`sysgraph.repository.ReadBeforeEmitResult` and `sysgraph.dedup.ReadBeforeEmitResult` each gain
`seen_count: int | None`, `fingerprint: str | None`, `first_seen: datetime | None`, defaulting
to `None`. `_FIND_AWAITING_PROPOSAL_QUERY` also selects `created_at`;
`_REINFORCE_PROPOSAL_QUERY` gains `RETURNING seen_count` so the value returned is
post-increment (not `existing + 1` computed client-side). Populated on the `reinforced` branch
only.

### D2 — one shared corroboration rule

New `src/personal_agent/captains_log/corroboration.py`:

- `suppresses_proposal(result, *, min_seen_count) -> bool`
  - `DECIDED_SKIP` → `True` (a decided kind never re-promotes; unchanged).
  - `REINFORCED` and `seen_count >= min_seen_count` → `False`.
  - `REINFORCED` below the bar → `True` (unchanged behaviour, and AC-2's negative).
  - `GENERATE_NEW` / `DEGRADED_GENERATE_NEW` → `False` (unchanged).
- `stamp_corroboration(pc, result) -> ProposedChange` — returns a `model_copy(update=…)` with
  the canonical `seen_count`, `fingerprint` and `first_seen`. The canonical fingerprint is what
  makes every later sighting map to the same ticket (F2).

Six call sites use it: `captains_log/reflection.py`, `insights/engine.py`, and the four
detector handlers behind `events/pipeline_handlers.py::_read_before_emit_suppresses` (renamed
to `_read_before_emit_decision`, returning the result instead of a bool).

### D3 — the cap counts Seshat's own open tickets (AC-5/AC-6)

New `src/personal_agent/linear_labels.py` holds the single marker:
`AGENT_AUTHORED_LABEL = "agent-filed"` (+ its colour). `tools/linear.py` imports it in place of
its private constant; `promotion.py` adds it to the created label set
(`["PersonalAgent", "Improvement", AGENT_AUTHORED_LABEL]`), so both creation paths carry it.
`Improvement` is retained on the promotion path for continuity with the 14 historical tickets
and ADR-0030, but it is **not** the counting predicate.

`LinearClient.count_open_agent_issues(team)` = non-terminal (`state.type nin
completed/canceled/duplicate`) **and** `labels.some.name in [AGENT_AUTHORED_LABEL]`, paginated.
`count_open_issues` is removed — this change is what orphans it.

Settings: `issue_budget_threshold` deleted; `seshat_open_ticket_cap: int = 10` and
`promotion_min_seen_count: int = 3` added. `feedback.py`'s daily budget probe moves to the new
counter. `PromotionCriteria.min_seen_count` defaults from `promotion_min_seen_count`, so the
bar reflection un-strips at and the bar promotion admits at are one number.

Gate is `count >= cap` (owner: "no more than 10"), and the per-run creation slice is
`min(promotion_initial_cap, cap - count)` so a single run cannot overshoot the cap.

### D4 — the refusal reaches the dashboard (AC-3)

`config/grafana/dashboards/self_improvement_funnel.json` panel 2 already exists and queries
`es-agent-logs` for `event_type: "throttled_budget"`. That value has never appeared in
`agent-logs`: the funnel document carries `event_type` but goes to
`agent-captains-funnel-events-*`, while the structlog line carries
`event: issue_budget_promotion_paused`. The panel has therefore always been blank — which is
exactly "the gate has been shut for an unknown period and nobody knew".

Fix without hand-authoring dashboard JSON: emit `event_type="throttled_budget"` as a **field on
the structlog warning**. `agent-logs-*` is `dynamic: true` and already maps `event_type` as
`keyword`, so the existing panel renders with no dashboard change. The funnel-index document is
kept and reused for the project-misconfiguration refusal (`event_type="misconfigured_project"`),
whose structlog line carries the same field.

### D5 — the promotion project is validated (AC-4)

`settings.linear_promotion_project` default → `"Linear Async Feedback Channel"` (verified to
exist; ADR-0040's own project). `LinearClient.create_issue` raises `LinearProjectNotFoundError`
when a non-empty project name does not resolve, instead of logging a warning and filing
project-less. `PromotionPipeline.run()` resolves the project **before** creating anything and
refuses the run with the visible `misconfigured_project` event when it is absent.

### D6 — the fingerprint dedup actually queries the description (F1)

`LinearClient.list_issues` gains a `descriptionQuery` filter →
`description: {containsIgnoreCase: …}` (verified supported). `_existing_linear_issue_for_
fingerprint` uses it and drops the label filter, so it matches both the historical
`Improvement`-only tombstones and newly marked tickets. The existing description re-scan is
kept as the confirming check.

## Steps

| # | Step | Verify |
| -- | -- | -- |
| 1 | Failing tests for AC-1a/1b, AC-2, AC-3, AC-4, AC-5, AC-6, AC-7 in `tests/test_captains_log/test_promotion_corroboration.py` (+ `test_linear_client.py` additions) | `make test-file FILE=tests/test_captains_log/test_promotion_corroboration.py` → red |
| 2 | D1 — widen both `ReadBeforeEmitResult`s, `RETURNING seen_count`, select `created_at` | targeted tests green |
| 3 | D2 — `corroboration.py` + six call sites | AC-1/AC-2 green |
| 4 | D3 — `linear_labels.py`, `count_open_agent_issues`, settings swap, `feedback.py` probe | AC-5/AC-6 green |
| 5 | D4 — `event_type` on the throttle warning + funnel doc | AC-3 green |
| 6 | D5 — project validation + default | AC-4 green |
| 7 | D6 — `descriptionQuery` + dedup fix | AC-7 green |
| 8 | Update `tests/test_captains_log/test_promotion.py` (3 sites patch the deleted setting), `test_feedback_loop.py`, `test_fre_1219_validator_agreement.py`, `test_linear_client.py` count tests | `make test` |
| 9 | Docs: `.env.example`, regenerate `CONFIG_INVENTORY.md` AppConfig table, ADR-0105 line 21, funnel ES template `_meta` | `uv run python scripts/audit/config_inventory.py verify` exits 0 |
| 10 | Gates + self-review | `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files` |

## Acceptance criteria → evidence

| AC | How it is proven |
| -- | -- |
| AC-1a | `test_live_167_proposal_reaches_promotion_and_links_to_tombstone` — real fingerprint/source/category/scope/seen_count; asserts the entry is scanned promotable and resolves to FRE-623 with `create_issue_fn` never called |
| AC-1b | `test_live_54_proposal_without_tombstone_creates_issue` — real reflection proposal; asserts `create_issue_fn` called once with the canonical fingerprint in the description |
| AC-2 | `test_reinforced_below_bar_is_still_suppressed` — `seen_count=2`; `proposed_change is None`, `scan_promotable_entries() == []` |
| AC-3 | `test_cap_refusal_is_visible_on_the_funnel_panel` — asserts the structlog warning carries `event_type="throttled_budget"` (the field the committed dashboard panel queries) **and** the funnel-index document is written |
| AC-4 | `test_unknown_promotion_project_refuses_loudly` — `create_issue` raises; pipeline returns `[]` and emits `misconfigured_project`; asserts no issue created |
| AC-5 | `test_cap_counts_only_agent_authored_open_issues` — asserts the GraphQL filter (marker label + non-terminal); 4 → proceeds, 10 → refuses |
| AC-6 | `test_both_creation_paths_carry_one_marker` — asserts `tools/linear.py` and `promotion.py` both apply `AGENT_AUTHORED_LABEL`, and that the counting query filters on that same constant |
| AC-7 | `test_nine_near_duplicates_admit_at_most_one` — nine proposals sharing (source, category, scope) through a repo double implementing the documented grouping → one row at `seen_count=9`, one promotable entry, one ticket; and at cap, the refusal is visible |

## Revisions after codex plan-review (2026-09-02)

Ten findings accepted; the two marked **blocking** would have shipped a broken deliverable.

**R1 (blocking) — `_merge_into_existing` discards the authoritative corroboration.**
`manager.py:398` does `pc["seen_count"] = pc.get("seen_count", 1) + 1`, ignoring the incoming
value. Stamping the sysgraph count onto the `ProposedChange` is therefore thrown away on save: a
live proposal at 167 whose local file sits at 1 becomes **2**, not 168, and still fails
`min_seen_count=3`. Verified directly at `manager.py:398-401`. The merge must take the
authoritative count and the earlier `first_seen`. Without this, AC-1 does not work end to end —
the original plan's D2 was necessary but not sufficient.

**R2 (blocking) — `event_type=` as a structlog kwarg is prohibited.** `event_type` is *not* in
`_RESERVED_EVENT_KEYS` (`es_handler.py:100`), so a payload field of that name is copied into
`event_data` and then wins in `es_logger.py:237`'s `{"event_type": event_type, **data}` —
overwriting the semantic event name. That is precisely the FRE-1066 defect, and
`test_es_handler.py:789` guards against it; the fix there was at the *call site*
(`payload_event_type=`), not a rename in the handler. Verified: no `payload_event_type` handling
exists in `es_handler.py`/`es_logger.py`. **Corrected approach:** name the structlog event
itself `throttled_budget`, so `event_type` derives from the event name (`es_handler.py:478`)
with no payload override. The committed panel then renders, the invariant holds, and the
descriptive detail moves into fields (`reason`, `current_count`, `threshold`).

**R3 — `_mark_promoted` swallows its write error** (`promotion.py:625`) while
`_finalize_promotion` records success anyway. That is the one genuinely uncontained re-fire
loop: the entry stays `AWAITING_APPROVAL` and re-promotes every run. `_mark_promoted` now
returns a bool; a failed status write is not counted as promoted. Self-healing, because the
fixed fingerprint dedup links the next run to the already-created issue instead of duplicating.

**R4 — the framing of containment was wrong.** Repeat promotion after a successful promotion is
terminated by the Captain's Log merge-into-`APPROVED` file (rejected at `promotion.py:249`'s
status check), **not** by the ten-ticket cap. The plan no longer credits the cap with that.

**R5 — `fingerprint` is redefined, not silently repurposed.** No consumer recomputes and
compares the hash (Codex enumerated all of them; all treat it as opaque identity), but
`models.py:197` documents it as `sha256(category:scope:normalized_what)[:16]`, which canonical
stamping makes false. Rather than add a second identity field to every consumer, the field is
redefined and documented as *the canonical proposal identity — the content hash at first
sighting, carried forward for later sightings of the same (source, category, scope) group*.
`compute_proposal_fingerprint`'s docstring is updated to match.

**R6 — the cap fails closed.** A count-query exception currently logs and proceeds
(`promotion.py:345`). For a governance cap that is the wrong direction: it now refuses the run
and emits the visible event.

**R7 — dedup links no longer consume creation capacity.** The Linear lookup sits inside the
capped slice (`promotion.py:352`), so a link — which creates nothing — can eat the last slot and
starve a genuinely new candidate. Capacity is now consumed only when `_create_linear_issue` is
actually reached.

**R8 — `handle_duplicate()` has the same F1 defect** (`feedback.py:347` passes the fingerprint
as `query` → title search). Folded into the same one-line fix.

**R9 — public `resolve_project_id`.** The only resolver is private `_project_id`; validation and
creation both use the new public method.

**R10 — accepted, not fixed: the cap is not concurrency-safe.** `run()` reads the count once and
then creates. Two concurrent runs could both see capacity. Deliberately not fixed: the pipeline
has a single scheduled trigger (`consolidation.completed`), and the per-run creation slice
bounds any overshoot to at most `promotion_initial_cap`. Distributed locking over a best-effort
optional sysgraph connection would be more machinery than the risk warrants. Recorded here and
in the handoff rather than silently ignored.

**R11 — the removal sweep is wider than step 9 said**, and `cap - 20` goes *negative* at cap 10.
Full set: `.env.example:537` · `CONFIG_INVENTORY.md:184` · `docs/guides/LINEAR_FEEDBACK_LOOP.md:26`
· `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md:49,623` · `ADR-0105:21` ·
`captains-funnel-events-index-template.json:23` · `scripts/audit/telemetry_surface_baseline.json:44`
· `test_promotion.py:598,626,664,695,835` · `test_linear_client.py` `TestCountOpenIssues` ·
`test_feedback_loop.py:79` · `test_fre_1219_validator_agreement.py:130`. Historical research/plan
docs are left immutable. The feedback warning becomes `count >= ceil(cap * 0.8)`.
`count_open_issues` is removed — this change is what orphans it.

**Added tests** (Codex's list): runs 2/3/10 through the real `save_entry()`; missing-local-file
recovery; `_mark_promoted` failure; stale-local vs authoritative count; count-query failure;
dedup links not consuming capacity; feedback duplicate description search; and the throttle
assertion driven through the real `emit() → log_event()` path rather than mocked kwargs.

## Risks

- **Re-promotion volume.** 19 live proposals sit at or above the bar. First run files
  `min(5, 10-4) = 5`; the next fills to 10 and then the cap holds. Governed, visible, and the
  behaviour the owner asked for — but master should expect ~5 new `Needs Approval` tickets on
  the first post-deploy consolidation.
- **Deleting `issue_budget_threshold`** touches `CONFIG_INVENTORY.md` parity (guarded by
  `tests/scripts/test_config_inventory.py`) — step 9 regenerates it.
- **Diff class: escalated** — this is governance/cost-adjacent code on a production write path
  (it files Linear tickets). Flagged for owner `/code-review ultra` before merge.
