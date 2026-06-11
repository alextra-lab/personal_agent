# FRE-523 — Redesign eval_mode suppression: run the memory pipeline during eval runs

**Ticket:** FRE-523 (Approved, Tier-2:Sonnet, project *Observability Foundation*)
**Related:** FRE-453 (eval set), FRE-505 (sub-agent audit), FRE-435/ADR-0087 (recall), FRE-521/522 (PWA eval flagging + reconciliation), ADR-0084 (pedagogical North Star)
**Supersedes:** FRE-387 (the original eval side-effect isolation that this reverses for the cognitive pipeline)

## Problem

The EVAL channel currently gates Captain's Log capture + reflection behind a single
`if not ctx.eval_mode:` in `executor.py`. With no capture written, the consolidator
(`read_captures()`) has nothing to read for eval turns → **zero entity extraction, zero
KG writes for eval runs** (measured `fre453-baseline-02`: 0/18 extraction). Cross-session
pedagogical continuity is therefore untestable through the harness.

## New suppression contract

| Concern | EVAL behavior |
|---|---|
| Captain's Log capture (`write_capture`) | **RUN** (primary + sub-agent) |
| `RequestCapturedEvent` (ADR-0041) | **RUN** |
| Reflection (`_trigger_captains_log_reflection`) | **RUN** |
| Entity extraction / consolidation | **RUN** (already un-gated; unblocked by capture) |
| Outward-facing tools (`create_linear_issue`, …) | **STAY SUPPRESSED** (`ctx.eval_mode` in `tools/linear.py:434`) |
| EVAL provenance on capture/KG | **STAMPED** (`eval_mode` flag → capture docs → `TurnNode.properties`) |

**Out of scope / unchanged:** `build_request_trace_es_handler` (request-trace ES doc) stays
suppressed for eval — it is observability telemetry, not part of the cognitive pipeline the
ticket enumerates. (Open question for owner — see bottom.)

### ⚠️ Indirect external side-effect path (found in codex review — MUST be closed)
Un-gating reflection means eval turns now write Captain's Log reflection entries. On
`consolidation.completed`, `build_consolidation_promotion_handler` →
`PromotionPipeline.run()` → `scan_promotable_entries()` promotes `AWAITING_APPROVAL`
proposal entries to **real Linear issues** via `linear_client.create_issue` (wired live in
`service/app.py` when `AGENT_LINEAR_API_KEY` is set). This bypasses the `tools/linear.py`
gate entirely. Filing tickets off eval turns is exactly what the ticket's "avoids filing
tickets about synthetic eval prompts" rationale forbids. **The promotion path must skip
eval-derived entries** (changes 6–8 below). The insights fan-out (`_publish_insight_events`,
`_save_proposals`) is internal-only (bus + ES) — no external effect, left as-is.

### Note on KG retrieval of eval entities (codex finding 5 — intentional)
Eval-derived Entity/Session nodes are **not** excluded from retrieval or promotion. The ticket
states the FRE-453 dataset is the owner's real learning threads and *belongs* in the KG;
acceptance criterion 3 requires a second eval run to *recall* prior-run content. Turn-level
`properties.eval_mode` is for **identifiability**, not filtering. No Entity/Session exclusion.

## Files & changes

### 1. `src/personal_agent/captains_log/capture.py` — provenance field
- `TaskCapture`: add `eval_mode: bool = False`.
- `SubAgentCapture`: add `eval_mode: bool = False`.
- `read_captures()`: pre-FRE-523 capture files on disk have no `eval_mode` key → Pydantic
  default `False` already handles it. No migration needed (the existing nil-UUID injection
  block is the precedent for forward-compatible reads).

### 2. `src/personal_agent/orchestrator/executor.py` — un-gate the cognitive pipeline
- **`_trigger_captains_log_reflection` (line ~1249):** remove `if ctx.eval_mode: return`.
- **Completion block (lines ~1448–1543):** remove the `if not ctx.eval_mode:` wrapper so
  `write_capture` + `RequestCapturedEvent` publish + reflection trigger always run on COMPLETED.
  - Add `eval_mode=ctx.eval_mode` to the `TaskCapture(...)` constructor.
  - Delete the `else:` branch that logged `eval_mode_side_effects_suppressed` (now inaccurate).
- **Unchanged:** `TraceContext(eval_mode=ctx.eval_mode)` at line ~1327 (keeps outward-tool
  suppression live).

### 3. Sub-agent path — deliberate + uniform provenance
Thread `eval_mode` so the already-unconditional sub-agent capture carries EVAL provenance:
- `src/personal_agent/orchestrator/sub_agent.py`
  - `_emit_sub_agent_capture(...)`: add `eval_mode: bool` param; set `eval_mode=eval_mode` on
    `SubAgentCapture`. Add a one-line comment: capture is intentionally unconditional per FRE-523.
  - `run_sub_agent(...)`: add `eval_mode: bool = False` param; pass to both
    `_emit_sub_agent_capture` call sites (success line ~397, cancel line ~353).
- `src/personal_agent/orchestrator/expansion.py`
  - `execute_hybrid(...)`: add `eval_mode: bool = False` param; pass to `run_sub_agent`.
- `src/personal_agent/orchestrator/expansion_controller.py`
  - `ExpansionController.execute(...)` + `_run_dispatch(...)`: add `eval_mode: bool = False`
    param; pass through to `run_sub_agent` (line ~431).
- `src/personal_agent/orchestrator/executor.py` (callers): pass `eval_mode=ctx.eval_mode` to
  `controller.execute(...)` (~1744) and `execute_hybrid(...)` (~2760).

### 4. ES templates — pin the new boolean (defensive, per ES-mapping discipline)
A boolean value won't be caught by the `.*_mode` enums-keyword dynamic template (that template
requires `match_mapping_type: string`), so it would map as `boolean` anyway — but pin it explicitly
(currently `eval_mode` is pinned in none of them):
- `docker/elasticsearch/captains-captures-index-template.json`: add `"eval_mode": { "type": "boolean" }`.
- `docker/elasticsearch/captains-subagents-index-template.json`: add `"eval_mode": { "type": "boolean" }`.
- `docker/elasticsearch/captains-reflections-index-template.json`: add `"eval_mode": { "type": "boolean" }`.
- Templates are re-applied by `scripts/setup-elasticsearch.sh` (master deploy concern; note in PR).

### 6. `CaptainLogEntry` — eval provenance on reflection entries
- `src/personal_agent/captains_log/models.py`: add `eval_mode: bool = False` to `CaptainLogEntry`.

### 7. Reflection generation — stamp eval provenance
- `src/personal_agent/captains_log/reflection.py`: `generate_reflection_entry(...)` add
  `eval_mode: bool = False` param; set it on the constructed `CaptainLogEntry`.
- `src/personal_agent/orchestrator/executor.py` `_trigger_captains_log_reflection`: pass
  `eval_mode=ctx.eval_mode` to `generate_reflection_entry`.

### 8. Promotion pipeline — skip eval-derived entries (close the Linear leak)
- `src/personal_agent/captains_log/promotion.py` `scan_promotable_entries()`: after loading
  `data`, add `if data.get("eval_mode"): continue` (with a `log.debug("promotion_skipped_eval_entry", ...)`).
  Reading the raw dict (not the validated model) guards even malformed entries.

### 5. Consolidator — stamp EVAL provenance onto the KG
- `src/personal_agent/second_brain/consolidator.py`: in `_process_capture`, when building the
  `TurnNode`, set `properties={..., "eval_mode": capture.eval_mode}` (both the normal and the
  FRE-380 stub-Turn paths). `TurnNode.properties` is a free dict — no schema migration. This
  satisfies "eval-derived KG content carries identifiable EVAL provenance."

## TDD — tests first

Rewrite `tests/test_orchestrator/test_eval_isolation.py` (the FRE-387 file this supersedes):
- **Invert** `test_eval_mode_suppresses_capture_and_reflection` →
  `test_eval_mode_now_writes_capture_and_reflection`: `eval_mode=True` ⇒ `write_capture`
  called once, `_trigger_captains_log_reflection` called.
- **Invert** `test_reflection_skipped_when_eval_mode_true` →
  `test_reflection_runs_when_eval_mode_true`.
- **Keep** `test_non_eval_writes_capture_normally`, `test_reflection_runs_when_eval_mode_false`,
  `test_es_trace_handler_skips_events_with_eval_mode`, `test_es_trace_handler_indexes_non_eval_events`,
  `test_session_writer_runs_for_eval_events` (unchanged — request-trace handler stays suppressed).
- **Add** `test_eval_capture_carries_eval_provenance`: capture written under `eval_mode=True`
  has `eval_mode is True`.

New `tests/test_orchestrator/test_sub_agent_eval_provenance.py`:
- `run_sub_agent(..., eval_mode=True)` emits a `SubAgentCapture` with `eval_mode=True`
  (patch `write_sub_agent_capture`, assert the captured arg).

New `tests/test_captains_log/test_capture_eval_field.py`:
- `TaskCapture(...)` / `SubAgentCapture(...)` default `eval_mode=False`; round-trips through
  `model_dump(mode="json")`.
- `read_captures()` tolerates a legacy on-disk capture file missing `eval_mode` (defaults False).

`tests/test_tools/test_linear.py` already has `test_eval_mode_blocks_issue_creation` — the
external-side-effect regression. Leave as-is (acceptance criterion 2 ✅).

Consolidator provenance: add to `tests/.../second_brain` (or existing consolidator test) a unit
that `_process_capture` writes `TurnNode.properties["eval_mode"]` matching the capture — patch the
memory service `store_*`/`create_turn` and assert the TurnNode arg.

Promotion leak (new — closes the codex-found gap):
- `tests/.../test_promotion*.py`: a promotable entry written with `eval_mode=True` is **not**
  returned by `scan_promotable_entries()` / not promoted, while an identical non-eval entry is.
- Reflection provenance: `_trigger_captains_log_reflection` with `eval_mode=True` writes an entry
  whose `eval_mode is True` (assert via the generator call / written entry).

## Exact commands
```bash
# module-scoped first
uv run pytest tests/test_orchestrator/test_eval_isolation.py \
  tests/test_orchestrator/test_sub_agent_eval_provenance.py \
  tests/test_captains_log/test_capture_eval_field.py \
  tests/test_tools/test_linear.py -q
# then the broader touched modules
make test-file FILE=tests/test_orchestrator/test_eval_isolation.py
make test            # full unit suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

## Acceptance mapping
1. Eval produces captures (primary + sub-agent) → consolidation extracts → **changes 1–3, 5**.
2. External tools stay suppressed (regression test) → **unchanged linear.py + existing test**.
3. Second eval run can recall prior run content → enabled by KG writes now occurring (verified
   post-deploy by master, not in PR checklist).
4. Eval-derived KG/capture carries EVAL provenance → **changes 1, 3, 5**.

## Open question for owner (before coding)
The ticket enumerates the cognitive pipeline to run and outward tools to suppress. It does **not**
mention `build_request_trace_es_handler` (the request-trace ES observability doc), which FRE-387
also suppressed for eval. FRE-522 reconciliation may want that trace indexed to join eval results
to the PWA per-case view. **Recommendation: leave it suppressed (surgical, in-scope) and let
FRE-522 decide.** Confirm, or tell me to also un-gate request-trace indexing for eval.
