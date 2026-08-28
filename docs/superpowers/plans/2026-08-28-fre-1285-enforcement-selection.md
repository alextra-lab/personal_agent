# FRE-1285 — Enforcement selection: light/heavy keyed on measured compliance

**Ticket:** [FRE-1285](https://linear.app/frenchforest/issue/FRE-1285) · **ADR:** ADR-0138 D5 ·
**Consumes:** FRE-1284 (`grounding/compliance.py`, `grounding_compliance_observations`)

## Scope (5 bullets)

1. A **pure selection module** that maps a compliance *rate* — never a model name — onto
   light/heavy, with a hysteresis band, a post-demotion cooldown, and probation sampling.
2. **Durable state** for the two things a rate alone cannot express: the standing level (so an
   in-band reading holds rather than flaps) and the demotion instant (so cooldown is real).
3. **Wiring into the turn path**: selection runs once per turn in `step_llm_call`, after the
   answering deployment key is stamped and *before* generation; heavy appends the
   forced-retrieval directive and reserves the tool iterations to act on it.
4. **Widening `retrieval_forced`** from "this generation followed a D4 retry" to "…or ran under
   heavy" — `compliance.py`'s own docstring names this ticket as the widener. Without it, heavy
   turns re-enter the metric and AC-5's oscillation returns.
5. Tests proving each of AC-1…AC-6 at the **outcome** level.

## Design decisions, and why

**Promote threshold is the existing `grounding_compliance_bar` (0.95), not a new setting.**
ADR-0138 D5 requires promote ≠ demote, not promote ≠ contract bar. Reusing the pre-registered bar
means the promote line cannot silently drift away from the contract it is supposed to represent;
only the lower edge (`demote_below`, 0.90) is new.

**The selection function takes `rate: float | None`, not a model key.** AC-2 says "inspect the
selection input". The strongest form of that is an input with no identity field in it at all, so
renaming a model cannot change the answer because the name never reaches the decision.

**Heavy is a hard gate — `tool_choice="required"` — not only a directive.** The first draft used a
volatile-tail directive plus an iteration grant; codex plan-review finding 3 correctly rejected it.
The executor receives a generation *before* it executes any tool, so a model that ignores the
directive composes its assertion with an empty source registry — which is precisely the state
ADR-0138 D5 says heavy makes unreachable ("cannot compose an assertion without a source set already
in hand"). Pinning `tool_choice="required"` on heavy's first generation makes the first thing the
model may emit a tool call, not prose. The plumbing exists: `tool_choice` is already threaded
executor → `respond()` → adapters, and `_forced_synthesis_tool_overrides` is the standing precedent
for the executor pinning it (FRE-484 pins `"none"`).

The directive and the iteration grant stay — the gate makes retrieval *happen*, the directive tells
the model what to retrieve *for*, and the grant means a turn that spent its tool budget legitimately
still has an iteration to retrieve with (FRE-1282's reasoning, unchanged).

**The gate degrades loudly, never silently.** It is applied only when tools are actually offered,
the strategy is NATIVE, and the turn is not force-synthesizing — the same three conditions under
which `tool_choice` reaches a backend at all (`client.py` nulls it otherwise). When any fails,
heavy falls back to directive-only and logs `grounding_heavy_gate_unavailable`, so a deployment
where the gate is not reaching the model is visible rather than a quiet downgrade to the design
codex just rejected.

**The directive goes in the volatile tail, not the system prompt.** `GROUNDING_CONTRACT_PROMPT`
sits inside the ADR-0081 cached static prefix; a clause varying per model per level would fragment
the prompt cache along a new axis. Appending a user-role message is what D4's retry already does.

**Selection applies only in `enforce` mode.** In `observe` mode nothing blocks and nothing is
forced, so every turn is unconfounded — which is the bootstrap FRE-1284's setting description
already describes. Forcing retrieval in a mode that promises not to change behaviour would be a
lie about the mode.

**Failure is fail-safe to heavy.** A DB read error, a missing row, or a misconfigured band all
resolve to heavy, logged. Unmeasured ≡ heavy is D5's bootstrap; a broken instrument is at most as
trustworthy as no instrument.

**A state transition is awaited, not backgrounded** (codex plan-review finding 4). The first draft
fire-and-forgot the upsert. Losing it is *directionally* safe — the next turn recomputes the same
transition from the same rate, and a lost promotion leaves the model heavy — but a lost demotion
loses the **cooldown stamp**, which is the one piece of state no later turn can reconstruct. Since a
transition happens on the order of once per hundreds of turns, awaiting it costs essentially nothing
and makes cooldown durable. Concurrent turns are handled by an `ON CONFLICT DO UPDATE … WHERE
excluded.updated_at > stored.updated_at` guard, so a slower turn holding a stale reading cannot
clobber a newer transition.

**Observation-write lag is an accepted property, not a hole.** FRE-1284 writes observations in the
background, so the immediately preceding turn may be absent from this turn's window. That is a
one-observation lag against a 100-observation window with a 30-sample floor; it can shift a reading
by at most 1/30 and cannot change a level except exactly at a threshold, where the hysteresis band
already absorbs it. Recorded rather than designed around.

## Steps

### 1 — `src/personal_agent/grounding/enforcement_selection.py` (new, pure)

`EnforcementLevel{LIGHT,HEAVY}` · `SelectionReason{UNMEASURED,DEMOTED,BELOW_BAND_HOLD,BAND_HOLD,
COOLDOWN_HOLD,PROMOTED,ABOVE_BAND_HOLD}` · `EnforcementBand(promote_at, demote_below, cooldown,
probation_rate)` frozen, validator `promote_at > demote_below` · `EnforcementState(level,
demoted_at)` frozen · `EnforcementSelection(applied, standing, reason, probation, changed)`.

`select_enforcement(*, rate, standing, band, now, rng=None) -> EnforcementSelection`. Every row
below reads `standing` as the **input** state and produces the **post-transition** state:

| Reading | Input standing | Result |
| -- | -- | -- |
| `rate is None` | LIGHT | heavy, `UNMEASURED`, **`demoted_at=now`** |
| `rate is None` | HEAVY | hold heavy, `UNMEASURED`, `demoted_at` preserved |
| `rate >= promote_at` | LIGHT | hold light, `ABOVE_BAND_HOLD` |
| `rate >= promote_at` | HEAVY, demoted within cooldown | hold heavy, `COOLDOWN_HOLD` |
| `rate >= promote_at` | HEAVY, never demoted or cooldown elapsed | **promote**, `demoted_at=None` |
| `rate < demote_below` | LIGHT | **demote**, `demoted_at=now` |
| `rate < demote_below` | HEAVY | hold heavy, `BELOW_BAND_HOLD` (no re-stamp — one demotion, one cooldown) |
| in band | any | hold, `BAND_HOLD` |

Then, against the **post-transition** standing:
`probation = post.level is HEAVY and rng.random() < band.probation_rate`;
`applied = LIGHT if probation else post.level`.

**Cooldown is stamped on every LIGHT→HEAVY transition, not only the below-band one** (codex
plan-review finding 1). A model that goes stale is a model that stopped producing evidence; letting
it re-promote the instant it rebuilds a window — with no cooldown, because nothing "demoted" it —
is the promotion-without-earning path D5's cooldown exists to prevent. A model that has *never*
been light keeps `demoted_at=None` and is promotable as soon as it is measured, which is the
correct reading of "a **demoted** model serves a cooldown".

`configured_band()` reads settings. `build_forced_retrieval_directive()` returns heavy's
pre-generation text. Module `_RNG = random.Random()` per `entailment_sampling`'s precedent.

→ verify: `make test-file FILE=tests/personal_agent/grounding/test_enforcement_selection.py`

### 2 — Settings (`config/settings.py`, after the D5 metric block)

`grounding_enforcement_demote_below: float = 0.90` ·
`grounding_enforcement_cooldown_hours: int = 24` ·
`grounding_enforcement_probation_rate: float = 0.10`.

Probation default documented with its arithmetic: at 0.10 with `min_samples=30`, an unmeasured
model needs ~300 turns carrying a non-exempt span before it can be measured at all. That is the
bootstrap cost of the unconfounded-measurement rule, and it is stated rather than discovered.

### 3 — Persistence

- `docker/postgres/migrations/0030_grounding_enforcement_state.sql` — `grounding_enforcement_state
  (model_key PK, level, demoted_at NULL, updated_at)`; `GRANT SELECT … TO grafana_ro`; idempotent.
- Mirror into `docker/postgres/init.sql`.
- `GroundingEnforcementStateModel` in `service/models.py`.
- `service/repositories/grounding_enforcement_repository.py` — `get(model_key)`,
  `upsert(model_key, state, now)` via `ON CONFLICT DO UPDATE`.

→ verify: repository unit test against the test Postgres, plus a migration-idempotency re-run.

### 4 — Executor wiring (`orchestrator/executor.py`, `orchestrator/types.py`)

- `types.py`: `grounding_enforcement: EnforcementSelection | None = None`.
- `_select_enforcement(ctx, trace_ctx)` — once per turn, `enforce` mode only, immediately after
  `ctx.answering_model_key = effective_model_key`. Reads the window, classifies via
  `compliance.classify`, selects, logs `grounding_enforcement_selected`, persists in the
  background **only when `changed`**, and on heavy appends the directive + adds the retrieval
  grant. Whole body wrapped: any exception → heavy, `log.exception`.
- `_record_grounding`: `retrieval_forced = ctx.grounding_attempts > 1 or applied is HEAVY`.

→ verify: `make test-file FILE=tests/personal_agent/orchestrator/test_grounding_enforcement_wiring.py`

### 5 — Docs

`grounding/__init__.py` module map entry.

**Not** an ADR-0138 edit. The plan first called for an implementation pointer in the ADR; on
checking, its "Implementation Notes" is a forward-looking section written at decision time, and no
ticket in the FRE-1280…1284 chain has amended it. Adding one here would invent a convention
mid-chain, in a file the `adrs` seat owns.

## Acceptance criteria

| AC | Assertion | Test |
| -- | -- | -- |
| **AC-1** | Rate crossing below `demote_below` demotes on the next selection; recovery above `promote_at` promotes only once ≥ `min_samples` fresh observations exist **and** cooldown has elapsed | `test_demotes_on_first_reading_below_band`, `test_promotion_blocked_until_cooldown_elapses`, `test_promotion_requires_measured_window` |
| **AC-1b** | **`LIGHT → stale → HEAVY → measured above band` does not promote without cooldown** — the path codex finding 1 identified, which every AC-1 test above passes over | `test_staleness_demotion_stamps_cooldown`, `test_stale_demoted_model_cannot_promote_immediately` |
| **AC-2** | Selection input carries no model identity; two keys with identical histories select identically | `test_selection_signature_has_no_identity`, `test_renaming_the_model_changes_nothing` |
| **AC-3** | A model with no observations is heavy | `test_unmeasured_is_heavy`, and each `UnmeasuredReason` |
| **AC-4** | Over N heavy turns the configured fraction is applied light with `standing == HEAVY`; those turns are still verified and a bad citation on one still blocks | `test_probation_fraction_over_many_turns`, `test_probation_turn_is_verified_and_blocks` |
| **AC-4b** | **A probation turn carries no heavy gate and no directive, and enters the compliance denominator** (`retrieval_forced is False`, observation recorded) — the property that makes probation break the bootstrap deadlock rather than merely look like it does | `test_probation_turn_has_no_gate_or_directive`, `test_probation_turn_is_an_unconfounded_observation` |
| **AC-5** | A model compliant only when pre-forced never promotes and never oscillates over several windows | `test_pre_forced_only_model_settles_on_heavy` |
| **AC-6** | The same seeded bad citation is blocked under both levels; the verification call is level-invariant | `test_verification_identical_at_both_levels` |
| **AC-6b** | **Heavy actually gates retrieval before generation** — `tool_choice == "required"` on the first heavy call, absent on light — and the gate's unavailability is logged rather than silent | `test_heavy_pins_tool_choice_required`, `test_light_leaves_tool_choice_unset`, `test_gate_unavailable_is_logged` |
| **Durability** | A transition is persisted before the turn proceeds; a stale concurrent write cannot clobber a newer transition; a write failure fails safe to heavy | `test_transition_is_awaited`, `test_stale_write_does_not_clobber`, `test_state_write_failure_falls_back_to_heavy` |

## Risk

Diff class: **escalate** — schema change (migration 0030) and production turn-path code. Mitigated
by `grounding_verification_mode` defaulting to `off`: nothing in this diff executes in prod until
the mode is turned up.
