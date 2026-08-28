# FRE-1135 — Delete dead Stage-6 assembly outputs; stop false session-fact admission

**Ticket:** https://linear.app/frenchforest/issue/FRE-1135
**Backing finding:** FRE-1131 audit §F2 (`docs/research/2026-08-03-fre-1131-context-memory-alignment-audit.md`); ADR-0125 D3.
**Backing ADR being retired:** ADR-0037 (recall controller).

## Finding recap

The executor rebuilds its message list from `SessionManager` and never reads
`gw.context.messages`. Three Stage-6 outputs are built, budgeted, and discarded:
the state document, the "Session Fact Recall" system message, and the recall
controller (Stage 4b) that produces the candidates for that message. Worse,
`session_facts_injected=True` — stamped about the *discarded* list — is forwarded
into the turn-evidence record, where `_resolve_admission` marks
`SESSION_FACT_SECTION` candidates **admitted** purely on that stamp, never checking
`wire_messages`. This is a false-admission path by construction (currently latent:
zero `session_fact` items in the last 300 evidence-bearing captures).

Directive: delete, don't wire. The cold tier (FRE-465/FRE-1134) is the correct home
for "reach facts beyond the window" — wiring the executor to `gw.context.messages`
would build a second, worse cold tier.

## Scope

1. Delete the dead assembly outputs: state document injection, Session Fact Recall
   block, the recall controller (Stage 4b) and its `full_session_messages` plumbing.
2. Remove `session_facts_injected` (the assembler's stamp) end to end. Remove the
   `CandidateSource.SESSION_FACT_SECTION` enum member and its `_resolve_admission`
   branch. Every remaining admission claim is derived only from `wire_messages` /
   `rendered_identities` / `block_reached_input` — never from a bare stamp.
3. Add a contract test proving admission for the (now sole) `MEMORY_CONTEXT` source
   still requires the identity to actually be present in the rendered/wire form —
   i.e. there is no remaining path from "a producer says it wrote something" to
   `admitted=True` without a wire-form check.

## Files touched

### Delete outright
- `src/personal_agent/request_gateway/state_document.py` (only caller is being removed)
- `src/personal_agent/request_gateway/recall_controller.py`
- `tests/personal_agent/request_gateway/test_state_document.py`
- `tests/personal_agent/request_gateway/test_recall_controller.py`

### `src/personal_agent/request_gateway/context.py`
- Drop `from personal_agent.request_gateway.state_document import build_state_document`
  and the state-doc prepend block in `assemble_context`.
- Drop `_session_fact_candidates()` entirely.
- Drop the `recall_context` parameter from `assemble_context()`, the "Session Fact
  Recall" system-message block, and the `session_facts_injected` computation.
- Drop `*_session_fact_candidates(recall_context),` from the `recall_candidates` tuple.
- Drop now-unused imports: `CandidateSource`, `MemoryItemKind` (from
  `captains_log.turn_evidence`), `RecallResult` (from `.types`).

### `src/personal_agent/request_gateway/pipeline.py`
- Drop `full_session_messages` parameter (and its docstring entry).
- Drop the Stage 4b block: `run_recall_controller` call + the `IntentResult`
  reclassification-to-`MEMORY_RECALL` branch.
- Drop `from personal_agent.request_gateway.recall_controller import run_recall_controller`.
- Drop `recall_context=recall_result` from both the `assemble_context()` call and the
  `GatewayOutput(...)` construction.
- Drop now-unused `IntentResult` import (from `.types`); `TaskType` stays (still used
  at the `degraded_stages` check).

### `src/personal_agent/request_gateway/types.py`
- Delete `RecallCandidate` and `RecallResult` dataclasses.
- Delete `session_facts_injected` field from `AssembledContext` (+ docstring entry).
- Delete `recall_context` field from `GatewayOutput` (+ docstring entry).

### `src/personal_agent/service/app.py`
- Drop `full_session_messages=db_messages` from both `run_gateway_pipeline()` call
  sites (`chat_stream` and the other endpoint). `db_messages` itself stays — it still
  feeds `prior_messages`.

### `src/personal_agent/orchestrator/executor.py`
- Drop the `session_facts_injected=(...)` kwarg passed to `build_turn_evidence` in
  `_record_turn_evidence`.

### `src/personal_agent/request_gateway/budget.py`
- Drop `session_facts_injected=context.session_facts_injected,` from the
  `AssembledContext(...)` reconstruction in `apply_budget`.

### `src/personal_agent/captains_log/turn_evidence.py`
- Delete `CandidateSource.SESSION_FACT_SECTION` (leaves `MEMORY_CONTEXT` as the sole
  member; trim the class docstring to match).
- Delete the `session_facts_injected` parameter from `_resolve_admission()` and its
  `if candidate.source is CandidateSource.SESSION_FACT_SECTION:` branch — the
  remaining branches (`not memory_context_present` / `rendered_budget[...] <= 0` /
  `not block_reached_input` / else-admit) already ground every admission decision in
  the wire form.
- Delete the `session_facts_injected` parameter from `build_turn_evidence()` and its
  pass-through call into `_resolve_admission`.
- **Keep** `MemoryItemKind.SESSION_FACT` and the `declared == "session_fact"` branch in
  `memory_item_identity()` untouched (codex plan-review finding). `RecalledMemoryRecord.kind`
  is a pydantic-validated `MemoryItemKind` field persisted into `TaskCapture` records
  (`captains_log/capture.py`); `_scan_captures()` reconstructs historical records with
  `TaskCapture(**data)`, and any past record holding `"kind": "session_fact"` would fail
  validation and be silently discarded if the enum member were removed. The ticket's
  "zero in the last 300 evidence-bearing captures" measurement doesn't establish the whole
  retained corpus is empty, so this stays for read-compatibility even though nothing
  produces it going forward.

### Tests to update
- `tests/personal_agent/request_gateway/test_recall_candidates.py`: delete
  `test_session_fact_candidates_are_recorded` and the now-unused `RecallCandidate`,
  `RecallResult` imports.
- `tests/personal_agent/captains_log/test_turn_evidence.py`: delete the
  `TestSessionFactCandidates` class (three tests — the middle one,
  `test_injected_session_facts_are_admitted`, is literally today's false-admission bug
  written down as a passing test). Drop `session_facts_injected` from `_evidence()`'s
  signature and its `build_turn_evidence(...)` call.
- `tests/personal_agent/captains_log/test_turn_evidence_identity.py`: drop the
  `"session_facts_injected": False,` line from `_build()`'s kwargs.
- `tests/personal_agent/memory/test_adr_0126_topic_scoped_stance_push.py` and
  `test_adr_0126_behavioural_stance_profile.py`: drop
  `session_facts_injected=result.session_facts_injected,` from their `_run_turn`
  helpers' `build_turn_evidence(...)` calls.
- `tests/scripts/test_check_evidence_truncation.py`: delete
  `test_real_state_document_module_is_clean_post_fix` (reads the now-deleted file from
  disk). The two synthetic-fixture Rule-C tests
  (`test_whole_file_scope_catches_generically_named_local`,
  `test_whole_file_scope_does_not_apply_to_other_files`) are untouched — they build
  their own `tmp_path` fixtures and don't depend on the real file existing.
- New: `tests/personal_agent/captains_log/test_turn_evidence.py` — add a contract test
  (AC-3 below), **parametrized over `list(CandidateSource)`** (codex plan-review
  finding: a test hardcoded to `MEMORY_CONTEXT` proves nothing about a future source
  reintroducing a stamp-based shortcut) proving a candidate of any source whose
  identity never rendered, or whose block never reached the wire form, is never
  admitted — the general shape of the invariant the `SESSION_FACT_SECTION` branch
  violated. Because the enum has exactly one member post-deletion, this is not
  redundant busywork today, but it means a future `CandidateSource` addition inherits
  the guarantee automatically instead of needing its own opt-in test.

### Docs (directly invalidated by this change, not scope creep)
- `docs/architecture_decisions/ADR-0037-recall-controller.md`: flip `**Status:**` from
  `Accepted` to `Superseded — by FRE-1135 (implementation deleted as dead code; ADR-0037's
  "reach facts beyond the window" purpose is now FRE-465/FRE-1134's cold tier)`, and add
  a matching "Document Status" footer line (repo convention, see ADR-0008).

## NOT in scope (flagged for the handoff, not touched here)

- `scripts/check_evidence_truncation.py`'s `WHOLE_FILE_SCOPE_SUFFIXES` ("Rule C") now
  points at a deleted file and can never match again. It's inert, not broken —
  `whole_file` just always evaluates `False`. Removing it cleanly means threading
  `whole_file` out of `_EvidenceTruncationVisitor` too; that's a separate guard-script
  cleanup, not part of this ticket's evidence-contract fix. Left as-is; noted in the
  handoff.
- ADR-0059 Context Quality Stream: `recall_controller.py`'s D3 compaction-quality check
  (the block using `get_dropped_entities` / `CompactionQualityIncident`) was the *only*
  producer of `CompactionQualityIncidentEvent` in the codebase. Deleting the controller
  makes `app.py`'s subscription to `STREAM_CONTEXT_COMPACTION_QUALITY_POOR` permanently
  a no-op (it was already written to tolerate "no events published"). Not touched here —
  removing a whole ADR-0059 surface is a separate, larger concern than this ticket's
  Stage-6 scope. Noted in the handoff for the owner to decide whether it deserves its
  own ticket.
- `config/settings.py`'s `context_quality_stream_enabled` field docstring names
  `request_gateway.recall_controller` as the (soon nonexistent) dual-write producer.
  Left as a one-line accuracy note in the handoff rather than editing — the setting
  still gates a live subscription path in `app.py`, so it isn't dead config, just a
  stale docstring pointing at deleted code.

## Acceptance criteria (testable, from the ticket + this ticket's own risk)

- **AC-1 (dead surfaces gone):** `grep -rn "build_state_document\|run_recall_controller\|full_session_messages" src/personal_agent` returns nothing. `state_document.py` and `recall_controller.py` no longer exist.
- **AC-2 (no assembler-stamp admission path):** `grep -n "session_facts_injected\|SESSION_FACT_SECTION" src/personal_agent/captains_log/turn_evidence.py src/personal_agent/request_gateway/types.py` returns nothing.
- **AC-3 (admission is wire-form-grounded, by test):** a new test, parametrized over
  `list(CandidateSource)`, asserts that a `RecallCandidateRecord` of any source is
  `admitted=False` whenever its identity is absent from `rendered_identities` OR
  `block_reached_input` is `False` — i.e., no code path can flip `admitted=True` from a
  flag alone, for any source that exists now or is added later.
- **AC-4 (behavior-preserving elsewhere):** `make test` passes; `gw.context.memory_context` / `gw.decomposition.strategy` consumers in `executor.py` are untouched — this ticket does not change memory-recall or decomposition routing, only removes the dead recall-controller reclassification path that fed them on the 5 historical occurrences the ticket cites as going nowhere.
- **AC-5 (docs updated):** ADR-0037's Status header no longer reads `Accepted`.

## Risk / "it fails if" check

The ticket's failure condition is removing "the *only* full-history read path" without
FRE-1134 confirming the cold tier owns that job — but the ticket's own sequencing note
says the controller deletion "may land with or after FRE-465's approval" and "need not
wait." Nothing here builds a replacement full-history reader; this is pure deletion of
inert code, consistent with the ticket's "delete, don't wire" directive.

## Test plan

1. Write `AC-3`'s contract test first (TDD) — confirm it fails against current code
   only in the sense that the vulnerable branch exists (it will pass trivially before
   the branch is removed, since it targets `MEMORY_CONTEXT` which was already
   wire-grounded — the real proof is that `TestSessionFactCandidates`'s
   `test_injected_session_facts_are_admitted` is a *demonstration* of the bug, which
   this PR deletes rather than fixes-in-place, because deleting the source removes the
   bug's precondition entirely).
2. Delete files/blocks per the file list above.
3. `make test` — full suite.
4. `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.
