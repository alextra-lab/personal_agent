# FRE-684 — ADR-0102 T4: Capability routing + fail-closed + per-attachment override

**Ticket:** [FRE-684](https://linear.app/frenchforest/issue/FRE-684/adr-0102-t4-capability-routing-fail-closed-per-attachment-cloudlocal)
**Backing ADRs:** ADR-0102 §1 (Tier 1 text — no capability required), §3 (Tier-2 delivery precedence),
ADR-0101 §5/§8a (the routing seam this extends), ADR-0044, ADR-0033
**Branch:** `fre-684-capability-routing-turn-assembly`
**Direct follow-on to FRE-683** (T3, merged as #508). **Revision 2** — round 1 of codex plan-review
(verdict: approve with changes) surfaced a real design bug in round 1's plan; this revision fixes it.
See "What changed from round 1" at the bottom.

## What already exists (confirmed by research, not re-added)

- `AttachmentRef.processing_target: Literal["cloud", "local"] | None` — already on the carrier
  (`orchestrator/types.py:171`).
- The generic ADR-0065 cost-gate reservation (`CostGate.reserve()`, `litellm_client.py:473`) wraps every
  cloud `LiteLLMClient.respond()` call unconditionally, sized off the actual outgoing `messages`
  payload. **Caveat (codex round 1):** the reservation amount is not a perfect actual-token count — it
  uses `litellm.token_counter()` with a fallback and can reserve based on unknown/absent pricing
  (`cost_estimator.py:122-150`); it is a real cap, not a precise estimate. Still, it is a hard ceiling
  that fires unconditionally and independently of anything this ticket does.
- `document_resolution.py` (T3) is not called anywhere in `executor.py` yet.

## The bug round-1 missed, and the fix

**The bug:** ADR-0102 §1 requires Tier 1 (text extraction) to "work on **any** model (no vision
capability required)". Round 1's design computed document routing *eagerly*, before knowing whether a
given PDF would resolve to Tier 1 or Tier 2 — so a plain local text-only model (no `supports_vision`,
no `supports_pdf_document`) would have been **incorrectly fail-closed** on a perfectly ordinary
native-text PDF that never needed vision at all.

**Why this is architecturally awkward to fix:** whether a document is Tier 1 or Tier 2 is only knowable
*after* fetching and parsing its bytes — which is `resolve_documents`'s (T3's) job, not the routing
layer's. Routing happens logically "before" resolution (it needs to hand `resolve_documents` a delivery
mode), but the capability check must only apply *if* Tier 2 actually turns out to be needed.

**The fix — make the capability check lazy, invoked only when Tier 2 is actually reached:**

### 1. `document_resolution.py` (T3, small interface change — this ticket's one T3 edit)

Change `resolve_documents`'s Tier-2 delivery parameter from a static value to a **caller-supplied
callback**, invoked only at the point a specific document is classified Tier 2 (i.e. exactly where
`tier == "text"` already returns early today, `document_resolution.py:239-243` — nothing above that
line changes):

```python
async def resolve_documents(
    attachments: Sequence[AttachmentRef],
    *,
    resolve_tier2_delivery: Callable[[], Literal["native_pdf", "rasterize"]],
    trace_id: str | None = None,
    session_id: str | None = None,
    task_id: str | None = None,
) -> ResolvedDocuments:
```

`ResolvedDocuments` gains one field: `used_tier2: bool` — `True` iff `resolve_tier2_delivery` was
invoked at least once across all documents in the turn. Set inside `_resolve_one_document`'s Tier-2
branch (right after the existing `if tier == "text": return ...` early-return, i.e. once we know we're
past it) and threaded back up through `resolve_documents`'s aggregation loop the same way
`running_total_bytes`/`remaining_page_budget` already are.

A document with a text layer dense enough for Tier 1 **never invokes the callback** — so a model with
neither `supports_pdf_document` nor `supports_vision` can still fully serve a Tier-1-only turn, per
ADR-0102 §1. `AttachmentUnsupportedError` raised by the callback propagates naturally through
`resolve_documents`'s existing per-document `try/except AttachmentUnsupportedError` re-raise
(`document_resolution.py:377-386`) — no new exception handling needed there.

**Existing T3 tests to update (mechanical):** every call site in `test_document_resolution.py` that
passes `tier2_delivery="native_pdf"` / `"rasterize"` becomes
`resolve_tier2_delivery=lambda: "native_pdf"` / `lambda: "rasterize"`. ~20 call sites, pure mechanical
substitution, no assertion changes (the tests still test exactly the same behavior — they're just
supplying the delivery mode via a trivial constant-returning callback instead of a literal).

### 2. `ExecutionContext` gains one field (`orchestrator/types.py`)

```python
document_effective_model_key: str | None = None
```
Set during turn-assembly (`step_init`) **iff** `resolve_documents` actually invoked the callback
(`doc_resolved.used_tier2 is True`) — i.e. iff a document-driven routing/capability decision actually
happened this turn. `None` means "no document-driven override; whatever `_resolve_vision_routing_key`
would say (unchanged) is authoritative" — the pre-existing image-only behavior, byte-for-byte.

### 3. `_resolve_document_routing_key(ctx, role_name) -> tuple[str, Literal["native_pdf", "rasterize"]]`
(new, `executor.py`, alongside `_resolve_vision_routing_key`)

**Unchanged from round 1** — this is the actual capability-precedence logic, just now invoked lazily
(as the callback body) instead of eagerly. Mirrors `_resolve_vision_routing_key`'s exact skeleton
(local-override / cloud-override / profile-default-then-escalate, "local wins" on conflict,
`AttachmentUnsupportedError` fail-closed), with a **combined** capability predicate:

```python
def _capable(model_def) -> Literal["native_pdf", "rasterize"] | None:
    if model_def is None:
        return None
    if needs_vision and not model_def.supports_vision:
        return None  # a raster image attachment in this turn demands vision regardless of PDF flags
    if model_def.supports_pdf_document:
        return "native_pdf"
    if model_def.supports_vision:
        return "rasterize"
    return None
```
where `needs_vision = any(a.content_type in RASTER_CONTENT_TYPES for a in ctx.attachments)` — so a
mixed image+document turn requiring Tier-2 delivery for the document still correctly requires the
chosen model to *also* support the image. `effective_target` (the local/cloud/none override) is
computed over the union of raster + PDF attachments, matching the existing "local wins" precedent
(`test_conflicting_overrides_local_wins`).

`_resolve_vision_routing_key` **is not touched** — zero regression risk to its 10 existing tests.

### 4. `_effective_attachment_routing_key(ctx, role_name) -> str` (new, tiny DRY helper)

```python
def _effective_attachment_routing_key(ctx: ExecutionContext, role_name: str) -> str:
    return ctx.document_effective_model_key or _resolve_vision_routing_key(ctx, role_name)
```
Used at **both** places that currently call `_resolve_vision_routing_key` directly:
`_maybe_confirm_attachment_cost` (executor.py:1642) and `step_llm_call` (executor.py:3045). This is the
correctness fix for a subtler bug round 1 would have shipped: if a document forces escalation to a
cloud model, the **image** cost-gate check (`_maybe_confirm_attachment_cost`, still image-blocks-only
per the scope boundary below) must check against *that same* effective model — not silently recompute
via image-only logic and land on a stale/different (e.g. still-local) key, which would skip the cost
confirmation for what is actually now a cloud call. No new cost-estimation logic is added; this only
fixes *which model* the existing image-only estimate is checked against.

## Scope boundary — what this ticket still does NOT do

- **No new cost-estimate logic for documents.** `_maybe_confirm_attachment_cost` still only receives
  image blocks (`resolved_blocks`, unchanged) — document blocks are not added to what it estimates
  against. T5 (FRE-686) owns the document-appropriate per-page estimator. **Interim gap, explicitly
  accepted:** a cloud-routed native-PDF turn gets **no** disclose-and-confirm prompt before spending
  (unlike images) — only the generic hard `cap_usd` reservation ceiling bounds it, and that ceiling's
  accuracy depends on `litellm.token_counter()`/known pricing (see caveat above, not a precise
  per-page estimate). This is a real, documented interim UX gap, not silently glossed over — it is
  T5's explicit job to close it, and T5 is already blocked-by this ticket in Linear (sequenced next).
- **No PWA changes** (T8/FRE-687). **No joinability-probe assertions** (T6/FRE-688).
- **Mixed-turn block ordering:** `content` places all resolved image blocks before all resolved
  document blocks (`resolved_blocks + document_blocks`), not global submission order across types. Each
  resolver preserves order *within* its own content-type set; cross-type interleaving is not attempted.
  Accepted v1 simplification — the ADR doesn't specify a cross-type ordering requirement, and T8 (PWA
  override affordance) hasn't shipped multi-type turns yet in practice.

## Turn-assembly wiring (`step_init`, ~executor.py:2272-2301)

```python
content: MessageContent = ctx.user_message
resolved_blocks: tuple[dict[str, Any], ...] = ()   # image-only; feeds _maybe_confirm_attachment_cost unchanged
document_blocks: tuple[dict[str, Any], ...] = ()
document_disclosures: tuple[str, ...] = ()

if ctx.attachments:
    from personal_agent.orchestrator.attachment_resolution import resolve_attachments
    from personal_agent.orchestrator.document_resolution import PDF_CONTENT_TYPES, resolve_documents

    resolved = await resolve_attachments(
        ctx.attachments, trace_id=ctx.trace_id, session_id=ctx.session_id, task_id=None
    )
    resolved_blocks = resolved.blocks

    if any(a.content_type in PDF_CONTENT_TYPES for a in ctx.attachments):
        doc_resolved = await resolve_documents(
            ctx.attachments,
            resolve_tier2_delivery=lambda: _resolve_document_routing_key(ctx, ModelRole.PRIMARY.value)[1],
            trace_id=ctx.trace_id, session_id=ctx.session_id, task_id=None,
        )
        document_blocks = doc_resolved.blocks
        document_disclosures = doc_resolved.disclosures
        if doc_resolved.used_tier2:
            ctx.document_effective_model_key, _ = _resolve_document_routing_key(
                ctx, ModelRole.PRIMARY.value
            )
            # ^ deliberately recomputed rather than captured off the closure: the callback only
            # returns the delivery *mode* to resolve_documents (its actual contract); this second
            # call is cheap and pure (no I/O — config/profile lookups only), same pattern already
            # used elsewhere in this codebase for recomputing a deterministic routing decision.

    ctx.attachment_disclosures = list(resolved.disclosures) + list(document_disclosures)
    all_blocks = resolved_blocks + document_blocks
    if all_blocks:
        content = (
            [{"type": "text", "text": ctx.user_message}, *all_blocks]
            if ctx.user_message else list(all_blocks)
        )
ctx.messages.append({"role": "user", "content": content})

if resolved_blocks and not await _maybe_confirm_attachment_cost(ctx, resolved_blocks):
    return TaskState.SYNTHESIS
```

**Fail-closed propagation (corrected from round 1):** if the callback raises
`AttachmentUnsupportedError` (invoked inside `resolve_documents`, re-raised up through `step_init`), it
propagates to the state machine's outer handler. This is **not** merely a generic fatal error as round 1
described it — `execute_task_safe()` classifies `ctx.error` via `classify_error()`
(`error_classification.py:172-180` has a specific `AttachmentUnsupportedError` classifier) and uses that
classified, original message as the reply when there's no `final_reply` set (`executor.py:4235-4252`).
So AC-6/AC-8's "clear, user-visible" requirement is met via this **existing, already-tested classifier
path** — same mechanism a `resolve_attachments` guardrail failure already relies on for images today.

## `step_llm_call` wiring (~executor.py:3037-3061)

```python
effective_model_key = _effective_attachment_routing_key(ctx, model_role.value)
```
replaces the direct `_resolve_vision_routing_key(ctx, model_role.value)` call. Telemetry log-gating
(`vision_routing_decision`) extended to also fire when a PDF attachment is present (currently gated on
raster-only).

## Files

- **Edit:** `src/personal_agent/orchestrator/document_resolution.py` — `resolve_documents` signature
  change (`tier2_delivery` → `resolve_tier2_delivery` callback), `ResolvedDocuments.used_tier2: bool`.
- **Edit:** `src/personal_agent/orchestrator/types.py` — `ExecutionContext.document_effective_model_key`.
- **Edit:** `src/personal_agent/orchestrator/executor.py` — `_resolve_document_routing_key`,
  `_effective_attachment_routing_key`, turn-assembly wiring, `step_llm_call` +
  `_maybe_confirm_attachment_cost` swapped to the new helper.
- **Edit:** `tests/personal_agent/orchestrator/test_document_resolution.py` — mechanical
  `tier2_delivery=` → `resolve_tier2_delivery=lambda: ...` substitution across ~20 call sites (T3's
  existing tests; no assertion changes).
- **Edit:** `tests/test_orchestrator/test_routing.py` — new `TestDocumentRouting` class.
- **Edit:** `tests/test_orchestrator/test_executor.py` — extend/add turn-assembly wiring tests.

## Test plan

**`TestDocumentRouting`** (`test_routing.py`, mirrors `TestVisionRouting`'s exact helper conventions,
10 existing tests in that class — corrected count from round 1's "11"):

- `test_no_pdf_attachment_is_not_this_functions_concern` — sanity: `_resolve_document_routing_key` is
  only ever invoked lazily by the callback, never for image-only turns (proven at the wiring-test level,
  not here — this class tests the function in isolation given it's called).
- **AC-6:**
  - `test_native_pdf_capable_primary_no_override_proceeds` → `("primary", "native_pdf")`.
  - `test_vision_only_primary_falls_back_to_rasterize` → `("primary", "rasterize")`.
  - `test_incapable_primary_escalation_permitted_escalates_to_native_pdf`.
  - `test_primary_and_escalation_both_non_capable_fails_closed` — **the literal AC-6 case.**
- **AC-8 (backend):**
  - `test_local_override_never_escalates_even_under_cloud_profile`.
  - `test_cloud_override_forces_native_pdf_even_on_local_profile`.
  - `test_cloud_override_fails_closed_when_no_escalation_model_configured`.
- **Mixed-turn combined predicate:**
  - `test_mixed_image_and_document_requires_vision_even_if_pdf_native_capable` — a candidate model has
    `supports_pdf_document=True`, `supports_vision=False`, and the turn also carries a raster image →
    disqualified.
  - `test_mixed_image_and_document_prefers_native_pdf_when_both_supported`.

**Tier-1 bypass (the round-1 bug, now the primary regression guard — `test_document_resolution.py`):**

- `test_text_dense_pdf_never_invokes_tier2_delivery_callback` — a `resolve_tier2_delivery` callback that
  raises `AssertionError` if ever called, fed a native-text PDF → `resolve_documents` returns a text
  block, callback never invoked, `used_tier2 is False`. **This is the test that would have caught round
  1's bug** — codex's finding, now locked in.
- `test_scanned_pdf_invokes_tier2_delivery_callback_exactly_once` — `used_tier2 is True`, callback
  call count == 1 (not once per selected page).

**Turn-assembly wiring** (`test_executor.py`):

- `test_document_attachment_injects_document_block`.
- `test_tier1_document_does_not_set_document_effective_model_key` — a Tier-1 (mocked) resolve_documents
  result with `used_tier2=False` → `ctx.document_effective_model_key` stays `None` after `step_init`.
- `test_tier2_document_sets_document_effective_model_key` — `used_tier2=True` →
  `ctx.document_effective_model_key` is set to `_resolve_document_routing_key`'s key for the mocked
  config.
- `test_document_disclosures_merged_onto_ctx`.
- `test_document_routing_failure_propagates_as_attachment_unsupported` — corrected from round 1: asserts
  the specific exception type propagates from `step_init`, and (separately) that `classify_error`
  produces the expected user-facing message for it (may already be covered by an existing
  `error_classification` test — check before duplicating).
- `test_document_blocks_do_not_reach_maybe_confirm_attachment_cost` — **corrected from round 1's wrong
  assertion**: a document-only turn (no raster images) → `_maybe_confirm_attachment_cost` is **not
  called at all** (mock `assert_not_called()`), matching the real `if resolved_blocks and not await
  ...` gating (`resolved_blocks` stays empty-tuple for a document-only turn, which is falsy).
- `test_maybe_confirm_attachment_cost_uses_document_effective_model_key_when_set` — a mixed turn where
  `ctx.document_effective_model_key` is pre-set to a cloud key → `_maybe_confirm_attachment_cost`'s
  internal effective-key lookup uses it (not a stale `_resolve_vision_routing_key` result) — the
  consistency fix from finding item 4 above.

## Quality gates

- `make test-file FILE=tests/personal_agent/orchestrator/test_document_resolution.py` (mechanical
  update regression) → `make test-file FILE=tests/test_orchestrator/test_routing.py` →
  `make test-file FILE=tests/test_orchestrator/test_executor.py` → full `make test`, `make mypy`,
  `make ruff-check` + `make ruff-format`, `pre-commit run --all-files`.
- Self-review: `code-review` at **high** effort (core routing/fail-closed seam + a T3 API change).
  `security-review` per policy (routing/auth-adjacent logic).

## Acceptance-criteria proof (for the Step-9 ticket comment)

- **AC-6** — the four AC-6 tests in `TestDocumentRouting`, plus
  `test_text_dense_pdf_never_invokes_tier2_delivery_callback` proving the fail-closed check doesn't
  over-fire on documents that don't need it (ADR-0102 §1 compliance).
- **AC-8 (backend)** — the three AC-8 tests in `TestDocumentRouting`; cost-gate reservation itself
  proven by existing generic `test_litellm_gate_wiring.py` coverage (no new reservation code added).

## What changed from round 1 (codex plan-review, verdict: approve with changes)

1. **Fixed the Tier-1 over-eager-fail-closed bug** — the actual bug, via the lazy-callback redesign
   above (§"The bug round-1 missed, and the fix").
2. **Corrected the cost-safety wording** — the reservation is a hard cap, not a precise actual-token
   estimate; explicitly stated the interim disclose-UX gap for documents rather than asserting it away.
3. **Corrected the fail-closed section** — cites `execute_task_safe`/`classify_error`, not a bare
   "generic fatal error."
4. **Added the `_effective_attachment_routing_key` consistency fix** — so a document-driven escalation
   doesn't leave the image cost-gate checking a stale key.
5. **Fixed the wrong test assertion** — document-only turns assert `_maybe_confirm_attachment_cost`
   `assert_not_called()`, not called-with-empty-tuple.
6. **Documented (didn't silently drop) the mixed-turn cross-type ordering simplification.**
7. **Corrected the "11 tests" → "10 tests" miscount** in `TestVisionRouting`.
