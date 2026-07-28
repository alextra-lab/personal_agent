# FRE-1003 — Remove the reflection-recall path entirely; retire dead reflection-producer prompt inputs

**Backing:** ADR-0125 D2 (supersedes ADR-0067), AC-2. Parent: FRE-999.
**Risk tier:** Standard (touches `src/` logic: context assembly, settings, a DSPy signature; deletes a module) → codex plan-review required.

## Scope (from the ticket)

1. Remove the reflection-recall **call site** in context assembly and the **recall module** itself — not merely the config default.
2. Remove the now-orphaned settings entries.
3. Fold in two supporting fixes to the reflection producer (from the ADR-0125 audit):
   - drop the trace identifier from the `GenerateReflection` DSPy signature's input fields.
   - fix the 200-char clip of the user message on the manual fallback path.
4. Do **not** otherwise touch the reflection producer (its 5 infra focus areas and `reply_length` input are correctly scoped — owner already corrected an earlier misreading of this).

## Pre-check finding

The second supporting fix (200-char clip on the manual fallback path) is **already done** — commit `0dd5617b` (FRE-1002, ADR-0125 D5/AC-5 guard, merged same day) replaced the silent `user_message[:200]`/`[:400]` slices in both `reflection.py`'s manual-prompt path (line 414) and `reflection_dspy.py`'s DSPy path (line 435) with `mark_truncated(user_message, 400)`. Verified via `git blame` — both lines carry commit `0dd5617b`. **No action needed for that item**; only the trace-identifier removal remains.

## Files touched

| File | Change |
|---|---|
| `src/personal_agent/captains_log/recall.py` | **Delete entirely** (the recall module) |
| `tests/personal_agent/captains_log/test_recall.py` | **Delete entirely** (tests the deleted module) |
| `src/personal_agent/request_gateway/context.py` | Remove the `if settings.reflection_recall_enabled:` block (lines ~372-391) — the call site + the in-function import of `captains_log.recall` |
| `src/personal_agent/config/settings.py` | Remove the 4 `reflection_recall_*` `Field(...)` declarations (lines ~2310-2352) |
| `.env.example` | Remove the `AGENT_REFLECTION_RECALL_*` documented block (lines 878-899) |
| `docs/reference/CONFIG_INVENTORY.md` | Remove the 4 `reflection_recall_*` rows |
| `src/personal_agent/captains_log/reflection_dspy.py` | Remove `trace_id: str = dspy.InputField(...)` from `GenerateReflection`; remove `trace_id=trace_id,` from the `reflection_generator(...)` call. Keep the `trace_id` function parameter (used for logging + `TelemetryRef`) |
| `scripts/study/baseline_harness.py` | Docstring in `_capitalized_entity_hints` cites `captains_log/recall.py` as duplication precedent — update to avoid a dangling reference to a deleted file |
| `tests/personal_agent/request_gateway/test_context.py` | Add AC-2 proof tests (see below) |

Not touched (deliberately): `ADR-0067-reflection-surfacing-in-context-assembly.md` status header — ADR-0125's own Implementation Notes assign that flip to master at the gate, since a header edit proves nothing about behaviour (this ticket's AC-2 is the behavioural proof).

## Step-by-step

**Revision (codex plan-review, 2026-07-27):** the original step 1 design for the "marker never
leaks" proof was flagged **blocking**-vacuous — it had no seeded fixture, so it could pass before
the fix for the wrong reason (the live ES-backed query returning `[]`, not the invariant holding).
It also proposed monkeypatching `os.environ`, which does not affect the already-constructed
`settings` singleton `context.py` reads (`get_settings()` returns a module-level object built once).
Revised design below fixes both.

1. **Red-phase characterization (temporary, not committed).** Before touching production code, add a
   throwaway test to `tests/personal_agent/request_gateway/test_context.py` that proves the leak is
   real and my harness can detect it:
   - Monkeypatch `ctx_module.settings.reflection_recall_enabled = True` (object attribute, not env var).
   - Monkeypatch `"personal_agent.captains_log.recall.query_relevant_reflections"` (the function
     `context.py` locally imports at call time) to return a single sentinel doc satisfying every
     filter the ADR names — recent timestamp, `seen_count >= 2`, non-empty `proposed_change.what`,
     status not `approved` — with an unmistakable marker string in `rationale`.
   - Call `assemble_context()` and assert the marker **is** present in the assembled messages.
   - Run it → confirm it **passes against current (unfixed) code**, proving the harness would have
     caught the defect. This test is deleted once the production deletion lands (nothing left to
     characterize) — its passing result here is the AC-2 evidence recorded in the PR/ticket handoff,
     not a permanent fixture.
2. **Permanent AC-2 proof tests** (added to `tests/personal_agent/request_gateway/test_context.py`,
   survive after the fix):
   - `test_reflection_recall_module_is_removed` — `importlib.import_module("personal_agent.captains_log.recall")`
     raises `ModuleNotFoundError`; assert `exc.value.name == "personal_agent.captains_log.recall"`
     specifically (not just any `ModuleNotFoundError`, which a missing transitive dependency could
     also raise).
   - `test_context_assembly_has_no_reflection_recall_reference` — `inspect.getsource(ctx_module)`
     contains none of `"captains_log.recall"`, `"query_relevant_reflections"`,
     `"format_reflections_section"`, `"reflection_recall"`. This is the literal AC-2 wording: "assert
     no call site or import from context assembly to the recall module remains."
   - `test_reflection_recall_settings_are_gone` — assert the live `settings` singleton has none of
     `reflection_recall_enabled` / `_recency_days` / `_max_results` / `_min_seen_count`; separately
     construct `AppConfig` with all four legacy `AGENT_REFLECTION_RECALL_*` env vars set (permissive
     values) and assert construction succeeds with none of those attributes present (`extra="ignore"`
     silently drops them — proven, not assumed).
   - A note in the test docstring / PR description that the marker-leak invariant is what step 1's
     (deleted) red-phase test demonstrated, not re-asserted here — a live path guard would be vacuous
     once the path is gone; the deleted-module + no-import-site tests are the operative permanent
     proof, matching AC-2's own wording ("assert no call site or import ... remains" as the given
     `must fail if the test would have passed with the call site still present` clause).
   - Out of scope (noted for master, not implemented here): codex additionally suggested a generic
     AST/import-boundary linter forbidding *any* future context-assembly dependency on a
     dimension-1-producer module. That generalizes to ADR-0125 D1 (the producer→dimension mapping
     enforcement), which is its own undecided mechanism with its own future implementation ticket —
     building it here would be scope creep beyond this ticket's literal AC-2 ask.
3. **DSPy supporting-fix test** (concrete, not deferred to a note) — add to
   `tests/test_captains_log/test_reflection_source_adr_0105.py` (reusing its existing fake-predictor
   `MagicMock` pattern at lines 47-67): assert `"trace_id" not in GenerateReflection.__annotations__`
   (or equivalent DSPy field introspection) and assert
   `"trace_id" not in fake_predictor.call_args.kwargs` after calling `generate_reflection_dspy(...)`.
2. Delete `src/personal_agent/captains_log/recall.py` and `tests/personal_agent/captains_log/test_recall.py`.
3. Edit `context.py`: remove the reflection-recall block (call site + in-block import), keep everything else (session-fact recall injection, memory query) untouched.
4. Edit `settings.py`: remove the 4 `reflection_recall_*` fields (and their leading `# FRE-348 / FRE-346 G2 / ADR-0067` comment, now dead).
5. Edit `.env.example`: remove the documented block.
6. Edit `docs/reference/CONFIG_INVENTORY.md`: remove the 4 rows.
7. Edit `reflection_dspy.py`: remove the `trace_id` `dspy.InputField` and the `trace_id=trace_id` argument passed into `reflection_generator(...)`. Leave the `generate_reflection_dspy(..., trace_id: str, ...)` function parameter untouched (still used for logging and `TelemetryRef`).
8. Edit `scripts/study/baseline_harness.py` docstring (one line).
9. Re-run the new tests → confirm green. Run `test_recall.py`'s absence doesn't break collection (file deleted, not skipped).
10. Full quality gates (Step 8 of the build skill).

## Test commands

```bash
make test-file FILE=tests/personal_agent/request_gateway/test_context.py
make test-file FILE=tests/personal_agent/captains_log/test_reflection_dspy.py   # if exists; else module test dir
make test  # full unit suite — confirm nothing else referenced the deleted module/settings
make mypy
make ruff-check
make ruff-format
pre-commit run --all-files
```

## Acceptance criteria proof (ADR-0125 AC-2)

- Sentinel/marker never reaches assembled context under forced-permissive legacy env OR stock defaults → `test_assemble_context_never_injects_reflection_marker` (both branches).
- No call site or import from context assembly to the recall module remains → `test_context_assembly_has_no_reflection_recall_reference` (source inspection, not a query-returned-nothing vacuous pass).
- Recall module itself is gone (not just its call site) → `test_reflection_recall_module_is_removed`.
- Settings entries are gone, not merely defaulted off → `test_reflection_recall_settings_are_gone`.

## Supporting fix proof

- `GenerateReflection.trace_id` field removed + not passed to the predictor → assert via `inspect.signature`/field introspection or a focused unit test on `reflection_dspy.py` that the signature's `input_fields` no longer include `trace_id`.
