# FRE-921 — ADR-0122 T3: PWA card rendering with catalog detail (assembled seam, AC-7)

**Ticket:** https://linear.app/frenchforest/issue/FRE-921
**ADR:** `docs/architecture_decisions/ADR-0122-build-time-artifact-builder-selection.md`
**Depends on:** FRE-882 (T2 — merged, PR #595, `Awaiting Deploy`)

## Scope

T2 already raises the `artifact_builder` constraint pause at the build boundary
(`tools/artifact_tools.py:1475-1481`), computes the availability-filtered
catalog options (`orchestrator/constraint_options.py:compute_artifact_builder_options`),
and wires the resolved key fail-closed to `get_llm_client_for_key`. What it did
**not** do is give the PWA `DecisionCard` anything but a bare `action_id` list —
`ConstraintPauseEvent.options: Sequence[str]` (`transport/events.py:157`) carries
only deployment keys, no cost/context/summary.

This ticket is PWA-only. It does not need a backend change to get catalog
detail onto the card: `GET /api/v1/sessions/{session_id}/config` (and the
sessionless `GET /api/v1/config`) already returns **every** role in
`config.roles`, including `artifact_builder` (`open: true` in
`config/model_roles.yaml:82`), with a full `candidates: DeploymentView[]` list
per open role (`gateway/session_api.py:556-569`, `_deployment_view` at
`:442-467`) — the same shape `ModelPicker.tsx` already renders for the
`primary` role. `StreamingChat.tsx` already calls `useSessionConfig(sessionId)`
and has `configRoles` in scope where it renders `<DecisionCard>` (`:60-64`,
`:416`). (Codex plan-review: confirmed `role_candidates()` does not drop
`artifact_builder` — `config/model_loader.py:469-477` — and it defaults to
`ModelKind.LLM` via `required_kind_for_role()` since it's absent from
`ROLE_KIND_REQUIREMENTS`, `llm_client/models.py:64-89`.)

So: cross-reference the constraint's bare `options` (action_ids) against
`configRoles.artifact_builder?.candidates` (matched by `.key`) inside
`DecisionCard`, and render the matched deployment's provider/placement, context
window, max output tokens, and cost when a candidate exists. Fall back to the
existing plain-label pill when it doesn't.

**Staleness caveat (codex plan-review finding):** `configRoles` is descriptive
only, fetched independently on a 60s poll (`useSessionConfig.ts:8,77-82`) and
via a slightly different availability predicate than the one that computed
`pending.options` at pause time (`build_provider_availability` vs.
`check_all_providers`'s live local-probe — `constraint_options.py:152-160` vs.
`provider_health.py:71-83`). So a rendered `action_id` can legitimately have no
matching candidate — e.g. the config poll hasn't caught up with a provider that
just became available/unavailable — and the plain-label fallback is what
degrades gracefully in that case. This is **not** the same as the
default-not-in-options case: `default_option` isn't part of `pending.options`
at all (`events.py:147-158`; `DecisionCard` only ever maps `pending.options`,
`DecisionCard.tsx:58-72`), so it never needs a fallback render in the first
place. The fallback path exists purely for a stale/missing catalog lookup on
an option that *is* being rendered — `pending.options` (sent by the WS waiter,
`ws_endpoint.py:350-364`) is what stays authoritative for which buttons exist
and which `action_id` gets sent; `builderCandidates` only decorates them when
available.

**Acceptance criterion owned here:** AC-7 (assembled seam) — proven live on
deploy, not by this PR's tests alone. This PR's job is the card leg: the card
must render real per-option detail so a real build-me-an-artifact request lets
the user compare on cost/context/max-output, and a non-default pick must still
send the same `action_id` (`candidate.key`) `onDecide` sent today, so the
already-shipped T2 backend runs the selected model. Nothing about the resolved
model, telemetry, or cost lane changes here — that is T2's job (AC-1/AC-2,
already merged). Live AC-7 verification happens post-deploy per the ticket's
own text ("Master asserts AC-7 at the acceptance gate" — ADR-0122 §Verification).

## Files touched

1. **`seshat-pwa/src/lib/constraint-options.ts`**
   Add `artifact_builder: 'Choose the artifact builder'` to `CONSTRAINT_TITLES`.
   No entry added to `CONSTRAINT_ACTION_LABELS` — the options are catalog
   deployment keys, not fixed action ids; `actionLabel()`'s existing
   fallback-to-raw-id behavior is what the plain-pill fallback path uses.

1b. **`seshat-pwa/src/lib/types.ts`** (added per codex plan-review finding #5)
   `DeploymentView.max_tokens` is currently typed `number` (`:181`) but the
   backend field (`ModelDefinition.max_tokens: int | None`,
   `llm_client/models.py:245-248`) is genuinely nullable and `_deployment_view`
   passes it through as-is (`session_api.py:461-462`). Widen to
   `max_tokens: number | null` so the card's "provider default" rendering for
   a null max-output is type-honest rather than silently relying on JS's loose
   `!= null` check against a type that claims it can't happen.

2. **`seshat-pwa/src/components/DecisionCard.tsx`**
   - Import `DeploymentView` from `@/lib/types`.
   - Add optional prop `builderCandidates?: DeploymentView[]` to
     `DecisionCardProps`.
   - For each option, look up `builderCandidates?.find(c => c.key === actionId)`
     when `constraint === 'artifact_builder'`. If found, render an enriched
     block-level button (provider/placement dot — same color convention as
     `ModelPicker.tsx`, key, provider, `{context_length/1000}K context`,
     `{max_tokens/1000}K max output` or `provider default` when `max_tokens`
     is nullish, cost per-M-tokens in/out when non-null, one-line `summary`
     truncated). If not found, render the existing plain-label pill
     unchanged (covers non-`artifact_builder` constraints and the
     candidate-missing edge case above).
   - No other behavior changes: `decide()`, the decide-once guard, the
     remember checkbox, and the countdown bar are untouched.

3. **`seshat-pwa/src/components/StreamingChat.tsx`**
   Pass `builderCandidates={configRoles.artifact_builder?.candidates}` to the
   existing `<DecisionCard>` call (`:416-421`). `configRoles` is already
   fetched in this component (`:60-64`) for the `primary`-role `ModelPicker`;
   no new fetch, no new hook call.

## Tests (TDD — failing first)

`seshat-pwa/src/__tests__/DecisionCard.test.tsx`:

- New fixtures: `BUILDER_CONSTRAINT` (`constraint: 'artifact_builder'`,
  `options: ['claude_opus', 'claude_haiku']`) and `BUILDER_CANDIDATES:
  DeploymentView[]` covering `claude_opus` only (cost, context, max_tokens,
  summary, provider, placement) — `claude_haiku` deliberately has no matching
  candidate, to exercise the fallback branch.
- `renders catalog detail (provider, context, max output, cost, summary) for a
  builder option with a matching candidate` — assert the formatted context/
  cost/max-output/summary strings are present.
- `falls back to the plain label pill when a builder option has no matching
  candidate` — asserts `claude_haiku` renders as a bare-label button (no cost
  text near it).
- `falls back to plain rendering entirely when builderCandidates is omitted`
  — regression guard: config still loading must not crash or dead-end the
  card.
- `calls onDecide with the candidate's key when a detail button is clicked` —
  proves the richer button still sends the same wire contract T2 already
  consumes.
- `ignores builderCandidates entirely for a non-artifact_builder constraint`
  (codex plan-review finding #4) — render `TOOL_LIMIT_CONSTRAINT` while also
  passing a `builderCandidates` prop whose keys collide with
  `continue_10`/`finish_now`; assert the plain labels render unchanged (no
  cost/context text leaks into an unrelated constraint just because a prop was
  passed).
- `renders "provider default" and omits cost text for a candidate with null
  max_tokens / null costs` (codex plan-review finding #4) — a local-model
  candidate fixture with `max_tokens: null`, `input_cost_per_token: null`,
  `output_cost_per_token: null`; assert no `$NaN`/`$0.00` text appears and the
  max-output line reads "provider default".
- Confirm the two existing `tool_iteration_limit` / `context_compression`
  test blocks are unaffected (no fixture changes there).

**`StreamingChat` prop plumbing (codex plan-review finding #4):** no dedicated
test added. The change there is a one-line prop pass — `builderCandidates=
{configRoles.artifact_builder?.candidates}` — directly mirroring the existing,
also-untested `modelCandidates = configRoles.primary?.candidates ?? []`
pattern already in that file (`:65`) that feeds `ModelPicker`. `StreamingChat`
has no existing test file and no established mocking harness for its
`useSSEStream`/`useSessionConfig` composition; standing one up for a one-line
prop pass is out of scope for this ticket. Flagged explicitly here (and in the
Linear handoff) rather than silently skipped, per codex's finding.

Run: `cd seshat-pwa && npm test -- DecisionCard`

## Quality gates

- `cd seshat-pwa && npm run lint` (FRE-395 lint gate — required for all PWA changes)
- `cd seshat-pwa && npm test`
- No `src/personal_agent` files change in this ticket, so the Python gates
  (`make test`, `make mypy`, `make ruff-check`) are expected to be no-ops;
  run `make ruff-check` and `make mypy` anyway as a cheap sanity check that
  nothing else drifted on this branch. `pre-commit run --all-files` before PR.

## Self-classification

**Standard** — this is a multi-file change implementing a step of an Accepted
ADR (ADR-0122 T3, the assembled seam), even though it is PWA-only and touches
no `src/personal_agent` logic. Codex plan-review requested before implementation.
