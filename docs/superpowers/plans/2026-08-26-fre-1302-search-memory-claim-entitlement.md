# FRE-1302 — search_memory's pulled Claims get EXTERNAL entitlement regardless of asserted_by

**Ticket:** https://linear.app/frenchforest/issue/FRE-1302
**Backing ADR:** ADR-0138 D2 (admissible sources, independence) — `Entitlement`/`_entitlement_of`
(source_registry.py) is D2's authorship layer, added under FRE-1282/FRE-1299; not named in the ADR
text itself.
**Sibling fix:** FRE-1299 (b216db18) threaded `asserted_by` through the *push* path
(`register_memory_item`). This ticket is the *pull* path (`search_memory` → `register_tool_result`).

## The bug

`query_claims` (memory/service.py) — `search_memory`'s only read surface for Claims (ADR-0126 D4,
pull-only) — doesn't select `cl.asserted_by` in its Cypher RETURN, so the field never reaches the
tool result. Separately, `register_tool_result` classifies `search_memory` as `SourceKind.TOOL` and
hardcodes `entitlement=Entitlement.EXTERNAL` — the most-trusted tier — for every call, regardless of
content. Net effect: an agent-derived Claim, surfaced via `search_memory`, is registered as more
trusted than an unlabelled Stance ever is (`AGENT_DERIVED`, the default-deny floor). `verification.py`
only denies `AGENT_DERIVED` sources (`SOURCE_NOT_ENTITLED`), so this is a live citation-gate bypass,
not a cosmetic gap.

`query_claims_history` (the `include_history=True` sibling pull, ADR-0126 D5) has the identical gap
and feeds the *same* `search_memory` tool result / same registered source — fixed alongside so the
one registration this PR touches doesn't end up half-fixed.

## Architectural constraint that shapes the fix

`search_memory` registers as **one source per call** (FRE-1280): matched_turns, entities, `claims`,
and (on demand) `claims_history` are one JSON blob, one identifier, one entitlement
(`orchestrator/executor.py` — `_register_tool_source` → one `register_tool_result` call, one
`_with_citation_marker` splice). There is no per-item entitlement in this architecture today, and
building one is exactly the "own scoping pass" the ticket flags as a design call, not this fix's job.

Given that constraint, the fix aggregates to the **most restrictive** entitlement among the Claim
rows actually present in the result: `EXTERNAL` when no Claim rows are present at all (turns/entities
-only results are unaffected — that gap is real but out of scope, matching the ticket's own
boundary), `USER_STATED` only when every returned Claim is user-asserted, `AGENT_DERIVED` otherwise.
This can over-deny a call that also returned legitimately-external turns/entities alongside one
agent-derived Claim — accepted deliberately: it fails in the same safe direction as every other
default-deny rule in this module (`_entitlement_of`'s own docstring: "fails in the only safe
direction"), and splitting registration to avoid it is the larger redesign the ticket defers.

## Changes

### 1. `src/personal_agent/memory/service.py`

- `query_claims` (~line 2853): add `cl.asserted_by AS asserted_by` to the RETURN clause; add
  `"asserted_by": "user" if row["asserted_by"] == "user" else "agent"` to the appended dict —
  the exact canonicalization `query_current_stances` already uses (FRE-1299, line ~3036): a legacy
  row with no property at all reads back `"agent"`, not unclassified. Update the Returns docstring.
- `query_claims_history` (~line 3098): same Cypher addition; same canonicalization added to the
  `chain` dict comprehension (~line 3166). Update the Returns docstring.

### 2. `src/personal_agent/grounding/source_registry.py`

- New module-level constant `_CLAIM_LIST_KEYS = ("claims", "claims_history")` — the two
  `search_memory` result keys carrying Claim rows.
- New function `_search_memory_entitlement(content: str) -> Entitlement`:
  - Parse `content` as JSON; any parse failure or non-dict → `Entitlement.EXTERNAL` (preserves
    current behaviour when the shape is atypical, same fallback posture `_strip_argument_echo`
    already uses).
  - Collect every item under `_CLAIM_LIST_KEYS` present as a list.
  - No Claim rows → `Entitlement.EXTERNAL` (turns/entities-only result, unaffected by this fix).
  - Reuse `_entitlement_of` per Claim row (same function `register_memory_item` already calls —
    one definition of "user-asserted", not a second one). All rows `USER_STATED` →
    `Entitlement.USER_STATED`; otherwise `Entitlement.AGENT_DERIVED`.
- `register_tool_result`: where it currently hardcodes `entitlement=Entitlement.EXTERNAL` in the
  admissible-source registration, branch on `tool_name == "search_memory"` and call
  `_search_memory_entitlement(admissible)` (the post-echo-strip content — same text the registered
  source's `content` field holds, so entitlement and containment reason about the same bytes).
  Every other `TYPED_RETRIEVAL_TOOLS` member keeps the existing hardcoded `EXTERNAL`.

### 3. Tests

- `tests/personal_agent/memory/test_query_claims.py`: `_claim_row` helper gains an
  `asserted_by: str = "agent"` parameter (default preserves existing tests' intent — they're
  about ranking/filtering, not authorship); `test_result_shape_is_claim_specific`'s exact key-set
  assertion gets `"asserted_by"` added (characterization oracle, recalibrated). New test: a row with
  no `asserted_by` key at all canonicalizes to `"agent"` (mirrors the stance legacy-edge test).
- `tests/personal_agent/memory/test_claims_stance_cypher.py` (or wherever `query_claims_history`'s
  Cypher shape is pinned — confirm during implementation): same RETURN-clause assertion pattern as
  `test_assert_claim_persists_and_reads_back_authorship`'s `"cl.asserted_by AS asserted_by" in
  fetch_cypher`, applied to `query_claims_history`.
- `tests/personal_agent/grounding/test_source_registry.py`: unit tests for
  `_search_memory_entitlement` / `register_tool_result(tool_name="search_memory", ...)` —
  no-claims content stays `EXTERNAL`; all-user-asserted claims → `USER_STATED`; any agent-derived
  or unlabelled claim → `AGENT_DERIVED`; a claim under `claims_history` alone triggers the same
  denial (proves the sibling path isn't a bypass of the fix).
- New `tests/personal_agent/grounding/test_search_memory_entitlement_e2e.py` (mirrors
  `test_stance_entitlement_e2e.py`'s shape): `query_claims`-style row → `register_tool_result` →
  `verify_turn`, proving an agent-derived Claim surfaced via `search_memory` is denied
  (`SOURCE_NOT_ENTITLED`) exactly like any other agent-derived source — the ticket's AC-3, and the
  regression proof that this was a real bypass, not merely a missing field.

## Explicitly out of scope (noted in the handoff, not filed as a ticket)

- Per-item entitlement within one `search_memory` call (would need multiple identifiers per tool
  result — an architecture change, not this fix).
- `matched_turns`/`entities` authorship (they carry no `asserted_by` at all today; same shape of
  gap, but the ticket scopes this fix to Claims).

## Verification

- `make test-file FILE=tests/personal_agent/memory/test_query_claims.py`
- `make test-file FILE=tests/personal_agent/grounding/test_source_registry.py`
- `make test-file FILE=tests/personal_agent/grounding/test_search_memory_entitlement_e2e.py`
- `make mypy` / `make ruff-check` / `make ruff-format` / `pre-commit run --all-files`
