# Plan — FRE-956: ADR-0124 Amendment B, conversation-only producer

**Backing:** ADR-0124 (Amendment B, 2026-07-24). Design spec:
`docs/superpowers/specs/2026-07-23-adr-0124-amendment-b-summarizer-conversation-only-design.md`.
Prior state: FRE-953 (Amendment A) shipped conversation text + tool metadata (name/status/error),
two correction tiers (`self_correction`, `status_contradiction`), and `self_correction` evidence
citable from either a tool error or the conversation.

**Revision note:** this plan went through one round of codex plan-review (adversarial, pre-code).
Section markers below (`[codex]`) call out where a finding changed the plan versus the first
draft. The most consequential finding: **the locator grammar for corrections is `assistant_text`
only, not `user_text | assistant_text`.** AC-11 says this explicitly ("the locator grammar names a
turn's assistant text only") and D3 confirms the surviving evidence is "the assistant's own
corrective text, which the user saw." This means **all 8** AC-12 positives need
assistant-text-only evidence citations, not just the 4 that were previously tool-backed.

## What Amendment B removes

1. `tool_evidence` from the `basis` enum — three bases remain: `user_statement`,
   `assistant_reasoning`, `mixed`.
2. `status_contradiction` from the correction tier — `self_correction` is the only kind.
3. **All** tool metadata (name/status/error) from the producer's prompt input — not just
   payloads. The prompt is user text + assistant text, all turns, fixed scaffolding, nothing else.
4. `self_correction`'s evidence source narrows to the **conversation only** — no more citing a
   `tool_result[N].error` field as evidence.
5. **[codex] Locator grammar narrows to `assistant_text` only** — not `user_text | assistant_text`
   as the first draft assumed. Per AC-11 (ADR:1014-1020) and the D3 consequential-changes table
   (ADR:595-596), both the `span`/`locator` (the correction claim) and `evidence_span`/
   `evidence_locator` (the supporting evidence) of a `self_correction` must resolve against a
   turn's `assistant_text`. `user_text` is no longer a valid locator target at all — it drops out
   of the grammar alongside the `tool_result[N]` targets. Practically: a self-correction whose
   supporting fact came from something the *user* said must have the assistant **restate that
   fact in its own reply** for it to be citable — consistent with the ADR's framing throughout
   ("what the user actually received... the assistant's own text").
6. Span+locator obligation narrows to `corrections` entries only (moot for other slots since no
   remaining basis ever obliged citation — that was always `tool_evidence`-only). **[codex:
   confirmed correct]** — the current `validate_digest_provenance` loop over
   established/decisions/unresolved only ever fires for `basis == "tool_evidence"`
   (`session_digest.py:337-348`); once that basis is unparseable the loop is dead code. No other
   basis value gates a citation requirement.

Tool invocation/success/failure **counts** remain as computed structured properties elsewhere
(unaffected — none currently exist as a computed property, so nothing regresses).

## Files to change

### 1. `src/personal_agent/memory/session_digest.py` (schema + validator)

- `BasisTag`: `Literal["user_statement", "assistant_reasoning", "mixed"]`.
- `CorrectionTier`: `Literal["self_correction"]`.
- Delete `_TOOL_FIELD_RE` and `_USER_FIELD`. **[codex] `resolve_locator` accepts only
  `assistant_text`** — `user_text` and any `tool_result[N]...` string both return `None` now
  (both are outside the grammar; the test suite should assert `user_text` itself no longer
  resolves, not just that tool fields don't).
- `validate_digest_provenance`: delete the `established/decisions/unresolved` loop entirely.
  Validate `corrections` only, unchanged internal logic (span/locator for the claim,
  evidence_span/evidence_locator for the support — both against `assistant_text`).
- Update every docstring/comment naming `tool_evidence`, `status_contradiction`, or `user_text` as
  a valid corrections locator (module docstring, the `CorrectionTier` comment block,
  `Locator.field`, `DigestItem.basis`, `Correction` class docstring,
  `validate_digest_provenance` docstring).

### 2. `src/personal_agent/second_brain/session_summary.py` (producer)

- Delete `_tool_block`.
- `_format_turn`: delete the `tool_results` rendering block and the `tools_used`
  declared-without-results block. The only note that can still fire is "no recorded assistant
  response." `_neutralise_delimiters` stays, applied to user/assistant text.
- **[codex] `_neutralise_delimiters`'s docstring is now stale** — it justifies the escaping via
  "a tool error line can still echo attacker-influenced content," but tool error no longer reaches
  the prompt at all. Rewrite the docstring's rationale around user/assistant text generally (a
  user message or an assistant response that itself echoes fetched/forwarded content, e.g. a
  pasted web page, is still attacker-influenceable — the function's *behavior* is unchanged, only
  its stated justification needs to track what's actually in scope now).
- **[codex — accepted, pre-existing tension, not fixed]** Byte-for-byte reconstruction (AC-8's
  "byte-reconstructible from the concatenation of those text fields... alone") is not literally
  true once `_neutralise_delimiters` mutates two specific literal substrings
  (`"--- Turn "` → `"--- turn "`, `"SOME EVIDENCE IS UNAVAILABLE"` → lowercased). This is a
  **pre-existing** tension shipped since FRE-947/953 (Amendment A already had this same
  neutralisation on user/assistant text; this ticket does not change that behavior). Removing the
  neutralisation to satisfy literal byte-identity would reopen the transcript-forgery risk it
  exists to close, which is out of scope and would be a regression, not a fix. **Resolution:**
  keep the neutralisation; keep the test suite's existing precedent of substring-containment
  checks on ordinary (non-forging) content, exactly as `test_input_is_never_silently_truncated`
  already does; add a strong **absence-of-metadata** proof instead of attempting literal
  byte-identity (see item 7 below); state this explicitly as a known, narrow, deliberate exception
  in the PR/ticket handoff so master isn't surprised by it.
- `build_prompt`'s evidence-notice trailer: replace "Corrections that rest on tool status, tool
  errors, or the session's own text remain legitimate." with "Corrections that rest on the
  session's own conversation text remain legitimate."
- **[codex]** `build_prompt`'s own docstring still describes "every tool invocation as metadata
  only — name, status, error" — rewrite to describe conversation-only input.
- `_parse_item`: basis tuple drops `"tool_evidence"`.
- `_parse_correction`: tier tuple becomes `("self_correction",)`.
- `_SYSTEM_PROMPT` rewrite:
  - `item` shape: `"basis": "user_statement" | "assistant_reasoning" | "mixed"`; drop the
    conditional `span`/`locator` lines from the `item` shape (never required outside corrections).
  - `correction` shape: `"tier": "self_correction"` (single value).
  - **[codex] Locator-grammar sentence: `field must be exactly one of: assistant_text, using the
    capture id shown in the transcript.`** — singular, not a `user_text | assistant_text` choice.
  - CORRECTIONS section: single kind, conversation-only, evidence explicitly framed as the
    assistant's own text:
    ```
    CORRECTIONS — precision above all. A missed error is recoverable from the raw evidence; a
    false error writes self-confirming state into memory. You are given only the conversation —
    no tool status, errors, or payloads. The only kind you may assert:
    - self_correction: the assistant corrected the record within the session. Cite the
      self-correction in span/locator and, in evidence_span/evidence_locator, the assistant's own
      supporting text — both must come from a turn's assistant response, never the user's message.
    ```
  - NEVER-assert paragraph: replace "...and NEVER assert a contradiction that would need a tool
    payload you were not given." with "...and NEVER assert a correction whose span or evidence
    would need to be cited from the user's own message — only the assistant's text is citable."
  - SAME-PROPOSITION test walkthrough: rewrite the JUDGMENT example so both sides are conversation
    statements (an earlier assistant claim vs. its own later correction), not "the tool reports
    what something IS." Keep JUDGMENT / APPROXIMATION / SCOPED structure.
- Module docstring at the top: update "What it reads (Amendment A)" to Amendment B language
  (conversation-only, zero tool metadata).

### 3. `tests/personal_agent/second_brain/test_session_summary.py`

- `_valid_output`: change the `established` item off `tool_evidence` — use
  `basis="assistant_reasoning"` or `"mixed"` with no span/locator.
- `test_prompt_input_completeness` → rewrite: assert full user/assistant text and capture ids
  present; assert **no** tool name, no `status=`, no tool error text, no "Tool invocations"
  header appears anywhere, even though captures carry `tool_results`.
- `test_tool_payload_and_arguments_absent_from_prompt` → extend to assert absence of name +
  status + error too (not just payload/arguments).
- Delete `test_tools_used_without_results_is_declared`.
- Rewrite `test_missing_evidence_notice_does_not_suppress_status_based_corrections` →
  `test_missing_evidence_notice_does_not_suppress_self_correction`.
- `test_unciteable_tool_evidence_fails_validation` → replace with
  `test_retired_tool_evidence_basis_fails_schema_validation` (`SCHEMA_INVALID` at parse time) and
  a new `test_uncitable_self_correction_fails_span_validation` (a `self_correction` whose
  `evidence_span` doesn't resolve → `SPAN_VALIDATION_FAILED`).
- **[codex] add `test_self_correction_evidence_from_user_text_is_rejected`** — a `self_correction`
  whose `evidence_locator.field == "user_text"` must fail `SPAN_VALIDATION_FAILED` (the direct
  regression test for the corrected locator grammar — this is the case the first plan draft
  missed).
- Rename `test_amendment_a_correction_tiers_parse_and_generate` →
  `test_self_correction_tier_parses_and_generates` (single tier, no parametrize).
- `test_legacy_correction_tier_letters_are_rejected` → parametrize
  `["A", "B", "status_contradiction"]`.
- **[codex] `test_forged_turn_delimiters_in_tool_error_are_neutralised` is now meaningless** (it
  forges via a tool error, which no longer reaches the prompt). Replace with
  `test_forged_turn_delimiters_in_user_message_are_neutralised` — hostile content in
  `user_message` (e.g. pasted content containing `"--- Turn 99 ..."`) must not create a fake turn
  boundary; same assertions (turn-header count, banner absence), same neutralisation mechanism,
  new (still-live) attack surface.

### 4. `tests/personal_agent/memory/test_session_digest_validator.py`

- `test_resolves_each_field_in_the_grammar`: drop the two `tool_result[...]` assertions **and**
  the `user_text` assertion — keep only `assistant_text`.
- **[codex]** `test_unresolvable_locators_return_none`: cover **both** `user_text` (now outside
  the grammar — this is the corrected-grammar regression case) and a `tool_result[0].output`
  locator (also outside the grammar), plus unknown capture id.
- Delete `test_tool_evidence_item_with_a_resolving_span_passes`,
  `test_non_tool_evidence_items_need_no_span`,
  `test_whitespace_differences_do_not_defeat_a_real_citation`, and the four `tool_evidence`
  AC-11-negative tests — replace with a single
  `test_no_slot_besides_corrections_requires_a_span` demonstrating all three remaining bases pass
  validation unconditionally for `established`/`decisions`/`unresolved` (no span, no locator).
- Re-add the four locator-grammar negative cases (absent / doesn't-resolve / wrong-field /
  elsewhere-in-session) against **corrections**, citing `assistant_text` only.
- Delete `test_status_contradiction_requires_both_spans_to_resolve` and
  `test_status_contradiction_fails_when_the_evidence_span_does_not_resolve`.
  `test_correction_is_checked_regardless_of_basis_tag` stays, but its `evidence_locator` must move
  from `assistant_text` (already is, check) — confirm it doesn't accidentally cite `user_text`.
- **[codex]** Update the module/test docstrings at the top (lines ~1-11) and around the AC-12
  fixture pre-validation test (~325-338) that still say "a tool error or the conversation" —
  Amendment B is conversation (assistant text) only.
- `test_ac12_positive_fixtures_have_a_resolving_reference_citation`: no code change — picks up
  the rebuilt fixture where all 8 positives cite `assistant_text` only.

### 5. `tests/fixtures/session_digest/build_fixtures.py` (rebuild)

- **`ac8()`**: keep the three existing cases; add a 4th, `user_typed_tool_name` (a user message
  that types a tool name in prose), proving a user-typed name passes through. **[codex]** Also add
  distinctive canary tokens to each case's tool metadata (name/error, not just output/arguments)
  that do not otherwise overlap with any conversation text in that case — this is what makes the
  strengthened absence-check (item 7 below) meaningful rather than vacuous.
- **`ac10()`**: drop the `tool_evidence` spec row; redefine `mixed` as a conversation-only
  combination (assistant blends what the user said with its own reasoning — no tool output).
  Grow topics from 10 to 14 (14 × 3 = 42 items, ≥8 per basis; criterion: ≥40 total, ≥8 each).
  Rebalance session chunking (~9 sessions).
- **`ac12()`**: **[codex-corrected]** rebuild **all 8** positives (not just b1–b4) so both the
  correction's `span`/`locator` and its `evidence_span`/`evidence_locator` resolve against
  `assistant_text`:
  - `b1`–`b4` (previously tool-error-backed): the assistant's own later message narrates the
    tool's error in its own words as part of its self-correction — e.g. t2 assistant: "Correcting
    myself — I re-read the output, which said 'relation sessions already exists', so the migration
    did not apply." `span` = the correction clause, `evidence_span` = the quoted error text,
    both citing `(sid-t2, assistant_text)` (different substrings of the same message — legal per
    `_check_located_span`, confirmed by codex against the current implementation).
  - `b5`–`b8` (previously user-text-backed): restructure so the assistant's own corrective
    message **restates** the user-supplied correcting fact in its own words, and both spans cite
    that restatement — e.g. t2 assistant: "You are right — since the env file shows
    `AGENT_SERVICE_PORT=9001`, I was wrong: it listens on 9001, not 9000." Both `span` and
    `evidence_span` resolve against `(sid-t2, assistant_text)`.
  - Update the `"amendment"` field string, the `_self_correction` helper's docstring, and the
    `evidence_field` parameter shape (drop the tool/user distinction — every case now supplies an
    assistant-text evidence field) to Amendment B language.
- **`ac13()`**: drop the `status_visible` case — fixture becomes a **pair**
  (`payload_absent`, `self_correction`). The existing `self_correction` case's t2 assistant text
  ("I was wrong — the deployment config sets it to 9001.") already self-contains both a claim and
  supporting clause in one assistant message, so it needs no further rework for the corrected
  grammar — confirm this reads naturally as a citable assistant-text pair before finalizing.
  Update `"threshold"` text and docstring.
- Re-run `uv run python tests/fixtures/session_digest/build_fixtures.py` to regenerate the
  committed JSON.

### 6. `tests/fixtures/session_digest/REGISTRY.md`

Update `ac8`/`ac10`/`ac12`/`ac13` sections: AC-8's new positive-control case + canary tokens;
AC-10 un-deferred (42 items / 3 bases / 14 each); AC-12's full 8-positive assistant-text-only
evidence rebuild; AC-13 reduced to a pair.

### 7. `scripts/eval/session_digest_eval.py`

- Module docstring: update scope to Amendment B; **[codex]** explicitly state the default-run
  cost impact of un-deferring AC-10 — roughly 9 additional paid generation calls per invocation
  (on top of AC-12's 20 + AC-13's 2), so an operator sees the bound before running without a new
  CLI flag.
- `_DEFERRED`: empty set.
- `score_ac8`: **[codex-strengthened]** rather than only checking name/status/error absence with
  ad hoc exceptions, probe for the canary tokens seeded in the rebuilt fixture (item 5) and assert
  none appear in the prompt — plus keep the existing completeness assertions and the
  `"Tool invocations"` header-absence check. This is presented explicitly as an absence-of-metadata
  proof, not a byte-identity proof (see the accepted pre-existing tension noted in file 2).

### 8. New: `scripts/verify_adr0124_amendment_b_no_retired_values.py` (the Amendment B verification seam)

**[codex: legitimately in scope, not the verification oracle — but underspecified in draft 1;
tightened here.]** A small, read-only, standalone script, mirroring `scripts/eval/`'s style.

- Pure function `scan_digests(raw_digest_json: Sequence[str]) -> RetiredValueScan` (dataclass:
  `population`, `tool_evidence_count`, `status_contradiction_count`). Unit-testable with no DB.
- **[codex] Testable query path:** structure the DB-facing half as
  `async def run_scan(driver, deploy_ts, *, allow_empty) -> RetiredValueScan`, where `driver` is
  anything exposing the neo4j `AsyncDriver`-shaped `.session()` async context manager — this lets
  tests inject a small fake/stub driver (fake session + fake result whose async iteration yields
  fake records supporting `record["digest"]`) instead of depending on `MemoryService`'s internal
  `.driver` attribute. `main()` wires argparse + a real `MemoryService().connect()` + `run_scan`.
- Cypher:
  ```cypher
  MATCH (s:Session)
  WHERE s.summary_generated_at IS NOT NULL
    AND s.summary_generated_at > $deploy_ts
    AND s.session_digest IS NOT NULL
  RETURN s.session_digest AS digest
  ```
- CLI: `--deploy-timestamp` (required, ISO-8601), `--allow-empty` (default off — an empty
  population fails loudly, since that would let the scan pass vacuously; this flag exists only for
  the script's own tests/dry-runs, never for the real post-deploy check).
- **[codex] Tests to add** (`tests/scripts/test_verify_adr0124_amendment_b_no_retired_values.py`):
  `scan_digests` unit tests (empty / clean / each retired value once); **and** `run_scan`
  integration-style tests against the fake driver covering: argument validation (missing
  `--deploy-timestamp` fails argparse), empty-population exit behavior (fails unless
  `--allow-empty`), a clean non-empty population (exit 0), and a population carrying a retired
  value (exit 1) — exercising the actual query-and-parse path, not just the pure scanner.

This is what master runs post-deploy as the Amendment B verification runbook step (documented in
the Step-9 ticket comment), once real post-deploy digests exist or a synthetic supplement is
loaded per the ADR's corpus-feasibility fallback (not built here).

## Explicitly not doing (ticket scope)

- Not building the verification oracle or a tool-error flag (rejected in Amendment B).
- Not touching Amendment A's payload removal or conversation-scope input (kept).
- Not changing `memory/models.py` (confirmed: only imports `SessionDigest`, no separate enum).
- Not adding computed tool-invocation/success-failure-count properties (none exist yet to
  regress; not this ticket's ask).
- Not removing `_neutralise_delimiters` or otherwise chasing literal byte-identity for AC-8 (see
  the accepted pre-existing tension above) — that would reopen a closed security surface.
- Not adding a `--confirm-paid` flag to the eval harness — documenting the cost bound in the
  docstring is judged proportionate for a manually-invoked operator script.

## Acceptance-criteria mapping (what proves each one)

- **AC-8**: rewritten `test_prompt_input_completeness` + extended
  `test_tool_payload_and_arguments_absent_from_prompt`; `score_ac8` (canary-token absence proof)
  against the rebuilt fixture.
- **AC-10**: rebuilt `ac10_basis_labelling.json` (42 items / 3 bases / 14 each) + `score_ac10`
  un-deferred.
- **AC-11**: rewritten `test_session_digest_validator.py` — locator grammar is `assistant_text`
  only; span+locator obligation on corrections only; explicit regression test that `user_text` no
  longer resolves and that citing it in a correction fails validation.
- **AC-12**: rebuilt `ac12_corrections.json` (8 self_correction positives, ALL with
  assistant-text-only evidence; 12 Tier-C negatives unchanged) + `score_ac12` (unchanged scoring
  logic).
- **AC-13**: rebuilt `ac13_missing_evidence.json` (pair, `status_visible` removed) + `score_ac13`.
- **Amendment B verification**: new `scripts/verify_adr0124_amendment_b_no_retired_values.py` +
  its tests (pure-scanner + fake-driver query-path tests); schema-level regression tests
  (`test_legacy_correction_tier_letters_are_rejected` parametrized with `status_contradiction`,
  `test_retired_tool_evidence_basis_fails_schema_validation`,
  `test_self_correction_evidence_from_user_text_is_rejected`) prove the producer can never emit or
  accept a retired value/grammar even before any live population exists.

## Verification commands

```bash
make test-file FILE=tests/personal_agent/second_brain/test_session_summary.py
make test-file FILE=tests/personal_agent/memory/test_session_digest_validator.py
make test-file FILE=tests/scripts/test_verify_adr0124_amendment_b_no_retired_values.py
uv run python tests/fixtures/session_digest/build_fixtures.py   # regenerate fixture JSON
uv run python scripts/eval/session_digest_eval.py --dry-run     # AC-8 offline, no spend
make test        # full suite
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Risk tier

Standard/Complex — touches `src/` producer logic and the schema/enum, implements a new ADR
amendment. Codex plan-review completed (findings folded in above); proceeding to TDD
implementation.
