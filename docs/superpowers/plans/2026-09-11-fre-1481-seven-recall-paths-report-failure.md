# FRE-1481 — Seven more recall paths convert a failure into an empty success

**Ticket:** FRE-1481 (Approved, Tier-2:Sonnet) · **ADR:** ADR-0148 D1 · **Builds on:** FRE-1476

## Scope

FRE-1476 built the memory-context status (ADR-0148 D1) and made three paths report their own
failure instead of returning the same empty value an honest search returns: the dense arm, the
proactive adapter, and the broad/entity-resolution steps inside multipath. Codex plan-review on
that ticket found seven more collapses below the same seam. This ticket closes them, following
FRE-1476's own pattern rather than inventing a new one:

- A recall arm gathered by `_multipath_fused_recall`'s `asyncio.gather(..., return_exceptions=True)`
  reports failure by **raising**. The public method keeps its documented fail-open contract; a
  private `_..._strict` sibling does the real work and does not swallow.
- A step that is not a gathered arm (an adapter method, a legacy single-caller helper) reports
  failure through its own return type's `failed` / `arms_failed` field, exactly as
  `ProactiveMemorySuggestions.failed` already does.
- A zero-vector embedding is a failure, not an outcome — `generate_embedding` never raises, it
  always degrades to `[0.0] * dimensions`. Every site that calls it needs its own positive
  detection of that shape; a bare "stop swallowing exceptions" does not catch it.

**FRE-1120 overlap, resolved by reading it (per the ticket's own instruction):** FRE-1120's
*detection* half is the zero-vector check this chain (FRE-1476 + this ticket) implements at each
arm. Its *retry* and *turn-evidence* halves touch none of these seven sites — `generate_embedding`
itself gains no retry logic here. FRE-1120 stays open, narrower, and untouched by this PR. A short
comment goes on FRE-1120 at handoff noting this, not a scope change to that ticket.

## The seven sites, and the fix for each

### 1 — Lexical arm (`memory/service.py`, `lexical_recall_arm`, current ~4961-5045)

Split exactly like the dense arm (`dense_recall_arm` / `_dense_recall_arm_strict`,
service.py:5090-5208):

- Add `_lexical_recall_arm_strict(...)` — the existing query body, but the `except Exception as e`
  at the query (~5025-5031) logs `lexical_recall_arm_failed` then
  `raise RecallArmFailedError(f"lexical recall arm: {e}") from e` instead of `return []`.
- `lexical_recall_arm` becomes a thin wrapper: `try: return await self._lexical_recall_arm_strict(...)
  except Exception: log a warning; return []`.
- `_multipath_fused_recall`'s gather (service.py ~5261-5273): change
  `self.lexical_recall_arm(query_text, **arm_kwargs)` → `self._lexical_recall_arm_strict(query_text, **arm_kwargs)`.

### 2 — Structural arm (`memory/service.py`, `_run_structural_arm_query` ~4753-4841, `structural_recall_arm_ranked` ~4943-4959)

The core query helper is shared by two public callers (`structural_recall_arm`, the EntityNode
form, and `structural_recall_arm_ranked`, the one the fusion core gathers). Split at the core.

**Revised per codex plan-review: the strict query helper's `None` return must stay unambiguous.**
The first draft renamed the whole current function body (including its early gating return for
disabled/disconnected) to `_..._strict` and had it re-raise on DB failure, then had the ranked-strict
wrapper drop its own `if records is None: return []` check entirely — which would crash on `None`
enumeration for the surviving gated-off case, since gating never raises. Fixed design:

- Read the current function first to find its exact gating checks (disabled arm / not connected /
  empty params) that precede the `try`. Keep those checks, returning `None`, **outside** and
  **before** the strict/raising logic in `_run_structural_arm_query_strict` — gating is not a
  failure (ADR-0148 doesn't ask this ticket to also fix "arm disabled" reporting, only the seven
  named collapses) so it must stay a `None` the ranked-strict wrapper can still safely treat as
  "no records" without it having come from a swallowed exception.
- Only the `except Exception as e` around the DB query itself (~4821-4828) changes: log
  `structural_recall_arm_failed`, then `raise RecallArmFailedError(f"structural recall arm: {e}")
  from e` instead of `return None`.
- New thin `_run_structural_arm_query` wrapper (`try: return await
  self._run_structural_arm_query_strict(...) except Exception: log; return None`), used by
  `structural_recall_arm` (EntityNode form) exactly as the old function was used — unchanged
  observable behavior for that caller.
- Add `_structural_recall_arm_ranked_strict(...)` — calls `_run_structural_arm_query_strict`
  directly. Because that strict method's only remaining `None` case is the pre-try gating check
  (a DB failure now raises past that point), `if records is None: return []` is **kept**, unchanged
  from today's `structural_recall_arm_ranked` — it is now unambiguously "gated off," never a
  swallowed failure.
- `structural_recall_arm_ranked` stays a thin fail-open wrapper around the strict variant, same
  `None`-safe conversion it has today.
- Gather call: `self.structural_recall_arm_ranked(**arm_kwargs)` → `self._structural_recall_arm_ranked_strict(**arm_kwargs)`.
- **Test-migration requirement (codex finding):** `tests/personal_agent/memory/test_multipath_core.py:126,159,173,186`
  mock `structural_recall_arm_ranked` directly — migrate these to the new `_structural_recall_arm_ranked_strict`
  name, same dead-mock risk as the multi-query/lexical mocks noted below.

### 3 & 4 — Multi-query arm (`memory/service.py`, `multi_query_recall_arm` ~5875-5992)

Two independent failure points inside one arm, gathered directly today at
`arm_coros.append(self.multi_query_recall_arm(query_text, **arm_kwargs))`.

**Revised per codex plan-review (2026-09-11): granularity is ANY variant failure, not ALL.** The
first draft raised only when every variant failed, reasoning that a surviving variant meant the arm
"ran to completion." Codex correctly rejected this against ADR-0148 D2 (`docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md:266`):
"a partially degraded recall is therefore `UNAVAILABLE`, never `NOTHING_RELEVANT`" — the precedence
applies below arm granularity, not just across arms (the same file already applies it at
sub-candidate granularity for the reranker, `context.py:97`). The correct rule, and the one that
also matches every other arm's existing all-or-nothing shape (a single dense/lexical/structural
query exception drops that whole arm's contribution, never a partial one): **any** variant that
fails — raises, or returns a zero-vector embedding — makes the whole strict arm raise. This is a
simplification versus the first draft, not an addition: no per-variant success/failure tally is
needed, just fail-fast on the first bad variant.

Add `_multi_query_recall_arm_strict(...)`, used by the gather in place of the public method. Design:

- **Paraphrase step.** The public method's `except Exception` around `generate_query_paraphrases`
  (~5917-5932) is documented "defense in depth" — the collaborator already fails open internally, so
  this rarely fires. In the strict variant, do **not** catch it: let it propagate (or catch, log,
  `raise RecallArmFailedError(...) from e`). An empty paraphrase list is not a failure — the arm
  still searches the original query — only an actual raise is.
- **Per-variant embedding+search step.** For each variant, detect a zero-vector embedding (the
  ADR's "single most important detail", currently undetected here entirely) the same way the dense
  arm does. The strict variant does **not** catch a per-variant exception or continue past a
  zero-vector: the first bad variant raises `RecallArmFailedError("multi-query arm: variant '<text>'
  failed")` immediately, discarding whatever partial `arm_rankings` had accumulated — matching the
  existing all-or-nothing arm-level contract in `_multipath_fused_recall`'s gather, which already
  drops an arm's entire contribution on any exception (service.py ~5285-5296). The public method's
  per-variant isolation (`continue` past a bad variant, comment at ~5940-5942) is untouched and
  keeps that documented behavior — only the strict sibling used by the gather changes.
- **Session acquisition failure** (the outer try/except at ~5960-5973, zero variants attempted):
  strict variant does not swallow it — it propagates.
- Factor the per-variant loop into one internal helper shared by both the public method and the
  strict variant so the query logic is not duplicated — the two callers differ only in whether a
  per-variant failure is swallowed-and-continue or raised-immediately.
- Public `multi_query_recall_arm` keeps its documented "never hard-fails recall" contract exactly.
- Gather call: swap to `self._multi_query_recall_arm_strict(query_text, **arm_kwargs)`.

This is the site AC-3 targets directly: with `multiquery_arm_enabled` true and `generate_embedding`
returning a zero vector for even one variant, `_multipath_fused_recall`'s gather records
`"multi_query"` in `arms_failed`, which must reach `AssembledContext.memory_status.status ==
UNAVAILABLE`.

**Test-migration requirement (codex finding):** `tests/personal_agent/memory/test_multipath_core.py`
mocks `multi_query_recall_arm` and `lexical_recall_arm` directly (lines ~58, 126, 159, 173, 186), as
do `test_broad_recall_relevance.py:176`, `test_entity_match_relevance.py:174`, and
`tests/test_telemetry/test_fre_1219_validator_agreement.py:143`. Once the gather calls the `_strict`
siblings instead of the public methods, these mocks stop intercepting — a dead-mock false pass, the
same failure mode already documented in that test file's own header for the dense-arm split. Every
one of these must be migrated to mock the new `_lexical_recall_arm_strict` /
`_structural_recall_arm_ranked_strict` / `_multi_query_recall_arm_strict` names before this PR, not
just the structural one the first draft called out.

### 5 — `suggest_proactive_raw` (`memory/service.py` ~1011-1135)

Confirmed single caller: `MemoryServiceAdapter.suggest_relevant` (protocol_adapter.py:367).

**Revised per codex plan-review: preserve the site-specific cause, don't collapse into the generic
one.** The first draft just let the exception propagate to `suggest_relevant`'s broad outer
`except Exception` (protocol_adapter.py:407-417), which maps *every* failure in that whole
function — session-entity lookup, raw retrieval, scoring — to the same generic
`failure_cause="proactive_recall_failed"`. That discards the fact immediately after the raw layer
is the one place that knows it, contradicting ADR-0148 D1's own stated reason for per-stage
reporting ("the cause of a failed retrieval is known at the arm, for one stack frame, and is then
discarded" — `docs/architecture_decisions/ADR-0148-absence-must-be-reachable-and-sayable.md:227`).
Fixed design:

- `suggest_proactive_raw`'s `except Exception as e` (~1082-1088): keep the
  `suggest_proactive_raw_failed` warning log, then re-raise (`raise`) instead of `return []`.
- Wrap the call site at `protocol_adapter.py:367` (`raw = await self._service.suggest_proactive_raw(...)`)
  in its own narrow `try/except`, sibling to the existing zero-embedding early-return block
  immediately above it (lines 342-357), returning
  `ProactiveMemorySuggestions(candidates=[], query_embedding_ms=emb_ms, failed=True,
  failure_cause="proactive_raw_query_failed")` directly — a stable, site-specific cause, distinct
  from the outer catch's generic `"proactive_recall_failed"`, which still covers every other
  failure in the function.

### 6 — Legacy (non-multipath) `query_memory` (`memory/service.py` ~4233-4718)

Only caller that reaches `request_gateway`'s status assembly: `MemoryServiceAdapter.recall`
(protocol_adapter.py:89), taken when `multipath_recall_enabled` is off or `query_text` is empty
(dispatch at service.py ~4270-4283). `MemoryQueryResult` (`memory/models.py:321-359`) already
carries `arms_failed: list[str]` — added by FRE-1476 but never set on this path.

**Widened per codex plan-review, same function, two more collapses codex found while checking this
site — folded in per the build skill's "meet the objective, fold in supporting changes" rule rather
than left half-fixed in a function this PR is already editing:**

- Initialize `arms_failed: list[str] = []` near the top of `query_memory`, threaded through to
  every `MemoryQueryResult(...)` construction in the function (the two below, plus the existing
  success-path return at ~4673-4677, which must also pass `arms_failed=arms_failed` so a partial
  degradation recorded mid-function is not dropped on an otherwise-successful return).
- The `not self.connected` early return (~4266-4268): currently returns a bare empty result.
  `query_memory_broad` already has the precedent for this exact case —
  `arms_failed=("not_connected",)` (service.py ~6045). Match that string: return
  `MemoryQueryResult(arms_failed=["not_connected"])`.
- The inner vector-search-only `except` (~4327-4333): today it swallows and lets the legacy path
  continue as if that sub-step completed — the same "attempted, quietly degraded" shape the plan
  already fixes for the multi-query arm's per-variant step. Append `"query_memory_vector_search"` to
  the local `arms_failed` list (log as today; do not return early — the existing continue-with-
  partial-results behavior for this sub-step is correct and untouched, only its failure becomes
  visible now).
- The outer `except Exception as e` (~4710-4718): `return MemoryQueryResult(arms_failed=[*arms_failed, "query_memory"])`.
- No other file changes needed for propagation — `MemoryServiceAdapter.recall` already forwards
  `result.arms_failed` into `MemoryRecallResult.arms_failed`, and `context.py`'s existing
  `_arms_cause` / `_stage_report` composition already consumes it.

### 7 — `resolve_message_entities` (`memory/protocol_adapter.py` ~284-313) + its consumer (`request_gateway/context.py` ~717-789)

The adapter's return type changes shape, per the ADR text ("needs a shape that can express
failure, since an empty list cannot") — mirrors `ProactiveMemorySuggestions.failed` /
`.failure_cause`, sized down:

- New frozen dataclass (placement: `memory/protocol.py`, alongside `MemoryRecallQuery` /
  `BroadRecallResult`, which is the shared protocol-contract types module):
  ```python
  @dataclass(frozen=True)
  class EntityResolutionResult:
      """Names the graph resolved from a message, or why resolution did not complete (FRE-1481)."""
      names: list[str]
      failed: bool = False
      failure_cause: str | None = None
  ```
- `MemoryProtocol.resolve_message_entities`'s abstract signature changes its return annotation to
  `EntityResolutionResult`. `@runtime_checkable` on `MemoryProtocol` only validates attribute
  presence, not return shape (codex finding) — `tests/personal_agent/memory/test_protocol.py:268`'s
  structural-conformance check will keep passing even against a fake still returning `list[str]`,
  so it is not migration coverage and every construction site below needs its own direct assertion.
  Codex's grep found more sites than the first draft's plan named; update all of them (re-grep at
  implementation time in case this list has drifted or missed one — codex's own pass may not be
  exhaustive either):
  - `tests/personal_agent/memory/test_protocol.py:211,253` — the local `MemoryProtocol` fake
    implementor itself still returns `list[str]`.
  - `tests/personal_agent/memory/test_protocol.py:458,481` — adapter result assertions.
  - `tests/personal_agent/memory/test_entity_match_relevance.py:312`
  - `tests/personal_agent/request_gateway/test_recall_candidates.py:51,190,353`
  - `tests/personal_agent/request_gateway/test_context.py:337,378,417`
  - `tests/personal_agent/request_gateway/test_memory_status_assembly.py:42,194,259`
    (`AsyncMock(return_value=["Sailing"])` and siblings)
- `MemoryServiceAdapter.resolve_message_entities` (protocol_adapter.py:284-313): success returns
  `EntityResolutionResult(names=result)`; the existing `except Exception` returns
  `EntityResolutionResult(names=[], failed=True, failure_cause="entity_resolution_failed")` instead
  of `[]`.
- `request_gateway/context.py` (~723-728), the only call site: unpack the result and set the
  function's existing `failure_cause` local (already declared above the `try`, already threaded
  into both `_stage_report(failure_cause)` calls at 757-773 and 783-789 — no further change needed
  there):
  ```python
  resolution = await memory_adapter.resolve_message_entities(...)
  entity_names = resolution.names
  if resolution.failed:
      failure_cause = resolution.failure_cause or "entity_resolution_failed"
  ```
- Delete the stale comment at ~784-788 ("a declared limit... Fixing it belongs with the other
  adapter-level collapses, not in this ticket") — this ticket is that fix.
- This automatically gives the correct D2 precedence for free: if resolution fails but proactive
  still finds real candidates (it does not strictly need `mentioned_entity_names`), `failure_cause`
  was already set before the proactive call, so both existing return points already compose
  `UNAVAILABLE` via `_stage_report`. No new precedence logic needed — the existing plumbing already
  does the right thing once `failure_cause` is set at the right point.
- **Revised per codex plan-review: don't silently drop a cause if both stages fail.** The function's
  single `failure_cause: str | None` local means that if entity resolution fails *and* the
  subsequent proactive call also fails, the proactive assignment at `context.py:755-756`
  unconditionally overwrites the resolution's cause — status still correctly composes `UNAVAILABLE`
  (codex confirmed this), but the evidence record loses which stage(s) actually failed. Fix by
  accumulating rather than overwriting: change the local to `failure_causes: list[str] = []`,
  `.append(...)` at both existing assignment points (the resolution-failure branch this ticket adds,
  and the existing `if suggestions.failed:` branch) instead of assigning, and pass
  `",".join(failure_causes) if failure_causes else None` into `_stage_report(...)` at both existing
  call sites — mirroring the join convention `_arms_cause` already uses one function above
  (`context.py:81-90`). Minimal, mechanical change; no new precedence logic.

## Test plan (one per site, AC-1/AC-2 shape: inject-and-fail + companion succeed)

New files, mirroring `tests/personal_agent/memory/test_dense_arm_reports_failure.py`'s structure
(one `Test...` class per failure mode, each with a companion "opposite case still passes" test):

- `tests/personal_agent/memory/test_lexical_arm_reports_failure.py`
- `tests/personal_agent/memory/test_structural_arm_reports_failure.py`
- `tests/personal_agent/memory/test_multiquery_arm_reports_failure.py` — covers both sub-sites
  (paraphrase raise, single-variant-zero-vector) as two `Test...` classes. Per the revised
  any-variant-fails rule, the companion test is now: one variant zero-vector, one variant real →
  arm still raises (proves the rule is any-fails, not all-fail) — the inverse of the first draft's
  planned companion.
- Extend `tests/personal_agent/memory/test_proactive.py` with the `suggest_proactive_raw` DB-raise
  case (asserting `ProactiveMemorySuggestions.failed`/`.failure_cause` on the existing
  `suggest_relevant` outer-catch path — no new file needed, this is one caller's behavior change).
- `tests/personal_agent/memory/test_query_memory_legacy_reports_failure.py` — legacy `query_memory`:
  outer exception → `"query_memory"` in `arms_failed`; disconnected → `"not_connected"`; inner
  vector-search failure → `"query_memory_vector_search"` present alongside a still-returned partial
  result (proves the degrade-with-visible-cause behavior, not just a hard failure); companion
  asserts a fully healthy run leaves `arms_failed` empty.
- `tests/personal_agent/memory/test_entity_resolution_reports_failure.py` — adapter-level unit test
  for `EntityResolutionResult`, plus a direct-invocation assertion against the `test_protocol.py`
  fake (not just the `@runtime_checkable` structural check, which codex confirmed does not validate
  return shape).
- Test-double migration (codex's full list, §7 above): update every construction site in
  `test_protocol.py` (211/253 fake, 458/481 adapter assertions), `test_entity_match_relevance.py:312`,
  `test_recall_candidates.py:51,190,353`, `test_context.py:337,378,417`, and
  `test_memory_status_assembly.py:42,194,259` to the new `EntityResolutionResult` shape.
- Strict-seam mock migration (codex finding, also in §2-4 above): update every existing mock of
  `lexical_recall_arm`, `structural_recall_arm_ranked`, and `multi_query_recall_arm` in
  `test_multipath_core.py` (~58,126,159,173,186), `test_broad_recall_relevance.py:176`,
  `test_entity_match_relevance.py:174`, and `test_fre_1219_validator_agreement.py:143` to target the
  new `_strict` method names — otherwise these become dead mocks that silently stop testing what
  they claim to.
- Extend `tests/personal_agent/request_gateway/test_memory_status_assembly.py` with:
  - the AC-3 case: multi-query arm enabled, `generate_embedding` returns zero-vector for one
    variant → `AssembledContext.memory_status.status == UNAVAILABLE`.
  - the entity-resolution-failure case: `resolve_message_entities` mocked to return
    `EntityResolutionResult(names=[], failed=True, failure_cause=...)`, proactive returns real
    candidates → status is still `UNAVAILABLE` (proves the D2 precedence works end to end, not just
    that `failure_cause` gets set).
  - the double-failure case: both entity resolution and proactive fail → both causes appear in the
    joined evidence string (proves the accumulate-don't-overwrite fix).

Each new/changed test file gets run individually while iterating:
`make test-file FILE=tests/personal_agent/memory/test_lexical_arm_reports_failure.py` (etc.), then
the full run before the PR: `make test`.

## Risk tier and codex plan-review

**Standard/Complex** — touches `src/` logic across four files in the memory and request-gateway
layers, changes a public adapter method's return type (`resolve_message_entities`), and the
multi-query arm's variant-failure rule is a real design decision, not mechanical repetition of
FRE-1476.

**Codex plan-review completed 2026-09-11.** Verdict: "Not ready for TDD implementation as-is,"
six findings, all addressed in this revision:
1. Multi-query granularity was wrong against ADR-0148 D2 — fixed to any-variant-fails (§3&4 above).
2. `suggest_proactive_raw`'s cause would have collapsed to the generic one — fixed with a narrow
   catch at the call site (§5 above).
3. The entity-resolution test-double inventory was incomplete — expanded to codex's full list (§7
   above).
4. The structural split's strict helper would have broken on its own gated-off `None` — fixed by
   keeping the gating check outside the raising logic (§2 above).
5. `MemoryQueryResult()` equality risk — codex found none; no test asserts default-model equality
   on a `query_memory` failure. No change needed.
6. Two more collapses in `query_memory` codex found while checking site 6 (the disconnected early
   return, and the inner vector-search-only swallow) — folded into §6 above, same function this
   ticket already edits.

Not re-sent for a second codex round — every finding traces to a verified source citation in the
review, and the fixes are mechanical applications of the pattern codex itself endorsed (the
any-variant-fails rule now matches the existing arm-level all-or-nothing shape; the cause-narrowing
and cause-accumulation fixes reuse conventions — `_arms_cause`'s join, the zero-embedding
early-return shape — already present in the files being edited). If TDD implementation surfaces a
further design question, escalate then rather than spend a second review round speculatively.

## Quality gates

`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`,
then self-review (`feature-dev:code-reviewer` scoped to `git diff origin/main...HEAD`; no
security-review trigger here — no new inputs, subprocess, files, auth, or network surface).
