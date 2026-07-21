# ADR-0122: Per-build artifact builder selection — choose the model before the plan is written

**Status:** Accepted — 2026-07-19 (owner); **amended 2026-07-21 — the card fires at turn start, not at the build boundary** (owner-directed after the first live AC-7 run; see Status Updates). Implementation chain FRE-878 (T1–T3) shipped and deployed; the amendment adds T4–T6. Seam AC-7 is **not yet met** — it failed live on 2026-07-21.
**Date:** 2026-07-19
**Deciders:** Owner (architect), cc-adrs (Opus)
**Tags:** artifact-pipeline, model-selection, human-in-the-loop, config, observability

---

## Context

**What is the issue we're addressing?**

ADR-0121 establishes the model catalog and makes `primary` a standing, user-selected role. The
artifact builder is different in kind, and the difference is the reason this is a separate decision.

**Artifacts are not one thing.** A dense interactive dashboard, a long prose document, a diagram, a
data table, and a single-file web app are different generation problems. A model excellent at one may
be mediocre at another, so a standing "my artifact builder is X" setting cannot express "use the model
that is good at *this*." The choice belongs **per build**, not on a settings page.

That inverts the assumption ADR-0119 made. ADR-0119 kept a config-page picker for
`artifact_builder` and deferred the card (ADR-0118 T3/T4) to a "fast-follow." The owner's position is
the reverse: **per-build selection is the point, and the configured default is the fallback that may
in practice never be changed.** So the card is the primary affordance, not a later enhancement.

**Per-build is not the same as at-the-build-boundary — and this ADR's first draft conflated them.**
It argued the card must fire at the `artifact_draft` call because "which kind you are building is only
known at build time." That does not survive contact with the sequence. The discriminating fact —
*what am I building* — arrives **with the user's request**, at the start of the turn. What the
intervening work adds is the *plan* (sections, data, styling), which refines size and shape but never
changes the kind. The build boundary is the **latest** moment at which the question can be asked, not
the earliest informed one. Everything the card needs is present the instant the user hits send.

**The builder already has an identity — what it lacks is choice.** Both superseded ADRs stated that
ADR-0118 T1 was unbuilt. That is **false as of 2026-07-17**: FRE-879 shipped completely, and this
ADR is written against the code, not against those documents. Already live:

- `ModelRole.ARTIFACT_BUILDER` exists (`llm_client/types.py:29`).
- `artifact_draft` resolves `get_llm_client(role_name="artifact_builder")`
  (`tools/artifact_tools.py:1454-1455`) — it no longer borrows `sub_agent`.
- Telemetry identity is switched on both axes: `respond(role=ModelRole.ARTIFACT_BUILDER)`
  (`artifact_tools.py:1486-1489`) and the `model_role` field on `artifact_draft_sub_agent_start`
  (`:1473-1480`).
- The cost lane exists: `_BUDGET_ROLE_BY_FACTORY_NAME["artifact_builder"] = "artifact_builder"`
  (`cost_gate/__init__.py:115-117`), with an owner-confirmed policy and caps in
  `config/governance/budget.yaml:46-49, 57, 62`.
- Both profiles bind it (`config/profiles/cloud.yaml:7` → `claude_haiku`;
  `config/profiles/local.yaml:8` → `sub_agent`).

So tier-conflation is dissolved and artifact spend is already isolable. **What does not exist is any
way for the user to choose**, and the binding lives on the `ExecutionProfile` — which ADR-0121
deletes along with Path, so the binding must move to an ADR-0121 Layer 3 role binding regardless of
this ADR.

`config/profiles/local.yaml:8` also illustrates the namespace defect ADR-0121 fixes: the local
artifact builder is bound to `sub_agent` — *a slot-alias, not a model* — so "which model builds
locally" cannot be answered without dereferencing a role name through a second file.

Large-output capability is a *measured* failure axis here, not hypothetical: the builder has hit a
provider output cap mid-generation before (FRE-478 — exactly 16,384 output tokens, forcing a slow
continuation call). Which model builds has observed consequences, which is why the card must show
it.

**The first live AC-7 run (2026-07-21) proved the cost of asking late.** T1–T3 shipped and deployed;
master measured the turn end to end:

| Time | Event |
|---|---|
| 07:01:47 | request arrives; socket connected; **user present and attentive** |
| — | `perplexity_query` runs (55 s) |
| — | primary composes the artifact plan (43 s LLM step, no output streamed) |
| 07:03:33 | **socket drops** — phone backgrounded during the silent wait |
| 07:03:44 | build boundary reached, card raised — **117 s after the request** |
| 07:04:25 | socket reconnects; the card is replayed for a decision already taken |

Two distinct faults, and separating them matters:

- **The pause fired into a gap.** The constraint waiter special-cases "no connection registered *at
  this instant*" by returning `connection_lost` immediately, bypassing its own timeout — so a
  momentary mobile drop was treated as permanent and the default applied silently. That is
  **FRE-928**, a defect in the reused ADR-0076 mechanism. It is **independent of this amendment and
  is not fixed by it** (§6).
- **The question was asked 117 s after it could have been answered.** The card determination itself
  takes **under a millisecond** — the options are the availability-filtered catalog deployments and
  depend on nothing the turn computes. All 117 s went to work the answer was never an input to. And
  the user who was present at 07:01:47 had put the phone down by 07:03:44. **This** is what the
  amendment addresses.

**The ordering is also backwards on the merits, not merely the ergonomics — and it is now shipping a
live defect.** The plan is composed with no knowledge of which model will render it, then handed to
that model. Worse, the *output budget* is a global constant:

`_draft_max_tokens()` (`tools/artifact_tools.py:807-817`) returns
`settings.artifact_draft_max_tokens` — **32768**, static — regardless of which model the card
selected. Against the live catalog:

| Deployment | Declared `max_tokens` | Asked for | |
|---|---|---|---|
| `claude_sonnet` (the configured default) | 32768 | 32768 | matches, by coincidence |
| `claude_haiku` | **4096** | 32768 | **8× over its declared cap** |
| `gpt-5.4-mini` | **8192** | 32768 | **4× over** |

So the moment a user exercises the feature that just shipped and picks anything other than the
default, the build requests an output budget the model does not declare. The same constant also gates
the truncation warning (`artifact_tools.py:1604`), so on a selected-Haiku build that warning cannot
fire at the model's real ceiling. FRE-478 — the incident that motivated large-output metadata in the
first place — was "fixed" by raising this global from 16384 to 32768, which encodes a **per-model
fact as a global constant**. Knowing the deployment before planning is what lets that be corrected at
the root.

**What needs to be decided:** when the user is asked; what triggers the ask; what happens when the
prediction is wrong in either direction; how a stored preference interacts; and whether the chosen
deployment's limits become explicit inputs to planning.

---

## Decision

Give the artifact builder a **first-class role identity** and surface its selection **at build time
through the existing ADR-0076 DecisionCard**, over the ADR-0121 catalog. Five parts.

### 1. Preserve the shipped identity; move the binding onto the ADR-0121 catalog

The role, cost lane, telemetry identity, and budget policy are **already built (FRE-879) and are not
rebuilt here.** The only change to that layer is where the *binding* lives: it moves off the
`ExecutionProfile` field (`artifact_builder_model`, deleted with Path) onto an ADR-0121 Layer 3 role
binding with `open: true` and a configured default. This resolves the slot-alias reference on the
local profile at the same time.

**The two-identities rule is retained as a standing invariant, not as new work.** `budget_role`
sets only the *cost lane*; the *telemetry* `role` on `MODEL_CALL_COMPLETED` comes from the
`respond(role=…)` argument, and a `model_role` field appears on the
`artifact_draft_sub_agent_start` log. All three are correct today. They are called out because the
ADR-0121 migration and the selection wiring both touch this call site, and regressing either axis
would make AC-1 pass while the build silently reported the wrong role. AC-2 is therefore a
**regression guard**, not a new capability.

One correctness note for the wiring in §7: `artifact_draft` today calls
`get_llm_client(role_name="artifact_builder")`, which is correct for a caller passing a *role name*.
Once a caller holds an already-resolved *key* from a card decision, it must switch to
`get_llm_client_for_key(key, budget_role="artifact_builder")` — passing a resolved key to
`get_llm_client` would mis-bill it (FRE-869).

### 2. The card fires at **turn start**, before the first LLM call (amended 2026-07-21)

**Owner decision.** The ask is raised in `step_init` (`src/personal_agent/orchestrator/executor.py:2567`, "Initialize:
determine intent and next action") — **before the turn's first LLM call and before any tool runs** —
not at the `artifact_draft` boundary. On the measured run that moves the card from 07:03:44 to
~07:01:47: the user is asked while they are still holding the phone they just typed on.

This is sound only because **the determination has no dependency on the turn's work.** The option set
is the availability-filtered catalog (§3) and the trigger is a deterministic regex over the request
text (§3a) — both computable in under a millisecond from `GatewayOutput`, which `step_init` already
holds (`ctx.gateway_output`, `executor.py:2735-2736`). Nothing the 117 seconds produced was ever an input
to the question. Moving it earlier removes latency without removing information.

Two consequences follow, and they are the amendment's real content: the trigger must now be a
*prediction* rather than an observation (§3a, §3b), and the answer — being available before planning
— becomes an *input* to planning rather than only to the build call (§5).

The pause machinery itself is unchanged: the same
`_maybe_pause_for_constraint` (`orchestrator/executor.py:554`) that powers tool-approval and the
ADR-0101 §8b attachment card. Everything the UX needs exists already:

| UX requirement | Existing ADR-0076 mechanism, reused as-is |
|---|---|
| Card at build time; the turn suspends until you choose | `ConstraintPauseEvent` → PWA `DecisionCard` → executor blocks on the waiter until `CONSTRAINT_DECISION` posts back (durably persisted suspend/resume) |
| A configured default that skips the card | `user_constraint_preferences` (`service/models.py:418`) + `_load_constraint_preference` pre-resolves silently (`constraint_preference_applied`, `executor.py:518-526`) |
| "Ask me every build" | the reserved preference value `always_pause` (`executor.py:518`) |
| "Remember this choice" | the in-card `remember` flag → `_save_constraint_preference` (`executor.py:614-617`) |
| Safe fallback on timeout / disconnect / headless | the last option auto-applies (`executor.py:554-600`); here that is the configured default — never a zero-artifact stall |

### 3a. The trigger: an artifact-build signal the gateway already computes but discards

**The regex already exists and is already in production.** `_ARTIFACT_BUILD_REGEX`
(`src/personal_agent/request_gateway/intent.py:80-87`, FRE-469) matches build/make/create/generate
against a vocabulary
of artifact nouns — guide, dashboard, chart, diagram, page, report, presentation, app, html, svg.
It has been live for months.

**Why the live run saw only a generic signal:** the regex is *unioned into* `_TOOL_INTENT_PATTERNS`
(`intent.py:119-120`, `r"|" + _ARTIFACT_BUILD_REGEX`), so a match appends the undifferentiated
`tool_intent_pattern` signal (`intent.py:309-311`) and the artifact-specific information is destroyed at
the moment it is computed. The classifier *does* know; the result shape simply does not say so.

**The change is therefore surgical, not a new classifier:** evaluate `_ARTIFACT_BUILD_REGEX`
separately and append a distinct `artifact_build_intent` signal to `IntentResult.signals`. The union
stays as-is so the tool-iteration budget (25 vs 6) is untouched — this is purely additive.
`IntentResult` already reaches the executor inside `GatewayOutput.intent`
(`src/personal_agent/request_gateway/types.py:166-183`), so no plumbing is added: `step_init` reads a
field it already has.

**Deliberately not an LLM call.** Asking a model "will this turn build an artifact?" would add a
hot-path inference to save a hot-path inference. The regex is deterministic, sub-millisecond, already
tuned, and — critically — **wrong in ways we can measure** (§3b).

### 3b. Being wrong in both directions, and the asymmetry between them

A prediction replaces an observation, so both error modes must have defined, non-destructive
behaviour. They are **not** symmetric, and the design treats them differently on purpose.

**False positive — the card asks, no build follows.** The selection is **turn-scoped** and simply
goes unused. Nothing is persisted, no state is corrupted, and the next turn starts clean. The cost is
one interaction the user did not need, paid at the cheapest possible moment (before any work), and
**bounded**: the first "remember this" answer suppresses the ask permanently (§4). The failure is
mildly annoying and self-limiting.

**False negative — a build happens that was not predicted.** The build **must not** be blocked or
degraded. `artifact_draft` reached with no turn-scoped selection resolves the role's configured
default — precisely today's pre-card behaviour — and emits a distinct
`artifact_build_intent_missed` log carrying the request text's shape.

**A false negative does not re-raise the card late.** That is a deliberate choice, and the reasoning
matters because the opposite is superficially attractive ("just ask when we get there"):
1. A late card is the exact behaviour this amendment exists to remove; reintroducing it as a fallback
   reintroduces the 117-second problem for the very requests the classifier understands least.
2. By the build boundary the plan is already composed blind, so the answer has lost its main value
   (§5) even if the user gives it.
3. The missed-prediction log is a **measurement**, and it is the only honest way to tune the regex —
   each miss names a phrasing the vocabulary does not cover. The alternative silently papers over the
   gap and the regex never improves.

So the system degrades to exactly what shipped before the card, and tells us why. **AC-11 asserts
that the miss is logged, not merely that the build survives** — a fallback that works but is silent
would leave the regex permanently untuned.

### 3c. What is genuinely new: a catalog-backed decision type

The reused machinery assumes a **closed, static** constraint set. Admitting a decision whose options
are *computed* requires a coordinated widening of four contracts. None is a new mechanism, and this
is stated plainly rather than hidden behind a "one-line change" claim:

- `orchestrator/constraint_options.py:62` — options are indexed out of a static `CONSTRAINT_OPTIONS`
  dict. `artifact_builder`'s options are computed from the ADR-0121 catalog and
  availability-filtered by provider health at pause time.
- `orchestrator/executor.py:554` — `_maybe_pause_for_constraint` requires `constraint` to be a key
  of `CONSTRAINT_OPTIONS`; it must accept the computed-options path.
- `transport/events.py:136-143` — `ConstraintPauseEvent.constraint` is a closed
  `Literal["tool_iteration_limit", "context_compression"]` and must admit `artifact_builder`.
  **Pre-existing drift closed in the same pass:** `attachment_cost` is defined in
  `constraint_options.py:41-46` and passed at `executor.py:1961-1966`, yet is absent from the
  Literal — both sides of that drift are cited so the fix is verifiable.
- `service/app.py:1501-1504` — settings validation checks `preferred_action ∈ {always_pause} ∪
  option_ids(constraint)`. For `artifact_builder` the valid actions are catalog keys, so validation
  must consult the catalog. This is an API contract change, not "no change."

Because options now come from the ADR-0121 catalog rather than a bespoke registry, the card gets
`summary`, cost, context window, and large-output capability for free — the user chooses on visible
detail rather than on a bare key.

### 3d. Turn start is already occupied — ordering, and the ambiguous-affirmative hazard

`step_init` is not empty when we arrive. It already raises a pause of its own: the
`attachment_cost` gate (`executor.py:2730`, via `_maybe_confirm_attachment_cost` at `:1741`) fires
for a turn carrying priced attachments, immediately **before** the gateway block this amendment reads
(`:2736`). So a single turn can now have two turn-start decisions. That is rare — an over-threshold
attachment *and* an artifact request — but it is reachable, and an implementer must not discover it at
runtime.

**Order: attachment cost first, builder second.** This is the existing code order and it is also the
correct one on the merits: declining the attachment cost short-circuits the turn
(`return TaskState.SYNTHESIS`, `:2733`), so no build follows and a builder question asked first would
have been wasted. Asking in the other order can only ever cost the user a pointless interaction.

**The ambiguous-affirmative hazard is pre-existing, documented, and must not be widened.**
`executor.py:2466-2477` records a code-review finding from FRE-749: two pending confirmations in one
turn "both key off the same generic yes/affirmative detector," so a single ambiguous "yes" could
resolve both pending states and let a priced attachment skip its gate. The fix there was to reset
`ctx.attachment_cost_confirmed` whenever content is re-injected.

The builder decision is **structurally less exposed** — its options are deployment keys, not
proceed/decline, so a bare affirmative names no option — but "less exposed" is not "immune", and this
amendment is adding a third pause to a turn that already had this bug once. **The builder decision
must be resolvable only by an explicit option id, never by the generic affirmative detector, and
must never be treated as satisfied by another pause's answer.** AC-14 asserts this directly rather
than trusting the structural argument.

**One card per turn, one selection for every build in it.** A turn that builds more than one artifact
(the tool loop permits up to 25 iterations on `tool_use`) uses the single turn-start selection for
all of them and does **not** re-ask per build. The user chose "the builder for this turn"; re-asking
mid-turn would reintroduce exactly the late-card behaviour being removed.

**The routing fork gets no worse, and arguably better.** §8's known limitation — a build routed to the
ADR-0086 expansion path never reaches `artifact_draft` and so never saw a card — is unchanged in
substance, but the turn-start placement is *agnostic* to which fork the turn later takes: the card
fires on intent, before routing is decided. On an expansion-path turn the selection simply goes unused
(a false positive by §3b, harmless and discarded). The expansion path still does not consume a builder
selection; that remains out of scope and tracked separately.

### 4. A stored preference suppresses the ask entirely — and still informs planning

Owner-directed: **a remembered choice means the user is never asked, in either placement.** The
existing pre-resolution (`_load_constraint_preference` → `constraint_preference_applied`,
`executor.py:518-526`) is consulted at **turn start**, before the card would be raised. Three
outcomes, all resolved in the same sub-millisecond step:

| Stored preference | Behaviour |
|---|---|
| a deployment key | resolves silently to that deployment; **no card**; the deployment is known before planning |
| `always_pause` | the card is raised regardless |
| none | the card is raised |

**The silent path must carry the resolved deployment forward, not merely suppress the card.** This is
the easy thing to get wrong: an implementation that pre-resolves the preference only to skip the pause,
without threading the deployment into planning (§5), would look correct — no card, right model at the
build — while still sizing the plan against a global constant. The sizing benefit belongs to *both*
paths. AC-13 asserts it on the preference path specifically, because that is the path where the card's
absence makes the omission invisible.

**Carrier.** The resolution is **turn-scoped state on the executor context**, alongside the existing
per-turn fields, not session-scoped selection state. An artifact-builder pick is a property of *this
build*, not a standing choice — standing choices are exactly what the preference store is for. This
also gives the false-positive path its harmlessness (§3b): turn-scoped state is discarded with the
turn.

### 5. The chosen deployment's limits become inputs to planning, not just to the build call

Owner-raised, and the live catalog shows it is a defect rather than an enhancement (see Context).
Once the deployment is known before planning, two things change:

**The output budget stops being a global constant.** `_draft_max_tokens()` becomes a function of the
resolved deployment — its declared `max_tokens`, floored by the configured ceiling rather than
replaced by it:

```
effective_budget = min(deployment.max_tokens, settings.artifact_draft_max_tokens)
```

This fixes the shipped mismatch (Haiku 4096 vs 32768 requested) in both directions: never request
more than a model declares, never exceed the operator's ceiling. The truncation warning
(`artifact_tools.py:1604`) compares against the same effective value, so it fires at the model's real
ceiling instead of a constant it may never reach.

**The plan is sized to the builder.** The planning step is told the effective output budget and the
deployment's context window, so a plan targeting a 4096-token builder is scoped differently from one
targeting 32768. This is the reordering's substantive payoff and the root-cause fix for the FRE-478
class: rather than discovering the ceiling by hitting it mid-generation and continuing in a second
call, the plan is written toward a ceiling that is known in advance.

**Honest scope limit:** this makes the plan *aware* of the budget; it does not guarantee the model
respects it. A plan can still overrun, and the FRE-471 truncate-with-warning guard remains the
backstop. AC-12 therefore asserts the budget is correctly derived and applied — an outcome we control
— rather than asserting artifacts never truncate, which we do not.

### 6. Relationship to FRE-928 — reduced exposure, not a fix

The amendment **narrows the window** in which a mobile drop can swallow the pause: at turn start the
user has just interacted, so the socket is overwhelmingly likely to be live. It does **not** repair
the underlying defect. The waiter still treats a momentary absence as permanent, bypassing its own
timeout, and the measured session dropped every 30–140 seconds — a drop can land on any instant,
including the first.

**FRE-928 must therefore ship on its own merits and must not be held for this amendment.** It is
also the precondition for AC-5's timeout leg, which this amendment leaves unchanged. The two are
complementary: this amendment makes the pause land when the user is present; FRE-928 makes it survive
their absence.

### 7. `artifact_draft` is wired to the resolved builder, fail-closed

When a card decision supplies a key, `artifact_draft` switches from its current role-name call
(`get_llm_client(role_name="artifact_builder")`, `artifact_tools.py:1454-1455`) to
`get_llm_client_for_key(builder_key, budget_role="artifact_builder")` (`factory.py:125` — the
correct call for a caller holding an already-resolved key; `get_llm_client` would mis-bill it,
FRE-869). With no decision — headless, no socket, timeout — it keeps the role-name path and the
role's configured default. **Both paths must bill the same lane**, which is why `budget_role` is
passed explicitly on the key path.

Before use, the key is checked against the catalog: it must exist, be `kind: llm`, be permitted for
an `open` role, and have an available provider. Anything else **fails closed to the configured
default** — never to an arbitrary model, never to no model.

### 8. Scope

**In scope:** the build-time card, the role identity, the cost lane, the telemetry identity, and the
`artifact_draft` wiring.

**Not in scope (unchanged by the amendment):** a standing config-page picker for `artifact_builder`. The ADR-0121 observe view
shows the configured default; changing it is a config edit. If usage shows the default is being
changed often, a picker is a trivial addition to the ADR-0121 config surface — but building it now
would be speculative, and the owner's expectation is that the default rarely moves.

**Known limitation, carried forward from ADR-0118 and not solved here:** the card fires only on the
`artifact_draft` path. A "build X" request routed to the expansion/HYBRID path (ADR-0086, gated by
`artifact_decomposition_enabled`, `config/settings.py:491-497`) reaches no builder and therefore no
card. In practice `artifact_draft` dominates (expansion fires roughly once per 90 days; the
deterministic fallback dominates), so the gap is narrow — but it is real, it is a dependency on the
routing fork, and it is tracked separately rather than quietly ignored.

---

## Alternatives Considered

### Option 0: Keep the card at the build boundary (what T1–T3 shipped; rejected 2026-07-21)
**Description:** Raise the card at the `artifact_draft` call, as originally specified — the model is
chosen at the last possible moment, after the plan exists.
**Pros:**
- **The build is certain.** No prediction, no false positives, no false negatives — the card fires if
  and only if a build actually happens.
- The plan is available, so in principle the card could show "this looks like a large artifact."
- It is what is built and deployed today; keeping it costs nothing.
**Cons:**
- **The user is usually gone.** Measured at 117 s after the request, dominated by a 55 s tool call and
  a 43 s silent LLM step. The owner had backgrounded the phone 11 s before the card appeared.
- **The answer arrives too late to be useful upstream.** The plan is already written, so the
  builder's real output ceiling cannot inform it — leaving the FRE-478 class unaddressed at the root
  and the shipped 4096-vs-32768 mismatch unfixable in the right place.
- **It maximises exposure to FRE-928.** The longer the gap between request and pause, the likelier a
  mobile client has dropped — exactly what happened.
- The certainty it buys is worth less than it appears: a false negative degrades to the pre-card
  default, which is the same outcome this option produces when the user does not answer.
**Why Rejected:** owner-directed after the live run. The certainty of asking late is real but cheap;
the cost — asking a user who has left, and sizing the plan blind — is the whole value of the feature.
§3b makes the prediction's failure modes non-destructive, which is what makes trading certainty for
timeliness safe.

### Option 0b: Ask early, but re-raise the card at the build boundary on a missed prediction
**Description:** Turn-start card as decided, plus a late card as a safety net when the regex missed.
**Pros:**
- The user can still choose on requests the classifier does not recognise.
- No silent degradation to the default.
**Cons:**
- Reintroduces the 117-second late card precisely for the requests we understand *least* — the worst
  population to inflict it on.
- By then the plan is composed blind, so the choice has lost its main benefit even when answered.
- Removes the pressure to fix the trigger: a silent safety net means the regex is never tuned, and the
  miss log becomes decoration rather than a work signal.
**Why Rejected:** it optimises the rare case by degrading the common one and eliminating the feedback
that would make the rare case rarer. Degrade to the default and **log the miss** (§3b, AC-11).

### Option 1: A standing config-page picker only (the superseded ADR-0119 approach)
**Description:** Expose `artifact_builder` as a role picker on the ADR-0121 config surface; no
build-time card.
**Pros:**
- Zero new mechanism — it is one more panel on a surface being built anyway.
- No widening of the constraint contracts; no pause path in the artifact flow.
- Cheapest possible implementation.
**Cons:**
- Cannot express model-fit per artifact *type*, which is the actual requirement — you would have to
  visit a settings page and change a global before each build, then remember to change it back.
- Puts the affordance where the decision is *not* being made; the user is in a conversation asking
  for an artifact, not in a config screen.
**Why Rejected:** it optimizes the mechanism and loses the capability. Build-time is when the
relevant information (what am I building) exists.

### Option 2: Inline natural-language model naming
**Description:** The user names the model in the request — "build me a dashboard on X with Gemini."
The primary parses it, validates against the catalog, and passes it to the builder.
**Pros:**
- No new UI at all; the picker is natural language.
- Frictionless compare-by-using ("now do the same with Haiku").
**Cons:**
- **Discoverability fails** — the user cannot know the available set without asking, forcing a
  multi-turn detour.
- The guardrail becomes validate-and-reject after the fact rather than invalid-states-
  unrepresentable.
- No clean home for a default or a "remember this" affordance.
**Why Rejected:** the discoverability gap and the multi-turn friction are exactly what a card
removes, and a card enforces the guardrail by construction. Retained on record as a possible
*additional* affordance later, not as the mechanism.

### Option 3: A bespoke model-selection card with its own preference store
**Description:** Build a new interactive card type, waiter, and preference table dedicated to model
selection.
**Pros:**
- Clean semantic separation between "constraint" and "selection."
**Cons:**
- Duplicates suspend/resume, durable pause persistence, safe-default-on-timeout, and a preference
  store that ADR-0076 already provides and ADR-0101 §8b already uses for a *choice*.
- Two mechanisms doing one job is precisely the orphan/duplication the design constitution forbids.
**Why Rejected:** all net-new cost for zero capability gain over widening ADR-0076's decision
surface. The real delta is a computed-options decision type, which is a coordinated widening of
existing contracts.

### Option 4: Classify build intent with an LLM instead of a regex
**Description:** At turn start, ask a small model whether this request will produce an artifact.
**Pros:**
- Robust to phrasings the regex vocabulary does not cover; would reduce false negatives.
- Could extract the artifact *kind*, enabling kind-aware recommendations later.
**Cons:**
- **Adds a hot-path inference to save a hot-path wait** — latency and cost on every turn, to decide
  whether to ask a sub-millisecond question.
- Non-deterministic: the same request could raise the card on one turn and not the next, which is
  worse than a predictable blind spot.
- The regex already exists, is tuned (FRE-469), and is in production for tool-iteration budgeting.
**Why Rejected:** wrong cost/benefit at this altitude. The regex's misses are measurable (§3b) and
each one is a concrete vocabulary fix. Revisit only if the miss log shows a class of phrasing that
pattern-matching genuinely cannot reach.

### Option 5: Let the primary model choose the builder
**Description:** The orchestrator picks the artifact builder per request, as the future sub-agent ADR proposes for
sub-agents.
**Pros:**
- No user interruption; automatic.
- Consistent with the sub-agent direction.
**Cons:**
- The user is the one who knows what they want from *this* artifact — density, length, interactivity,
  fidelity to a style — much of which is not in the request text.
- Removes the compare-by-using loop that is the pedagogic point of exposing the choice.
- Would consume the model-performance history that deliberately does not exist in config (ADR-0121
  §2 declared-vs-observed line).
**Why Rejected:** wrong authority for this role. The builder is a role the owner explicitly wants to
feel and compare by hand; sub-agent dispatch is a role they explicitly do not. *(Note: this option is
independently blocked by the turn-start decision — the primary has not run yet when the card fires.)*

---

## Consequences

### Positive Consequences

- **Model fit can follow artifact type** — the capability this ADR exists for.
- **The user is asked while they are still there.** The ask moves from ~117 s after the request to
  ~0 s, which on the measured run is the difference between a present user and a backgrounded phone.
- **The plan can be sized to the builder**, fixing the shipped global-constant mismatch and
  addressing the FRE-478 class at its root rather than by raising a constant.
- **Exposure to FRE-928 shrinks** — the pause lands moments after an interaction rather than two
  minutes into a silent wait.
- **The gateway's existing artifact signal stops being discarded**, at the cost of one added signal
  string and no new classifier.
- **Artifact spend and performance become isolable for the first time**, via a distinct cost lane
  and telemetry role.
- **The `sub_agent` tier-conflation is dissolved** — the builder stops being hostage to an unrelated
  role's binding.
- **Mechanism reuse is real**: no new pause/resume, no new card framework, no new preference store.
  The net-new surface is a computed-options decision type threaded through four enumerated seams.
- **The card shows real detail** — cost, context, capability, summary — because it reads the
  ADR-0121 catalog rather than a bare key list.
- **The pattern generalizes**: any future role wanting per-invocation selection reuses this decision
  type unchanged.

### Negative Consequences

- **A previously-closed enum surface is opened.** Four static contracts widen at once (options
  source, executor guard, event Literal, settings validation). None is a new mechanism, but this is
  a coordinated multi-file change, not a one-liner.
- **An interaction is added to a flow that previously had none.** Mitigated by the configured
  default pre-resolving silently, but a user who sets `always_pause` accepts a pause per build.
- **The trigger is now a prediction, so it can be wrong in both directions.** The certainty of the
  build-boundary placement is genuinely given up; §3b makes both failure modes non-destructive, but
  they are new.
- **The card can interrupt before any output has streamed.** At the build boundary the user had seen
  the turn working; at turn start they see a question first. Intended — they are present — but a real
  change in feel.
- **A second placement exists in the code path**: the turn-start ask plus the build-boundary
  fail-closed check (§7). The latter no longer raises a card, so this is one mechanism with a
  resolution fallback, not two competing surfaces — but it must be read that way to stay legible.
- **The routing-fork gap persists** (§5): expansion-path builds get no card.
- **The card is another turn-suspending surface**, so its timeout and disconnect behaviour must be
  right or a build stalls — covered by AC-5.

### Risks and Mitigations

| Risk | Severity | Mitigation |
|---|---|---|
| A selected model cannot emit a large artifact and truncates mid-document | Medium | The card surfaces `max_output_tokens` from the catalog; the FRE-471 truncate-with-warning guard already prevents a zero-artifact outcome; FRE-478 is the precedent this is drawn from |
| The card is wired but the call still runs on the old model (label-changed, call-didn't) | **High** | AC-1 asserts the emitted `MODEL_CALL_COMPLETED.model`, not the configuration; the two-identities rule (§1) is asserted separately by AC-2 |
| A stale or hallucinated key reaches the client factory | Medium | Fail-closed catalog check in `artifact_draft` (§4); falls back to the configured default; AC-4 |
| No answer stalls the build | Medium | Timeout/disconnect auto-applies the configured default; AC-5 asserts an artifact is still produced |
| Widening the constraint contracts introduces drift (settings API accepts an unsupported action) | Medium | Validation consults the catalog (§3c); AC-6 asserts a non-catalog action is rejected 422; the `attachment_cost` Literal drift is closed in the same pass |
| The regex misses a build phrasing, so the user is never asked | Medium | Degrades to the configured default (pre-card behaviour) and emits `artifact_build_intent_missed`; AC-11 asserts the **log**, making each miss a concrete vocabulary fix rather than a silent gap |
| The card fires on turns that never build | Low | Turn-scoped selection, discarded unused; no persisted state; bounded by the remember-preference path; AC-10 asserts no leakage into the next turn |
| The preference path suppresses the card but never threads the deployment into planning | **High** | Invisible failure — no card, correct model at build, plan still sized against a constant; AC-13 asserts the preference path specifically |
| Requesting more output than the model declares (live today: Haiku's 4096 vs 32768 requested) | **High** | `min(deployment.max_tokens, settings ceiling)` (§5); AC-12 asserts the derived budget on a non-default pick in both the under- and over-shoot directions |
| The turn-start ask lands in a momentary mobile disconnect | Medium | Exposure reduced but **not** removed — FRE-928 is the actual fix and ships independently (§6) |
| A single ambiguous "yes" resolves both the attachment gate and the builder pick | **High** | The FRE-749 bug class (`executor.py:2466-2477`), now reachable with a third turn-start pause; ordering fixed and isolation asserted by AC-14 |
| A multi-artifact turn re-asks per build, reintroducing the late card | Low | One selection per turn covers every build in it (§3d); AC-10 asserts a single pause event across two builds |

---

## Implementation Notes

**Already built — do NOT re-implement (FRE-879):** `ModelRole.ARTIFACT_BUILDER`
(`llm_client/types.py:29`); the `artifact_builder` resolution in `artifact_tools.py:1454-1455`; the
telemetry identity on both axes (`:1473-1480`, `:1486-1489`); the cost lane
(`cost_gate/__init__.py:115-117`); the budget policy and owner-confirmed caps
(`config/governance/budget.yaml:46-49, 57, 62` — to be reconciled with ADR-0120, which abolishes
hard caps in favour of visibility and consent).

**Files affected by the amendment (steps 4–6):**
- `src/personal_agent/request_gateway/intent.py:80-87, 119-120, 309-311` — emit a distinct
  `artifact_build_intent` signal; leave the `_TOOL_INTENT_PATTERNS` union in place.
- `src/personal_agent/orchestrator/executor.py:2567` (`step_init`) — raise the decision here off
  `ctx.gateway_output.intent.signals`, consult the preference, store the resolution turn-scoped;
  sequenced **after** the existing `attachment_cost` gate at `:2730` and before/alongside the gateway
  block at `:2736`. Keep the two decisions isolated per the FRE-749 finding (`:2466-2477`).
- `src/personal_agent/tools/artifact_tools.py:807-817` (`_draft_max_tokens`) — derive from the
  resolved deployment, floored by `settings.artifact_draft_max_tokens`; `:1604` — warn against the
  same effective value; the planning prompt receives the effective budget and context window.
- `src/personal_agent/orchestrator/constraint_options.py` — unchanged; the option computation is
  already independent of turn state, which is what makes the move safe.

**Files affected (original steps 1–3, shipped):**
- `config/model_roles.yaml` — `artifact_builder` binding, `open: true` (live:
  `{ deployment: claude_sonnet, open: true }`). Delivered by the ADR-0121 migration.
- `src/personal_agent/orchestrator/constraint_options.py` — catalog-backed, availability-filtered
  options provider alongside the static `CONSTRAINT_OPTIONS`.
- `src/personal_agent/orchestrator/executor.py` — accept the computed-options path; add the
  `artifact_builder` decision at the artifact-build boundary.
- `src/personal_agent/transport/events.py` — widen `ConstraintPauseEvent.constraint`; close the
  `attachment_cost` drift.
- `src/personal_agent/service/app.py` — settings validation consults the catalog for
  `artifact_builder` actions.
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — resolved key →
  `get_llm_client_for_key(..., budget_role="artifact_builder")` + fail-closed check; **and**
  `:1468-1470` — switch `respond(role=…)` and the `model_role` log field to `ARTIFACT_BUILDER`.
- PWA — `DecisionCard` renders the builder options with cost/context/summary (no new card type).

**Dependencies:** **ADR-0121** (catalog, role bindings, provider availability — this ADR cannot start
before its step 1–2 land), ADR-0076 (DecisionCard, preferences), ADR-0077 (`artifact_draft`),
ADR-0101 §8b / FRE-691 (selection-card precedent), ADR-0120 (cost-lane policy reconciliation),
FRE-869 (budget-lane correctness), FRE-471 (truncate-with-warning guard), FRE-478/495 (output-cap
precedent).

**Testing strategy:** unit tests for computed-option generation and availability filtering, the
fail-closed catalog check, `budget_role_for("artifact_builder")` returning a distinct lane, settings
rejection of a non-catalog action, preference pre-resolution versus `always_pause`, and
safe-default-on-timeout. A live build on the deployed stack with a non-default pick.

**Sequencing.** Steps 1–3 **shipped and deployed** (FRE-881, FRE-882, FRE-921). The amendment adds
steps 4–6, which move the placement and correct the sizing. AC-7 is **not** met — it failed live on
2026-07-21 — and remains the closing seam.

1. ~~Computed-options decision type~~ — shipped (FRE-881).
2. ~~Card at the build boundary + fail-closed catalog check + preference behaviour~~ — shipped
   (FRE-882).
3. ~~PWA card rendering with catalog detail~~ — shipped (FRE-921).
4. **Artifact-build intent signal** — evaluate `_ARTIFACT_BUILD_REGEX` separately and emit a distinct
   `artifact_build_intent` signal on `IntentResult.signals`; the existing union into
   `_TOOL_INTENT_PATTERNS` is left untouched so the tool-iteration budget does not move. Purely
   additive. No behaviour change until step 5 consumes it.
5. **Move the ask to turn start** — raise the decision in `step_init` off that signal, ahead of the
   first LLM call; consult the stored preference there; carry the resolution as turn-scoped state;
   remove the build-boundary *card* while keeping the fail-closed resolution check; emit
   `artifact_build_intent_missed` when a build is reached with no turn-scoped resolution; order the
   ask **after** the existing `attachment_cost` gate and keep the two decisions isolated (§3d).
   AC-10, AC-11, AC-14.
6. **Size the plan and the call to the chosen deployment** — `_draft_max_tokens()` becomes
   `min(deployment.max_tokens, settings.artifact_draft_max_tokens)`, the truncation-warning threshold
   follows it, and the planning step is given the effective budget and context window.
   **(Seam ticket — re-runs AC-7, plus AC-12, AC-13.)**

AC-2 (the shipped identity) is asserted as a **regression guard in every step**, because ADR-0121's
migration and this ADR's wiring both touch that call site. **FRE-928 is a hard prerequisite for the
AC-5 timeout leg** but ships on its own schedule (§6) — it must not be held for this amendment.

---

## Verification / Acceptance Criteria

- **AC-1 — A non-default pick actually runs on that model.** *Check:* build an artifact after
  selecting, in the card, a builder that is **not** the configured default **and not** the model any
  other role resolves to (so a fallback cannot coincidentally produce the expected id); the
  `MODEL_CALL_COMPLETED` telemetry for the `artifact_draft` span shows the **selected** deployment's
  resolved provider/model id — **including when the selection was made at turn start, several tool
  calls before the build**, so the turn-scoped resolution must survive the intervening work.
  *Fails if* the emitted `model` is the default regardless of the selection — the classic
  "label changed, call didn't" — or if the pick is lost across the turn's tool calls.
- **AC-2 (regression guard) — the shipped identity survives.** The role, cost lane, and telemetry
  identity are already live (FRE-879); this asserts the ADR-0121 migration and the card wiring do
  not regress them. *Check:* for a build on the **default** builder and again on a **selected**
  builder, (a) `MODEL_CALL_COMPLETED.role == "artifact_builder"` in both cases, **and** (b) the
  cost-gate reservation debits the `artifact_builder` `budget_counters` row while `main_inference`
  is untouched (`budget_reservations`/`budget_counters`, `docker/postgres/init.sql:245-280`).
  *Fails if* either axis reports `sub_agent`/`main_inference` on **either** path — note the selected
  path is the new risk, since it switches to `get_llm_client_for_key`, where omitting `budget_role`
  silently re-bills to the default lane (FRE-869). *(`api_costs` has no `budget_role` column; the
  lane is asserted in cost_gate.)*
- **AC-3 — Spend is separable per builder model, not just per role.** *Check:* after two builds on
  **two different** selected builders, spend grouped by (role, model) returns non-zero, distinct
  rows for each model, both under the `artifact_builder` lane. *Fails if* per-model attribution
  collapses — the ADR-0121 §8 telemetry migration is what makes this answerable, and this AC is how
  its usefulness is proven at the artifact surface.
- **AC-4 — An invalid builder key fails closed to the default, never onward.** *Check:* drive the
  decision path with (a) a key absent from the catalog, (b) a catalog key with `kind: embedding`,
  and (c) a catalog key whose provider is unavailable. In every case the build completes on the
  **configured default** and the substituted key is logged. *Fails if* any of the three reaches the
  client factory, or the build errors instead of falling back.
- **AC-5 — No answer never means no artifact, *and the decision path genuinely ran*.** The two
  no-answer cases behave differently in the reused mechanism and are therefore asserted differently.
  This split is deliberate: demanding a pause event in the no-socket case would require *changing*
  ADR-0076 rather than reusing it, since `register_constraint_waiter` returns immediately with
  `resolution="connection_lost"` and explicitly does **not** invoke the push callback when no
  connection is active (`transport/agui/ws_endpoint.py:250-259`).
  - **Timeout (socket connected, user does not answer):** a `ConstraintPauseEvent` with
    `constraint="artifact_builder"` is **emitted and persisted**; the resolution is
    `timeout_default`; the artifact renders on the configured default.
  - **No socket (headless / disconnected):** a resolution record for
    `constraint="artifact_builder"` with `resolution="connection_lost"` exists for that turn and the
    artifact renders on the configured default. *(Amended 2026-07-21: the original wording asserted
    **no** pause event is emitted here. FRE-928 establishes that the event **is** persisted for
    replay and that the waiter must wait out its timeout rather than returning instantly, so this leg
    no longer asserts the event's absence — only that a resolution record exists and the build
    completes. FRE-928 owns the timeout-and-replay behaviour itself.)*

  *Fails if* the build stalls or yields zero artifact; if the timeout case produces no persisted
  pause event; **or if the no-socket case produces no `artifact_builder` resolution record at all** —
  that last clause is what keeps this discriminating, because an implementation that never wired the
  decision at all would produce a perfectly good artifact with no `artifact_builder` constraint
  record on either path.
- **AC-6 — The card and the settings API offer exactly the valid, available set.** *Check:*
  `set(ConstraintPauseEvent.options)` **equals** the availability-filtered set of catalog
  deployments where `kind: llm` — asserted in both directions (no non-catalog key leaks in; a
  deployment whose provider is down is absent; an available one is present). Separately, the
  settings API rejects a `preferred_action` naming a non-catalog key with 422. *Fails if* the option
  set differs in either direction, or the API accepts an unsupported action.
- **AC-7 (assembled seam) — the whole loop, live.** *Check:* on the deployed stack a real "build me
  an artifact about X" request surfaces the card showing per-option cost and context; a **non-default**
  pick renders a correct, grounded artifact on the chosen model; AC-1 and AC-2 telemetry corroborate
  both the model and the lane; and with a stored preference set instead, no card appears, yet the
  build still runs on the *preferred* model (the preference is a config key, so the check maps it
  through the catalog to its resolved model id before comparing — comparing the raw key to the
  emitted `model` would be a dimension error). *Fails if* any leg breaks — card → decision →
  resolved key → tool → correct model → correct lane — **or** if a stored preference is logged and
  swallowed while the build silently falls back to the default.

- **AC-10 — The ask is resolved before the turn does any work, and leaves nothing behind when it was
  wrong.** *Check, three parts:*
  **(a) Ordering, asserted at the state machine, not across event streams.** The `artifact_builder`
  decision is resolved **while the executor is still in `TaskState.INIT`** — i.e. `step_init`
  (`src/personal_agent/orchestrator/executor.py:2567`) returns with the resolution already on the
  execution context, before it transitions to `PLANNING`/`LLM_CALL`. Assert this directly in an
  executor test: on return from `step_init`, the turn-scoped builder resolution is populated.
  *(Deliberately **not** asserted as "the pause event precedes `MODEL_CALL_COMPLETED`": the pause is
  an AG-UI session event carrying a Postgres `seq`, while `MODEL_CALL_COMPLETED` is a structlog
  telemetry event on a different path — the two share no monotonic ordering primitive, so that
  comparison could be satisfied by a test that never proves what it claims.)*
  **(b) Live corroboration, within the one stream that is ordered.** On a real build turn, the
  `artifact_builder` `ConstraintPauseEvent`'s session-event `seq` is lower than that of every
  tool-execution event for the same turn.
  **(c) No leakage, one ask per turn.** On a turn where the card was answered but **no**
  `artifact_draft` call followed, the next turn in the same session raises the card again and resolves
  independently. On a turn making **two** `artifact_draft` calls, exactly **one**
  `ConstraintPauseEvent` for `artifact_builder` is emitted and **both** builds run on the selected
  deployment.
  *Fails if* `step_init` returns without the resolution — which is exactly what the currently deployed
  build-boundary placement does, so (a) fails against today's code — if the pause's `seq` exceeds any
  tool event's, if a turn-scoped pick leaks into a subsequent turn, or if a second build re-asks.
- **AC-11 — A missed prediction degrades to the default *and says so*.** *Check:* drive a build
  through a request that does **not** match the artifact-build vocabulary (so no card is raised) but
  which nevertheless reaches `artifact_draft`; the artifact renders on the role's configured default,
  and an `artifact_build_intent_missed` event is emitted for that turn identifying the request.
  *Fails if* the build errors or hangs, **or** if it completes with no missed-prediction event —
  a silent fallback leaves the trigger permanently untunable, which is the whole reason this AC
  asserts the log rather than just the survival.
- **AC-12 — The output budget follows the selected model, in both directions.** *Check, both legs
  required, and both expressible against the live catalog by varying the ceiling rather than
  inventing a deployment:*
  **(a) Model below ceiling — the model wins.** With `settings.artifact_draft_max_tokens = 32768`,
  select `claude_haiku` (declared `max_tokens` **4096**): the generation call requests **4096**, and
  the truncation-warning threshold (`src/personal_agent/tools/artifact_tools.py:1604`) is likewise
  4096.
  **(b) Model above ceiling — the ceiling wins.** With `settings.artifact_draft_max_tokens` set to
  **2048**, select `claude_sonnet` (declared `max_tokens` 32768): the call requests **2048**.
  *(The ceiling is varied rather than the catalog because **no deployment in the live catalog exceeds
  32768** — `claude_sonnet` 32768 is the maximum — so a leg written as "pick a model declaring more
  than the ceiling" would be untestable as stated, and an implementation that special-cased the
  undershoot without ever implementing the `min(...)` clamp would pass unchallenged.)*
  *Fails if* either leg requests `settings.artifact_draft_max_tokens` regardless of the pick — the
  live behaviour today, where selecting Haiku asks for 8× its declared cap — or if only the undershoot
  direction is honoured. *(Asserts the budget is correctly derived and applied, which we control — not
  that artifacts never truncate, which we do not.)*
- **AC-13 — The silent preference path is sized correctly too.** *Check:* with a stored preference
  naming a **non-default** deployment whose `max_tokens` differs from the ceiling, run a build: no
  `ConstraintPauseEvent` is emitted, the build runs on the preferred deployment, **and** the
  generation call's requested `max_tokens` equals that deployment's derived budget (per AC-12), not
  the global constant. *Fails if* the preference is pre-resolved only to suppress the card while the
  budget still comes from the constant — an implementation that passes AC-12 on the card path can
  still fail here, which is exactly why this is asserted separately.

- **AC-14 — Two turn-start decisions coexist without contaminating each other.** *Check:* on a
  single turn carrying **both** an over-threshold priced attachment and an artifact-build request,
  (a) the `attachment_cost` decision is raised **before** the `artifact_builder` decision; (b)
  answering the attachment gate resolves **only** it — the builder decision remains pending and is
  still raised; (c) a bare affirmative ("yes") supplied while the builder decision is pending does
  **not** select any deployment, and the builder decision remains unresolved or falls back to the
  configured default, never to an arbitrary candidate; (d) declining the attachment gate
  short-circuits the turn and no build occurs. *Fails if* one answer resolves both pauses, if the
  builder decision is satisfied by the generic affirmative detector, or if the ordering is reversed.
  *(This is the FRE-749 bug class — `executor.py:2466-2477` documents a single ambiguous "yes"
  resolving two pending states and letting a priced attachment skip its gate. This amendment adds a
  third turn-start pause to that same turn, so the invariant is asserted rather than argued.)*

**Seam owner:** AC-7 is owned by the **PWA card-rendering ticket (step 4)** — the child where the
assembled intent first holds. This ADR does not close when the decision type merges; it closes only
when AC-7 is proven on the deployed stack. Master asserts AC-7 at the acceptance gate.

---

## References

- ADR-0076 — constraint governance / DecisionCard: the suspend/resume, preference, and
  safe-default machinery reused here unchanged
- ADR-0077 — `artifact_draft`, the sole real artifact builder
- ADR-0086 — expansion/HYBRID decomposition (the routing-fork limitation in §5)
- ADR-0089 — artifact execution security (the sealed-box the built artifact runs in)
- ADR-0101 §8b / FRE-691 — cloud-attachment cost DecisionCard, the live precedent for a *choice*
  on this machinery
- ADR-0118 — superseded; its DecisionCard-reuse analysis and the two-identities finding are carried
  forward here
- ADR-0119 — superseded; its config-picker-only approach is Option 1 above, rejected
- ADR-0120 — cost governance (the `artifact_builder` budget policy must reconcile with it)
- ADR-0121 — model catalog and selection layer: the catalog this card reads, and a hard prerequisite
- Orchestrator-invoked sub-agents — **future ADR, not yet written** (Option 4's mechanism, for a role where it does fit)
- FRE-471 — truncate-with-warning guard (why a truncating model never yields zero artifact)
- FRE-478 / FRE-495 — output-cap and context-window incidents; why large-output capability is shown
  on the card
- FRE-869 — `get_llm_client_for_key` budget-lane correctness
- `src/personal_agent/tools/artifact_tools.py:1436-1437` — the hardcoded `sub_agent` builder binding
- `src/personal_agent/tools/artifact_tools.py:1468-1470` — the `respond(role=…)` telemetry identity
- `src/personal_agent/orchestrator/executor.py:462` — `_maybe_pause_for_constraint`
- `src/personal_agent/orchestrator/constraint_options.py:62` — the static options dict to widen
- `src/personal_agent/transport/events.py:139` — the closed `ConstraintPauseEvent.constraint` Literal
- `src/personal_agent/cost_gate/__init__.py:95-117` — `budget_role_for`; the builder's own lane
  (`"artifact_builder": "artifact_builder"`) is **already live** (FRE-879), not pending
- `src/personal_agent/request_gateway/intent.py:80-87` — `_ARTIFACT_BUILD_REGEX` (FRE-469), the
  existing trigger; `:119-120` — its union into `_TOOL_INTENT_PATTERNS`, which discards the
  distinction; `:309-311` — the generic `tool_intent_pattern` signal the live run observed
- `src/personal_agent/request_gateway/types.py:166-183` — `GatewayOutput.intent`, the seam already
  carrying the signal to the executor
- `src/personal_agent/orchestrator/executor.py:2567` — `step_init`, the turn-start insertion point
- `src/personal_agent/tools/artifact_tools.py:807-817` — `_draft_max_tokens`, the global constant to
  replace; `:1604` — the truncation warning gated on the same constant
- `config/models.yaml` — the live catalog whose declared `max_tokens` (Haiku 4096, gpt-5.4-mini 8192,
  Sonnet 32768) contradict the 32768 global
- FRE-928 — the constraint waiter bypassing its own timeout when no socket is attached; independent
  of this amendment and not fixed by it (§6)
- `src/personal_agent/transport/agui/ws_endpoint.py:250-259` — `register_constraint_waiter`'s
  no-socket path (why AC-5 splits the two no-answer cases)

---

## Status Updates

### 2026-07-21 - Amended (card moves to turn start; plan sized to the builder)
**Changed By:** cc-adrs (Opus)
**Reason:** Owner-directed after the **first live AC-7 run failed**. T1–T3 had shipped and deployed;
master measured the turn: request 07:01:47, card raised 07:03:44 — **117 s later**, dominated by a
55 s `perplexity_query` and a 43 s silent planning step. The card determination itself took **under a
millisecond**, because its options are the availability-filtered catalog and depend on nothing the
turn computes. The socket dropped at 07:03:33 (phone backgrounded during the silent wait) and
reconnected at 07:04:25, so the card was replayed for a decision already taken by default.

Owner's decision: **ask at the beginning of the turn**, so the model's parameters are known when the
build starts. This ADR's original rationale — "the artifact kind is only known at build time" — was
wrong on its own terms: the kind arrives with the *request*; only the *plan* arrives later, and the
plan refines size, not kind. Per-build selection is preserved; only the moment moves.

Design worked through for the amendment. **The trigger** is `_ARTIFACT_BUILD_REGEX`, which already
exists (`intent.py:80-87`, FRE-469) but is unioned into `_TOOL_INTENT_PATTERNS` (`:119-120`), so it emits
only the generic `tool_intent_pattern` — exactly what the live run showed. Emitting a distinct signal
is additive: no new classifier, no new plumbing (`GatewayOutput.intent` already reaches `step_init`).
**False positives** are harmless — the selection is turn-scoped and discarded unused. **False
negatives** degrade to the configured default (pre-card behaviour) and emit
`artifact_build_intent_missed`; the card is deliberately **not** re-raised late, since that
reintroduces the 117-second problem for exactly the requests we understand least and removes the
pressure to tune the vocabulary. **A stored preference** suppresses the ask in either placement and
must still thread the resolved deployment into planning — AC-13 guards the invisible failure where it
suppresses the card but not the constant.

**A live defect surfaced while grounding the amendment.** `_draft_max_tokens()`
(`artifact_tools.py:807-817`) returns `settings.artifact_draft_max_tokens` = **32768 static,
regardless of which model the card selected**. Against the live catalog, selecting `claude_haiku`
(declared `max_tokens` **4096**) makes the build request **8× its declared cap**; `gpt-5.4-mini`
(8192) 4×. The same constant gates the truncation warning, so it cannot fire at the real ceiling.
This shipped *with* the card and is a defect in the deployed feature, not merely an enhancement —
hence AC-12. FRE-478 was "fixed" by raising this global from 16384 to 32768, encoding a per-model
fact as a global constant; knowing the deployment before planning is what allows the root fix.

**FRE-928 is reduced-exposure, not fixed, by this amendment** (§6) and must ship on its own merits —
it is the prerequisite for AC-5's timeout leg. AC-5's no-socket wording was corrected: it previously
asserted **no** pause event is emitted with no socket, which contradicts FRE-928's replay design.
New: AC-10 (the ask precedes all turn work; a turn-scoped pick does not leak), AC-11 (a missed
prediction degrades *and logs*), AC-12 (budget follows the model in both directions), AC-13 (the
silent preference path is sized too), AC-14 (two turn-start decisions coexist without
contaminating each other). Sequencing steps 4–6 added; 1–3 marked shipped.

**Revised after codex review of the amendment.** Two blocking findings, both accepted. **(1) AC-10's
ordering claim was unprovable as written.** It asserted the pause event precedes the first
`MODEL_CALL_COMPLETED`, but the pause is an AG-UI session event carrying a Postgres `seq` while
`MODEL_CALL_COMPLETED` is a structlog telemetry event on a separate path — no shared monotonic
primitive, so a test could satisfy the letter without proving the claim. Re-anchored on the state
machine: the resolution must be on the context when `step_init` returns, before the transition out of
`TaskState.INIT` — directly assertable, and it fails against the deployed build-boundary placement.
Session-event `seq` ordering against tool events is retained as live corroboration, since those two
*do* share a stream. **(2) AC-12's overshoot leg was untestable** — no deployment in the live catalog
exceeds the 32768 ceiling (`claude_sonnet` at 32768 is the maximum), so "pick a model declaring more
than the ceiling" could never be exercised and an implementation that special-cased the undershoot
without ever writing the `min(...)` clamp would pass unchallenged. Rewritten to vary the *ceiling*
(set it to 2048, select Sonnet) rather than invent a deployment. Non-blocking: file paths corrected to
`src/personal_agent/…` and several cited line numbers refreshed against source (`step_init` at
`:2567`, the FRE-749 note at `:2466-2477`, the intent regex at `:80-87`/`:119-120`/`:309-311`).

**§3d and AC-14 were added before that review**, from a partial investigation log left by an earlier
codex run that hung before reporting: `step_init` already raises the `attachment_cost` pause
(`executor.py:2730`) immediately before the gateway block this amendment reads, so a turn can carry
two turn-start decisions. Ordering fixed (attachment first — declining it short-circuits the turn),
and the FRE-749 ambiguous-affirmative hazard asserted rather than argued.

### 2026-07-19 - Proposed
**Changed By:** cc-adrs (Opus)
**Reason:** Initial proposal, splitting the build-time artifact-builder card out of the superseded
ADR-0118/0119 pair and re-basing it on the ADR-0121 catalog. The owner's framing inverted ADR-0119's:
because different artifact *types* may suit different models, and the type is known only at build
time, the card is the primary affordance and the configured default is the fallback that may never
be changed — where ADR-0119 kept a config picker and deferred the card as a fast-follow. Carried
forward from ADR-0118: the DecisionCard-reuse analysis, the honest enumeration of the four contracts
that must widen, the `attachment_cost` Literal drift to close in passing, FRE-869's billing
correctness, and the two-identities finding (`budget_role` cost lane versus the `respond(role=…)`
telemetry role — both must switch or AC-1 passes falsely). Dropped from ADR-0118: the bespoke
`artifact_builder_candidates` registry, whose fields duplicated `ModelDefinition` and whose
`max_tokens` loader gate could not fail for the reason it existed (FRE-880) — options now come from
the ADR-0121 catalog. A standing config picker for this role is explicitly deferred as speculative.

**Revised after codex review round 1.** The first draft repeated ADR-0118/0119's claim that the
`artifact_builder` role was unbuilt. **That was false**, and verifying it against source rather than
against those documents is the whole lesson of this ADR chain: FRE-879 shipped completely on
2026-07-17 — `ModelRole.ARTIFACT_BUILDER` (`llm_client/types.py:29`), the role resolution in
`artifact_tools.py:1454-1455`, the telemetry identity on both axes (`:1473-1480`, `:1486-1489`), the
cost lane (`cost_gate/__init__.py:115-117`), and owner-confirmed budget caps
(`config/governance/budget.yaml:46-49, 57, 62`). The Context, Decision §1, sequencing, and files-
affected sections were rebased on that reality: step 1 is deleted as already-done, AC-2 becomes a
**regression guard** on shipped behaviour rather than a new capability, and the remaining scope is
selection only. AC-5 was found gameable — an implementation with no card at all would satisfy it
via the pre-existing default-on-timeout path — and now additionally requires that a
`ConstraintPauseEvent` was emitted and persisted. AC-1 now requires the selected model to differ from
every other role's resolved model so a fallback cannot coincidentally produce the expected id, and
AC-3 asserts per-*model* attribution rather than per-role. Stale line citations corrected
(`_maybe_pause_for_constraint` is at `executor.py:554`, not `:462`) and both sides of the
`attachment_cost` Literal drift now cited.

**Revised after codex review round 2.** AC-5's hardening had over-corrected: requiring an emitted
and persisted `ConstraintPauseEvent` in the **no-socket** case contradicts the very mechanism this
ADR claims to reuse — `register_constraint_waiter` returns immediately with
`resolution="connection_lost"` and deliberately does not invoke the push callback when no connection
is active (`transport/agui/ws_endpoint.py:250-259`). Demanding an event there would have forced a
change to ADR-0076 under the banner of reusing it. AC-5 now asserts the two no-answer cases
separately: the timeout case requires the persisted pause event; the no-socket case requires the
absence of one **plus** an `artifact_builder` resolution record, which preserves the discrimination
against a never-wired implementation without altering the reused mechanism. A stale reference
claiming the builder still bills to `main_inference` was also removed — it contradicted this ADR's
own corrected premise and would have sent an implementer back to the false starting point.
