# FRE-1332 — Grounding observability: give the compliance metric a denominator (ADR-0139 D1)

## Scope

Implements ADR-0139 D1 only. D2 (admissibility), D3 (`OBSERVED` entitlement) and D7
(near-miss resolution) are separate, later tickets (FRE-1334, FRE-1355) and are explicitly
out of scope. Neither `Entitlement.OBSERVED` nor `RegisteredSource.invocation_check_required`
exist in the codebase yet — code in this ticket must not reference them.

Round-5 amendment (FRE-1349) changes: `observed_span_outcomes` and
`invocation_checked_span_outcomes` are two independent fields (not one), and
`near_miss_markers` is a dict split by resolution status (all `"unresolved"` today, since D7
does not exist).

## Design

### 1. `src/personal_agent/grounding/source_registry.py` — offered/admitted counters

`SourceRegistry` gains one private counter and two read-only properties:

```python
self._tool_results_offered: int = 0
```

`register_tool_result` increments `_tool_results_offered` unconditionally on entry — every
call is one "offer" to the registry, regardless of outcome, **including a D4 retry that
resubmits the same tool call**: each call is a genuine offer even when `_register` later
dedupes its content against an earlier source.

```python
@property
def tool_results_offered(self) -> int: ...

@property
def tool_results_admitted(self) -> int:
    """Sources this turn's tool/documentation calls actually registered.

    Computed from ``self._sources`` rather than a second counter incremented on
    every ``ADMISSIBLE`` return: ``_register`` reuses an existing source's identifier
    when a D4 retry resubmits identical content (its dedupe key is ``(kind, origin,
    content)``), so a naive per-call counter would overcount a retried admission
    that produced no new source. Deriving the count from the registered-source
    collection is correct by construction and cannot drift from it — which is
    exactly AC-3's reconciliation claim, not a coincidence.
    """
    return sum(1 for s in self._sources if s.kind in (SourceKind.TOOL, SourceKind.DOCUMENTATION))
```

**Why the offered counter lives here, not in executor.py:** `register_tool_result` is the
single place that decides admissibility; counting anywhere else risks drifting from what
actually got offered.

### 2. `src/personal_agent/grounding/citations.py` — near-miss detector

```python
_NEAR_MISS_CANDIDATE_PATTERN = re.compile(r"\[[^\]]*@[^\]]*\]")

def count_near_miss_markers(text: str) -> int:
    """Count citation-shaped strings that fail CITATION_MARKER_PATTERN (ADR-0139 D1)."""
    return sum(
        1
        for candidate in _NEAR_MISS_CANDIDATE_PATTERN.findall(text)
        if not CITATION_MARKER_PATTERN.fullmatch(candidate)
    )
```

Deliberately narrow per the ADR: citation-shaped (bracketed, contains `@`), and only
"near" because it fails the real pattern. `[S1]` and ordinary bracketed prose contain no
`@`, so the candidate pattern never matches them — no separate exclusion needed. A
well-formed marker matches the candidate pattern **and** `fullmatch`s
`CITATION_MARKER_PATTERN`, so it is correctly excluded.

**Only `]` is excluded from the character classes, not `[`.** Codex's plan review caught
that excluding both brackets misses a malformed marker carrying a nested `[` before its
`@` or digest — `[S1@[0123]]` matches nothing under `[^\[\]]*`, since the class cannot
cross the inner `[` either, and the fabricated marker goes uncounted. Excluding only `]`
bounds each candidate to "the next `]` after this `[`", which still cannot cross a real
bracket pair's own boundary (so two adjacent well-formed markers `[foo] [S1@…]` are still
scanned as two independent candidates, not one run), while letting a stray `[` inside a
malformed one still land inside a single candidate and get compared against
`CITATION_MARKER_PATTERN`.

Docstring notes D7 (FRE-1355) will consume near-miss text for resolution; this ticket
only counts.

### 3. `src/personal_agent/grounding/verification.py` — turn evidence classification

```python
class TurnEvidenceClass(StrEnum):
    NO_ASSERTIONS = "no_assertions"
    UNCITABLE = "uncitable"
    CITABLE = "citable"


def classify_turn_evidence(
    verification: TurnVerification, *, tool_results_offered: int, tool_results_admitted: int
) -> TurnEvidenceClass:
    if not verification.spans:
        return TurnEvidenceClass.NO_ASSERTIONS
    if tool_results_offered > 0 and tool_results_admitted == 0:
        return TurnEvidenceClass.UNCITABLE
    return TurnEvidenceClass.CITABLE
```

Mirrors AC-1 / AC-2 literally. Only called when `verification.available` (see below) —
an unavailable turn has no meaningful span list to classify from.

### 4. `src/personal_agent/grounding/compliance.py` — AC-5's actual teeth

`is_unconfounded_observation` is FRE-1284/1285's single choke point: both the per-model
metric and enforcement selection read the window `GroundingComplianceRepository` builds
from what this function admits. Today it does not exclude `uncitable` turns, so a turn
where every tool result was refused is currently counted as a **failed** compliance
observation for the model — exactly the confound D1 exists to separate out. Fixed here,
not duplicated in each consumer:

```python
def is_unconfounded_observation(record: GroundingRecord, *, citable: bool) -> bool:
    return (
        record.available
        and record.non_exempt_count >= 1
        and not record.retrieval_forced
        and citable
    )
```

Single call site (`executor.py::_record_compliance_observation`) updated to pass
`citable=(turn_evidence_class is TurnEvidenceClass.CITABLE)`.

### 5. `src/personal_agent/orchestrator/executor.py` — wiring

In `_record_grounding`:

```python
registry = ctx.source_registry
tool_results_offered = registry.tool_results_offered if registry else 0
tool_results_admitted = registry.tool_results_admitted if registry else 0
turn_evidence_class = (
    classify_turn_evidence(
        verification,
        tool_results_offered=tool_results_offered,
        tool_results_admitted=tool_results_admitted,
    )
    if verification.available
    else None
)
near_miss_markers = (
    {"unresolved": count_near_miss_markers(ctx.final_reply)}
    if verification.available and ctx.final_reply
    else None
)
```

`observed_span_outcomes = {}` and `invocation_checked_span_outcomes = {}` (or `None` when
unavailable) — hardcoded empty with a one-line comment: neither `Entitlement.OBSERVED` nor
`invocation_check_required` exists yet, so no span can ever qualify; D2/D3 (FRE-1334)
populate these by construction once they land, no reshaping needed here.

Pass `turn_evidence_class` through to `_record_compliance_observation(ctx, record,
turn_evidence_class)`, which forwards `citable=...` to `is_unconfounded_observation`.

All six new fields added to the `log.info("grounding_verification_completed", ...)` call.

## Out of scope, deliberately

- `observed_span_outcomes` / `invocation_checked_span_outcomes` populated with real data —
  D2/D3 (FRE-1334).
- `near_miss_markers`'s `"resolved"` bucket — D7 (FRE-1355).
- Grafana panels/alert rules — FRE-1333.
- AC-3's live 50-turn ES reconciliation — verified post-deploy (runbook item); this ticket
  proves the invariant structurally in a unit test instead.

## Files touched

- `src/personal_agent/grounding/source_registry.py`
- `src/personal_agent/grounding/citations.py`
- `src/personal_agent/grounding/verification.py`
- `src/personal_agent/grounding/compliance.py`
- `src/personal_agent/orchestrator/executor.py`

## Tests (TDD — failing first)

- `tests/personal_agent/grounding/test_source_registry.py` — offered/admitted counters:
  a refused `bash` call increments offered only; an admitted `fetch_url` call increments
  offered and raises admitted by one; a **repeated identical `fetch_url` call** (the D4
  retry shape) increments offered again but leaves admitted unchanged, since `_register`
  reuses the existing source — the case Codex's plan review caught; the AC-3 structural
  invariant (`tool_results_admitted == count of TOOL/DOCUMENTATION kind sources`) over a
  mixed sequence of admits, a duplicate, and refusals.
- `tests/personal_agent/grounding/test_citations.py` — AC-4's four cases for
  `count_near_miss_markers`: fires on `[S@bash-tempo-trace-dba5b2]`; does not fire on
  `[S1@0123456789abcdef]` (well-formed), `[S1]`, or ordinary bracketed prose. Plus a
  regression case for the nested-bracket miss Codex's plan review caught: fires on
  `[S1@[0123]]`.
- `tests/personal_agent/grounding/test_verification.py` — `classify_turn_evidence`: AC-1
  (uncitable), AC-2 (weights-only turn is citable, not uncitable), no-assertions case.
- `tests/personal_agent/grounding/test_compliance.py` — `is_unconfounded_observation`
  excludes an uncitable-but-otherwise-eligible record; existing cases updated for the new
  required kwarg.
- `tests/personal_agent/orchestrator/test_executor_grounding.py` — end-to-end via
  `step_synthesis`, patching `executor.log` (FRE-552 pattern, see
  `test_frozen_reset_emit.py`) to assert the six new fields on the emitted
  `grounding_verification_completed` line for: AC-1's seeded uncitable turn, AC-2's
  weights-only turn, and the `verification.available is False` case (all six fields
  `None`).

## Commands

```
make test-file FILE=tests/personal_agent/grounding/test_source_registry.py
make test-file FILE=tests/personal_agent/grounding/test_citations.py
make test-file FILE=tests/personal_agent/grounding/test_verification.py
make test-file FILE=tests/personal_agent/grounding/test_compliance.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor_grounding.py
make test
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```
