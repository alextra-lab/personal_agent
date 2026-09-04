# FRE-1376 — Gate the DELEGATE route; word-boundary the coding-keyword substring match

Ticket: https://linear.app/frenchforest/issue/FRE-1376
Backing refs: `request_gateway/intent.py:40-63,271-283` · `request_gateway/decomposition.py:103` ·
`orchestrator/executor.py:4675` · `delegation/adapters/` · FRE-1377 (out of scope — router redesign)

## Root cause (confirmed against the real session)

Session `5014ca54-4975-4eab-9518-a7f4c2a80e54` (Postgres `sessions.messages`, read-only query
against `cloud-sim-postgres`) — exact 1,900-char user message extracted and used verbatim as the
AC-1 fixture. Message opens with "Research " (matches `_ANALYSIS_PATTERNS`) but contains
"...harnesses **implement these** three capabilities..." which the bare substring check
`"implement the" in message.lower()` matches, firing rule 3 (coding→DELEGATION) before rule 5
(analysis) is ever reached — confirmed no other `_CODING_PATTERNS` alternative fires on this text.

Separately, `decomposition.py:103` maps `TaskType.DELEGATION` unconditionally to
`DecompositionStrategy.DELEGATE`; `config/settings.py` has no field configuring a delegation
target; three adapters exist un-wired. A misroute to DELEGATION today always dead-ends.

**Scope discipline** (owner-directed on the ticket): fix this defect only. No rule-ladder
redesign (FRE-1377), no general keyword-routing solution.

## Revisions after codex plan-review (verdict: "Ready with fixes noted")

Codex independently verified the 8 core regex cases pass, confirmed decomposition.py is the
correct gate location (executor.py only ever reads `decomposition.strategy`), and confirmed the
complexity-based fallback is defensible. It flagged four real issues, all addressed below:

1. **`(?!ing)` lookahead is fragile** — it also lets "unit tester"/"unit testability"/"integration
   testers" through as false matches, and `_CODING_PATTERNS`'s version has no leading boundary
   (matches "subunit tests"). Fixed by switching to `\btests?\b` (boundary after the optional
   plural), which excludes all of those by construction.
2. **`delegation_target: str | None` is ambiguous** — `executor.py:4724`'s
   `compose_delegation_package` call never reads it; `target_agent` is always hardcoded to
   `"claude-code"` by that call's own default. A string field implies adapter *selection*, which
   this ticket does not implement (out of scope — "the gate is the fix"). Renamed to
   **`delegation_enabled: bool`** — pure availability signal, no selection semantics, and it also
   removes the blank/invalid-string validation gap Codex raised (`bool(" ")` would have been a
   footgun under the string design).
3. **CP-05 / two other `tests/evaluation/harness/dataset.py` paths hard-assert
   `decomposition_assessed.strategy == "delegate"`** for explicit "Use Claude Code to ..." /
   "Write a function ..." messages (lines 210, 1291, 1592) — one of them (line 1592) also asserts
   `delegation_package_created` fired. These are deliberately testing the composition pipeline
   *once wired*, not the misroute this ticket fixes. Rather than degrade them to expect the
   fallback (which would stop them from ever exercising real package composition), add
   `AGENT_DELEGATION_ENABLED: "true"` to `docker-compose.eval.yml`'s two gateway services — the
   eval environment is where "delegation is wired" should actually be true. Production's default
   stays `false` (unset). No fixture content changes needed.
4. Additional test coverage per Codex's gap list: boundary-edge negatives (tester/testability/
   testers/subunit), an explicit assertion on the emitted `decomposition_assessed.reason`
   telemetry field (not just the returned value), explicit `delegation_enabled=False`/`True`
   monkeypatching in pipeline tests (no ambient-env reliance), and a resource-pressure-still-wins
   test with `delegation_enabled=True` set (proving precedence: the guard runs before the
   `_apply_matrix` call regardless of the flag).

## Changes

### 1. `src/personal_agent/request_gateway/intent.py`

- `_CODING_PATTERNS`: the `(?:(?:unit|integration)\s*test)` alternative becomes
  `r"|(?:\b(?:unit|integration)\s*tests?\b)"` — adds a leading boundary (blocks "subunit tests")
  and switches the trailing check from a bare substring to `tests?\b` (matches "test"/"tests",
  blocks "testing"/"tester"/"testability"/"testers").
- Replace the bare substring check (`any(kw in user_message.lower() for kw in _CODING_KEYWORDS)`)
  with a compiled `_CODING_KEYWORD_PATTERN`, built from the same `_CODING_KEYWORDS` tuple (single
  source of truth):

  ```python
  _CODING_KEYWORD_PATTERN: re.Pattern[str] = re.compile(
      r"(?i)(?:"
      + "|".join(
          rf"\b{re.escape(kw)}s?\b" if kw.endswith("test") else rf"\b{re.escape(kw)}\b"
          for kw in _CODING_KEYWORDS
      )
      + r")"
  )
  ```

  The `s?` (kept inside the boundary check, not as a separate lookahead) is what lets "unit
  test"/"unit tests" both match while "unit testing"/"unit tester" don't — the trailing `\b` is
  checked only after the optional "s" is consumed, so a word-char continuation right after either
  form fails the boundary.

- `classify_intent` rule 3 condition becomes:
  `if _CODING_PATTERNS.search(user_message) or _CODING_KEYWORD_PATTERN.search(user_message):`
  (drop `.lower()` — the pattern already carries `(?i)`).

### 2. `src/personal_agent/config/settings.py`

Add, after `expansion_budget_max` (~line 506):

```python
delegation_enabled: bool = Field(
    default=False,
    description=(
        "Whether the external delegation route (DecompositionStrategy.DELEGATE) is wired "
        "to a live adapter (see delegation/adapters/). False (default) means "
        "DELEGATION-classified requests fall back to SINGLE/HYBRID/DECOMPOSE by complexity "
        "instead of composing a DelegationPackage nothing can receive (FRE-1376)."
    ),
)
```

### 3. `src/personal_agent/request_gateway/decomposition.py`

- `assess_decomposition(intent, governance, delegation_enabled: bool = False)` — new keyword
  param, threaded into `_apply_matrix`.
- `_apply_matrix(task_type, complexity, delegation_enabled: bool)`.
- `case TaskType.DELEGATION:` becomes:

  ```python
  case TaskType.DELEGATION:
      if delegation_enabled:
          return DecompositionStrategy.DELEGATE, "delegation_route_external"
      match complexity:
          case Complexity.SIMPLE:
              return DecompositionStrategy.SINGLE, "delegation_no_target_fallback_single"
          case Complexity.MODERATE:
              return DecompositionStrategy.HYBRID, "delegation_no_target_fallback_hybrid"
          case _:
              return DecompositionStrategy.DECOMPOSE, "delegation_no_target_fallback_decompose"
  ```

- Docstrings updated to describe the gate.

The gate lives here (the strategy mapper), not in the classifier: `classify_intent` stays a pure
text→TaskType function; whether delegation is *possible* is a runtime/config fact the classifier
has no business knowing. `executor.py:4675` already keys `compose_delegation_package` off
`decomposition.strategy == DecompositionStrategy.DELEGATE` exclusively (verified — no other call
site), so gating the strategy is sufficient; no executor.py change needed.

### 4. `src/personal_agent/request_gateway/pipeline.py`

Stage 5 call becomes:

```python
decomposition = assess_decomposition(
    intent=intent,
    governance=governance,
    delegation_enabled=settings.delegation_enabled,
)
```

No new telemetry emission needed — the existing `decomposition_assessed` log (and the
`route_traces.decomposition_reason` column it feeds) already carries `reason`, and the new
`delegation_no_target_fallback_*` reason strings make the gate visible there (AC-2's "recorded
in telemetry").

### 5. `docker-compose.eval.yml`

Add `AGENT_DELEGATION_ENABLED: "true"` to both `seshat-gateway-control` and
`seshat-gateway-treatment` service environments, so the pre-existing delegation-composition eval
paths (CP-05, and the two others at dataset.py:1291/1592) keep validating real behavior instead
of silently going stale against the new safe-by-default production setting.

## Tests (TDD — failing first)

**`tests/personal_agent/request_gateway/test_intent.py`**
- `TestCodingKeywordWordBoundary` (AC-4): parametrized negatives — "The framework will implement
  these features automatically.", "Developers often implement their own caching layer.", "The
  module was refactored last week.", "We rely on unit testing throughout the codebase.", "Our unit
  tester caught the issue.", "We need to improve unit testability.", "The integration testers
  filed a report.", "The subunit tests passed." (leading-boundary edge case) — assert
  `task_type != TaskType.DELEGATION`. Parametrized positives — "Please implement the function as
  described.", "Please refactor this module.", "Add a unit test for this.", "Add unit tests for
  this." (plural) — assert `task_type == TaskType.DELEGATION` and `"coding_pattern" in signals`.
- `test_unit_tests_plural_still_matches`: "Can you look into why our unit tests keep failing and
  fix the flaky ones?" and "Write unit tests for the edge cases." — assert DELEGATION (regression
  guard for the eval-harness CP-24 / dataset.py assumption identified above).
- `test_fre1376_research_query_classifies_as_analysis` (AC-1): the verbatim 1,900-char fixture
  from session `5014ca54` — assert `task_type == TaskType.ANALYSIS`.

**`tests/personal_agent/request_gateway/test_decomposition.py`**
- Replace `TestDelegationAlwaysDelegate` with `TestDelegationGate`:
  - without `delegation_enabled` (default False) — SIMPLE→SINGLE/`delegation_no_target_fallback_single`,
    MODERATE→HYBRID/`..._hybrid`, COMPLEX→DECOMPOSE/`..._decompose`.
  - with `delegation_enabled=True` (AC-3) — SIMPLE and COMPLEX both →
    DELEGATE/`delegation_route_external`.
  - `test_expansion_denied_overrides_delegation_enabled`: `delegation_enabled=True` +
    `expansion_permitted=False` still → SINGLE/`expansion_denied` (precedence check — the
    resource-pressure guard runs before `_apply_matrix` regardless of the flag).

**`tests/personal_agent/request_gateway/test_pipeline.py`**
- Update `test_delegation_produces_delegate_strategy`: explicitly monkeypatch
  `get_settings().delegation_enabled = True` before the call (this test now exercises the AC-3
  positive path explicitly, since DELEGATE is no longer the unconditional default).
- Add `test_delegation_without_target_configured_falls_back` (AC-1 + AC-2, end to end through the
  real pipeline): explicitly monkeypatch `delegation_enabled = False` (no ambient-env reliance).
  The FRE-1376 fixture message → `intent.task_type == ANALYSIS`, `decomposition.strategy !=
  DELEGATE`. Add a second case using a genuine coding message (e.g. "Refactor the routing module")
  → `intent.task_type == DELEGATION`, `decomposition.strategy != DELEGATE`, `reason` starts with
  `"delegation_no_target_fallback"`.
- Add `test_decomposition_assessed_reason_telemetry_reflects_gate`: capture structlog output
  (mirroring `test_pipeline_emits_telemetry_events`) for both the `delegation_enabled=True` and
  `=False` cases, asserting the emitted `decomposition_assessed` event's `reason` field — not just
  the returned `DecompositionResult.reason` — matches (AC-2's "recorded in telemetry").

## Quality gates
`make test` · `make mypy` · `make ruff-check` + `make ruff-format` · `pre-commit run --all-files`.

## Diff class
Self-serve. Touches the pre-LLM classification/decomposition path (production, in the turn's call
chain) but is a pure deterministic function change with full unit coverage — not a destructive
path, no schema, no cost/governance code beyond routing. Re-assess at Step 6 if review surfaces
otherwise.
