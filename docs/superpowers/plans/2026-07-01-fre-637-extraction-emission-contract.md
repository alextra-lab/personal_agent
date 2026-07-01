# FRE-637 — Extraction-Emission Contract (ADR-0098 D5)

**Ticket:** [FRE-637](https://linear.app/frenchforest/issue/FRE-637) · Tier-1:Opus · Memory Recall Quality
**Backing ADR:** [ADR-0098](../../architecture_decisions/ADR-0098-memory-substrate-and-lifecycle-architecture.md) D1 + D2 (emission shape) + **D5** (the binding constraint)
**Validated by:** [FRE-636 spike](../../research/2026-06-27-fre-636-taxonomy-validation.md)
**Blocks:** FRE-638 (T2 storage), FRE-639 (T3 System gate), FRE-640, FRE-642 (seam)

## Design provenance
The emission contract below was designed by an Opus subagent (this session, 2026-07-01) grounded
in the ADR text + current `entity_extraction.py` / `consolidator.py` code. This plan is the
implementable distillation.

---

## Scope (3 bullets)
1. Redesign the extractor's **output contract** (`entity_extraction.py`: prompt + return dict) to emit,
   per item: a knowledge `class` on every entity (World/Personal/System), structured **Stances**
   (owner→World affect/mastery, never flattened), and Personal-situational **Claims** — plus a
   Python-stamped **provenance + timestamp** on every Stance and Claim.
2. Keep the existing `entities`/`relationships`/`entity_names`/`summary` keys and the 7 entity-types +
   6 relationship-types vocab **unchanged** — `class` and the two new arrays are purely **additive**;
   `consolidator.py` reads specific keys and ignores unknown ones, so no consolidator write-path change.
3. Thread `turn_timestamp=capture.timestamp` into the extractor (one additive kwarg) so a Claim's
   `observed_at` is the **turn** time, not the (lagging) consolidation-run time.

## Altitude of the AC this ticket proves (important — read at gate)
The ADR's **AC-3** is phrased at graph-edge altitude ("≥1 `HAS_STANCE` edge from the owner node").
FRE-637 is scoped to **extraction-emission only**; the actual Neo4j `HAS_STANCE` edge write is
**FRE-638 (T2)**, which is *blocked on* this ticket. Therefore FRE-637 proves AC-3 **at the extractor
output layer**: the redesigned extractor emits a structured Stance (owner → a named World concept,
carrying affect + nullable mastery) **and** a Personal Claim, with **neither flattened** into any
entity `description`. The graph-edge form of AC-3 + native traversal (AC-5) close at FRE-638 / the
assembled-ADR seam FRE-642. This is the correct decomposition — you cannot assert a graph edge without
FRE-638's storage code, and FRE-638 depends on this contract. Master gate: read this paragraph.

---

## Acceptance criteria carried → proof
| AC (this ticket's altitude) | Test / proof |
|---|---|
| **AC-3a — Stance survives as a structured item** | Unit test over synthetic car-buying fixture: `len(result["stances"]) >= 1`, stance has non-empty `affect`, `mastery is None`, `subject=="owner"`, `target in result["entity_names"]`. |
| **AC-3b — Personal fact survives as a Claim** | `len(result["claims"]) >= 1`; a claim `content` mentions the lease; `subject=="owner"`, `class=="Personal"`. |
| **AC-3c — neither is flattened** | No `entities[i]["description"]` contains the affect/lease text (substring check `love`/`lease`) — this assertion *reproduces the current bug* when it fails. |
| **provenance stamped by Python** | Every stance/claim has `provenance` with `observed_at == turn_timestamp.isoformat()`, `trace_id`/`session_id` matching the caller. |
| **class on every entity** | Every `entities[i]["class"] in {"World","Personal","System"}`. |
| **class on every Stance/Claim** | Every `stances[i]["class"] == "Stance"`; every `claims[i]["class"] == "Personal"` (D5 says "a class for every item" — entity classes are `{World,Personal,System}`; Stance-class lives in `stances[]` as a relation, not an entity). |
| **System is determinable** (lays AC-4 only) | Second fixture (operational turn) → every emitted entity `class == "System"`. Proves the extractor can *determine* System; the *gate* is FRE-639. Single fixture proves determinability; the four-subject AC-4 breadth (healthcheck / telemetry / harness / ping) is FRE-639's — the test encodes this as a `# lays AC-4, full breadth = FRE-639` limitation. |

**AC-3 is proven as an extractor proxy, not AC-3 closure** — the ADR's AC-3 check is a Neo4j `HAS_STANCE` edge from the owner node, which is FRE-638/642. FRE-637 proves the extractor *emits* the structured stance/claim; the graph-edge close is downstream.

---

## Files touched
- `src/personal_agent/second_brain/entity_extraction.py` — prompt (system + template), function signature
  (`turn_timestamp` kwarg), post-parse finalize (class default + stance/claim stamping), `_default_extraction_result`.
- `src/personal_agent/second_brain/consolidator.py` — **one additive kwarg** on the existing extractor call
  (`turn_timestamp=capture.timestamp`). No other change.
- `tests/test_second_brain/test_entity_extraction_contract.py` — **new** unit test file (mocked LLM;
  no live server) with the two synthetic fixtures + assertions above.

---

## Steps (atomic, TDD)
1. **Failing test first.** New `tests/test_second_brain/test_entity_extraction_contract.py`. Mock the
   LLM call (patch `LocalLLMClient.respond` / the cloud client) to return the Opus worked-trace JSON for
   the car-buying fixture, plus a System operational fixture. Assert the AC table above. **Run → confirm
   it fails** (current code has no `stances`/`claims`/`class`). The suite MUST include the Codex-flagged
   edge tests:
   - `test_stance_item_has_class_stance` — every `stances[i]["class"] == "Stance"` (not array-membership only).
   - `test_default_extraction_result_includes_empty_stances_and_claims` — the fallback paths (timeout,
     empty response, JSON parse failure, generic exception) all return `stances: []` + `claims: []`.
   - `test_supplemented_person_entity_gets_default_class` — the regex-supplemented Person
     (`_supplement_person_entities_from_user_message`) carries a `class` (finalize runs *after* supplement).
   - `test_python_stamping_overrides_llm_provenance` — if the mocked LLM returns bogus `observed_at`/`trace_id`,
     the final output uses the caller-provided values (Python stamps, LLM does not).
   - `test_consolidator_passes_capture_timestamp` — `_process_capture` calls the extractor with
     `turn_timestamp=capture.timestamp` (patch the extractor, assert the kwarg).
   - Verify: `make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py` → fails.
2. **Prompt redesign.** Add KNOWLEDGE CLASS + STANCES + CLAIMS blocks to `_EXTRACTION_PROMPT_TEMPLATE`,
   rules 11-13, GOOD/BAD examples, new JSON skeleton; update `_EXTRACTION_SYSTEM_PROMPT`. Keep existing
   entity/relationship vocab + rules 1-10 — but **reword rule 8** to resolve the System conflict: an
   *empty/placeholder/test-artifact* exchange still returns empty arrays, **but a real owner operational
   turn** (a healthcheck they actually ran, telemetry/log review, a harness explainer) emits its subjects
   as `class=System` rather than being dropped. (Codex flag: current rule 8 "system test → empty" would
   otherwise swallow the very System material D5 wants labelled.)
3. **Function + finalize.** Add `from datetime import datetime, timezone` import (the module has none today).
   Add `turn_timestamp: datetime | None = None` kwarg. Add module helpers
   `_build_provenance`, `_coerce_mastery`, `_normalize_entity_class`, `_finalize_extraction`. Call
   `_finalize_extraction` in the success path; add `stances`/`claims` to the returned dict and to
   `_default_extraction_result`. Add `stances_found`/`claims_found` to the completion log.
4. **Consolidator thread-through.** Add `turn_timestamp=capture.timestamp` to the extractor call.
5. **Green.** Re-run the new test → passes. Run the existing `test_entity_extraction.py` (integration,
   `requires_llm_server` — skipped in `make test`) is untouched; confirm no unit regressions.
   - Verify: `make test-file FILE=tests/test_second_brain/test_entity_extraction_contract.py` → passes;
     `make test` module + full green.
6. **Quality gates:** `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

---

## Decisions (owner-confirmed 2026-07-01)
1. **Persist `class`? — NO (emission-only).** `class` is emitted in the returned dict (proves AC-3 at
   emission altitude) but is NOT written to Neo4j this ticket — that is FRE-638's job. No
   `properties['class']` mirror. FRE-637 stays pure emission.
2. **Entity `class` default fails OPEN to `World`** (missing/invalid → World, never silently System) so a
   hedging model never starves the tutor. System-precision is FRE-639's knob.
3. **Provenance stamped on BOTH Stances and Claims** — ADR-0098 D2 treats `HAS_STANCE` as a supersedable,
   provenance-bearing edge (preferences flip), so FRE-638 will need it there.
4. **`subject: "owner"` is a sentinel** the extractor emits; FRE-638 must resolve it to the `is_owner`
   `:Person` node (ADR-0052). This is a hand-off contract to note on FRE-638 — not enforceable here.

## Follow-ups to file (Needs Approval, Memory Recall Quality project)
- Note on FRE-638: (a) resolve `subject:"owner"` → is_owner node; (b) read `class` + consume
  `stances`/`claims`; (c) `is_fallback` gate in consolidator is entity+summary-based and ignores
  stances/claims — revisit when claims-only turns can stand alone.
- Pre-existing debt (mention, do not fix): the 7 entity-types have no "Product" slot; car models land as
  `Technology`.
