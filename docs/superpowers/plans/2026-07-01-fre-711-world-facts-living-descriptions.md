# FRE-711 — World facts as living Claims: retire the Entity-description first-write-wins

**Ticket:** FRE-711 (Approved, Tier-1) · **Backing ADR:** ADR-0098 D2 · **Builds on:** FRE-638/712 (living-Claim substrate)
**Blast radius flagged:** hot dedup/recall path + the FRE-375 test-overwrite guard → **codex design pass required.**

## The fork (resolved → recommended, owner to confirm)
ADR-0098 D2 says "World facts = Claims/SPO over the entity spine." Two readings:
- **(A) Full SPO World Claims** — World facts become first-class `:Claim` nodes recall *traverses*. Large: changes the
  recall read path, real latency risk, and the extractor doesn't emit SPO triples. Over-reach for this ticket's
  acceptance direction.
- **(B) Living entity description (recommended)** — the entity's `description` (what recall already reads as a *property*)
  becomes **correctable** instead of frozen-on-first-write, with superseded descriptions **retained** as linked history.
  This is exactly the ticket's acceptance direction ("a wrong/thin first World description can be corrected … original
  retained as superseded … recall returns the corrected value … no regression to recall latency or the dedup guard").

**Recommendation: (B) as a tactical bridge.** Recall keeps reading `Entity.description` (context.py `entity.get("description")`)
— **zero read-path change, zero latency regression**. `Entity.description` is framed as a **materialized "current" cache** of the
World fact; the superseded history nodes carry full provenance (text, confidence, eval_mode, source_trace_id, proposed_name,
valid_from/valid_to) so they can **migrate into Claims/SPO later without losing the audit trail** (Codex #1). Full SPO (A) is a
later ADR-scoped step, gated on the extractor emitting triples — this ticket does not close D2's SPO end-state, it de-freezes the
description and stands up the history substrate.

## Design (B): confidence + eval-gated description correction, in ONE atomic statement

Today (`service.py` `create_entity`): `e.description = CASE WHEN e.description IS NULL OR '' THEN $new ELSE existing END`
— frozen. The only thing stopping a **test/eval** write from clobbering a real description is this freeze (FRE-375).

Replace it with a **single conditional Cypher statement** (not an app-side read-then-write — that races two concurrent
consolidations into a double-archive / stale-overwrite; the existing `assert_claim`/`assert_stance` are single atomic
statements and this must match — Codex #2). The gate is evaluated **inside** the MERGE against the freshly-matched node, so
there is no stale read:

- `ON CREATE` → set `description`/`description_confidence`/`description_eval_mode`/`description_set_at` (first write; unchanged).
- On MATCH, compute in-Cypher from the node's current props:
  - `do_fill`  = current description empty **and** `$new` non-empty (thin/missing first description gets filled).
  - `do_correct` = current non-empty **and** `$new` non-empty **and** `$new <> current`
      **and** NOT (`$eval` AND current is non-eval)         ← **eval gate, preserves FRE-375 (Codex #4)**
      **and** `$conf > coalesce(current_conf, $default_conf)` ← **STRICT `>` (Codex #3): a same-confidence re-extraction never
        clobbers; only a genuinely higher-confidence source corrects. Legacy rows (null conf) coalesce to the source default so
        they are NOT mass-reset on first post-deploy consolidation.**
  - `FOREACH (_ IN CASE WHEN do_correct THEN [1] ELSE [] END | CREATE (e)-[:HAD_DESCRIPTION]->(:EntityDescriptionVersion {…}))`
    archives the **old** value (captured in a prior `WITH`, so archive sees pre-`SET` text) before the `SET`.
  - `SET e.description = CASE WHEN do_correct OR do_fill THEN $new ELSE e.description END` (+ conf/eval/set_at likewise).

**Semantic consequence (owner decision below):** because every conversation extraction carries the same source confidence
(0.8), strict `>` means normal same-source re-extractions **do not** change the description — correction fires only for a
**higher-confidence source** (manual/user/tool) or the **empty-fill** case. This is faithful to the acceptance direction
("corrected by a later *higher-confidence* assertion") and to ADR-0098 D2's "not naive last-write-wins," but the feature is
**dormant for uniform-confidence conversation flow**. Enriching a thin *non-empty* description at equal confidence would need an
explicit extractor signal (a World `update_kind`, like FRE-712's for claims) — proposed as a follow-up, not built here.

`entity_type` / `properties` first-write-wins are **unchanged** (out of scope — the ticket is the *description*; narrower blast
radius). Recall path (context.py) is **untouched**.

### Threading (create_entity gains two args)
`create_entity(..., description_confidence: float = 0.8, eval_mode: bool = False)`. The consolidator passes
`KnowledgeWeight.from_source("conversation").confidence` and `capture.eval_mode` (already on the capture — used for the Turn).
`$default_conf` = the same source default, so legacy-null confidence is treated as already-0.8 (Codex #3).

## Acceptance criteria (definition of done — from the ticket's acceptance direction + ADR-0098 D2)
- **AC-1 (correctable by a higher-confidence assertion):** an entity with a thin/wrong description, then a later **non-eval**
  extraction of the *same* entity with a different, **strictly-higher-confidence** description → `e.description` becomes the new
  value. *Fails if* the first value persists (freeze) — proven behaviourally against live Neo4j.
- **AC-2 (original retained as superseded):** after AC-1, a `HAD_DESCRIPTION`→`EntityDescriptionVersion` node holds the old
  text with `valid_to` set and full provenance (confidence, eval_mode, source_trace_id, proposed_name). *Fails if* the old
  description is gone (no audit trail).
- **AC-3 (recall returns the corrected value, read-path unchanged):** the context-assembly read (`context.py`) returns the
  corrected `description`; the recall read path is not modified. *Fails if* recall still returns the stale value.
- **AC-4 (FRE-375 preserved — eval cannot clobber, behavioral):** seed a non-eval description, then `create_entity(eval_mode=True)`
  with a different description at **equal or higher** confidence → the stored description is **unchanged** and **no** history
  node is created for the rejected eval write. *Fails if* a test/eval write clobbers a real one (the FRE-375 regression).
- **AC-5 (safety net — same-confidence does not clobber; empty-fill; idempotent):** a same-confidence non-eval re-extraction
  with a *different* description does **not** overwrite (strict `>`), an empty new description never overwrites, re-extracting the
  same text creates no new version.
- **AC-6 (dedup/alias correction is audited):** a description extracted under a surface form that **dedups** to a canonical
  entity corrects that canonical entity's description (when it wins the gate), and the archive records the **proposed surface
  name** so a bad dedup-driven correction is auditable. *Fails if* the surface form is lost from the audit trail.

## Files
| File | Change |
|------|--------|
| `src/personal_agent/memory/service.py` | `create_entity`: +`description_confidence`/`eval_mode` args; **one atomic Cypher statement** — in-MERGE `do_fill`/`do_correct` gate, `HAD_DESCRIPTION` archive, conditional `SET description`. (No separate Python gate module — the logic is race-safe in Cypher, Codex #2.) |
| `src/personal_agent/second_brain/consolidator.py` | pass `description_confidence` (source weight) + `capture.eval_mode` + the proposed surface name into `create_entity` |
| `tests/personal_agent/memory/test_entity_description_first_write_wins.py` | **replace** the description *shape* assertions with the AC-4 behavioral anti-clobber guarantee; **keep** the `entity_type`/`properties` first-write-wins shape assertions unchanged |

## Steps (TDD)
1. **service `create_entity` (mocked-driver)** — `tests/personal_agent/memory/test_entity_description_correction_cypher.py`: the write Cypher contains the `do_correct`/`do_fill` gate expressions, the `HAD_DESCRIPTION` archive, strict `>` confidence, the eval gate, `proposed_name`; `entity_type`/`properties` clauses still first-write-wins. Implement the atomic statement.
2. **FRE-375 test** — replace the description shape assertions with the AC-4 behavioral anti-clobber test; keep type/properties shape assertions.
3. **Consolidator** — thread `description_confidence` + `eval_mode` + proposed name; wiring test asserts they reach `create_entity`.
4. **Behavioural AC proof (integration, live :7688)** — `tests/personal_agent/memory/test_world_description_correction.py`: AC-1 (higher-conf correction), AC-2 (version node + provenance), AC-3 (property read = corrected), AC-4 (eval can't clobber), AC-5 (same-conf/empty/idempotent), AC-6 (alias-driven correction records proposed name).

## Quality gates
`make test` (module then full) · `make mypy` · `make ruff-check`+`ruff-format` · `pre-commit run --all-files`.
ADR-0074 identity threading: the new `HAD_DESCRIPTION` / `EntityDescriptionVersion` write carries `source_trace_id`.

## Out of scope / follow-ups
- Full SPO World Claims recall traversal (fork A) — a later ADR-scoped step, gated on the extractor emitting triples.
  The `EntityDescriptionVersion` history nodes are schema'd to migrate into that model without losing the audit trail.
- **World `update_kind`/enrichment signal** (follow-up ticket): so a thin *non-empty* description can be enriched at
  equal confidence via an explicit extractor signal (mirroring FRE-712's claim `update_kind`), rather than requiring a
  strictly-higher-confidence source. Without it, uniform-confidence conversation flow leaves non-empty descriptions frozen.
- Back-migration of existing frozen descriptions — not touched; the correction path applies going forward. Note for master:
  **no bulk rewrite of historical rows**; legacy-null confidence coalesces to the source default so legacy descriptions are
  not mass-reset on the first post-deploy consolidation.
