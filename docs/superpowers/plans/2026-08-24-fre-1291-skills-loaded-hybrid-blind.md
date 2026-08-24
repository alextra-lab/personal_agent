# FRE-1291 — `route_traces.skills_loaded` is blind to `hybrid`/`keyword`

**Ticket:** https://linear.app/frenchforest/issue/FRE-1291
**ADR:** ADR-0066 D1 (`hybrid` is the decided default routing mode)

**Revision note:** an earlier draft of this plan added a new `ExecutionContext.skills_injected`
field written from the `hybrid`/`keyword` branches. Codex plan-review (2026-08-24) found a
strictly better fix already sitting in the codebase and it changed the plan completely — see
"Why the union-with-a-new-field draft was wrong" below.

## Defect

`route_traces.skills_loaded` (assembler.py:314) reads `ctx.loaded_skills`. That field is
written in exactly two places: the `model_decided` router's pre-load path
(executor.py:4287) and the `read_skill` tool handler (tool_dispatch.py:217). The `hybrid`
branch (executor.py:4329-4334) and `keyword` branch (executor.py:4336) call
`get_skill_bodies(...)`, inject the returned bodies into the prompt, and assign the names to
a *local* `_skill_body_names` — `ctx.loaded_skills` never sees them. Under `hybrid` (the live
default), telemetry records an empty array even though skills were injected.

## The fix that actually belongs here

`_skill_body_names` (declared once at executor.py:4225, populated identically by all three
routing-mode branches — model_decided at 4328, hybrid at 4331, keyword at 4336) is **already**
threaded into `_record_turn_evidence(..., skill_body_names=_skill_body_names, ...)` at
executor.py:4784-4792, unconditionally, once per turn, regardless of routing mode. That
function stores it on `ctx.turn_evidence.assembled_context.skill_bodies`
(`captains_log/turn_evidence.py:404-447`) — gated on `block_reached_input`
(`turn_evidence.py:757-759,816`): `skill_bodies=list(skill_bodies) if block_reached_input else []`.
This is *more* accurate than a raw "what did get_skill_bodies return" record — it only claims
a skill was loaded if the volatile block provably reached the wire input, which is exactly
the ADR-0125 D3 evidence contract FRE-1004 built for this reason.

So the FRE-1004 evidence pipeline already answers "what skills did this turn actually inject,
for any routing mode" correctly. **The bug is entirely in the assembler**: it reads
`ctx.loaded_skills` (the dedup key, populated only by `model_decided` + `read_skill`) instead
of unioning it with `ctx.turn_evidence.assembled_context.skill_bodies` (the evidence record,
populated for all three modes).

### Why the union-with-a-new-field draft was wrong

The original draft planned to add `ExecutionContext.skills_injected: set[str]`, updated
immediately after `get_skill_bodies()` returns, and union it into the assembler. Codex found
two problems: (1) it's redundant with evidence already recorded correctly elsewhere, and (2)
it's *less* accurate — updating it immediately after `get_skill_bodies()` returns means it
would claim injection even in the case (rare, but real: a non-string user-content shape the
inliner declines) where `_inline_volatile_with_outcome` fails to actually splice the block
into the wire input, producing exactly the kind of confidently-wrong telemetry this ticket
exists to fix. `turn_evidence.assembled_context.skill_bodies` doesn't have that gap because
it's built *after* the inline step and reads its outcome.

## Fix (single file)

**`src/personal_agent/observability/route_trace/assembler.py`** — in `assemble_route_trace`,
before the `return RouteTraceRow(...)` (near the other pre-computed locals, ~line 280), add:

```python
_turn_evidence = getattr(ctx, "turn_evidence", None)
_assembled_context = getattr(_turn_evidence, "assembled_context", None)
_evidence_skill_bodies = getattr(_assembled_context, "skill_bodies", None) or []
```

And change line 314 from:

```python
skills_loaded=tuple(sorted(getattr(ctx, "loaded_skills", None) or set())),
```

to:

```python
skills_loaded=tuple(
    sorted((getattr(ctx, "loaded_skills", None) or set()) | set(_evidence_skill_bodies))
),
```

`ctx.loaded_skills` stays in the union deliberately: a `read_skill` tool call *after* the
turn's first primary call (`tool_iteration_count > 0`) lands there but not in
`turn_evidence` (recorded only at call 0 — `executor.py:4783`), so dropping it would lose
real mid-turn `read_skill` loads. No changes to `executor.py`, `types.py`, or any dedup /
injection code path — this ticket's out-of-scope line holds by construction, not by test.

`assemble_sub_agent_route_trace` (segment rows) is untouched: it never derived `skills_loaded`
from anything skill-related (confirmed — it doesn't set the field at all, defaulting via
`RouteTraceRow`), and sub-agents don't run the routing-mode branches this ticket is about
(`sub_agent.py` builds its own prompt from `SubAgentSpec`, no `ExecutionContext`, no
`turn_evidence`). Out of scope for this ticket regardless.

## Tests (TDD — write first, confirm RED against current `main`, then implement)

**`tests/observability/route_trace/test_assembler.py`** — add:
- `test_skills_loaded_includes_evidence_skill_bodies` (AC-1/AC-2/AC-3 reproduction, mode-agnostic
  at this layer) — `ctx` with `loaded_skills=set()` and a `turn_evidence` stand-in whose
  `assembled_context.skill_bodies == ["web-search"]` → `row.skills_loaded == ("web-search",)`.
  Run this against current `main` first: it fails (`skills_loaded == ()`), which is the AC-2
  reproduction — the exact shape of the historical bug (skill injected, evidence recorded,
  telemetry still empty).
- `test_skills_loaded_unions_loaded_skills_and_evidence_skill_bodies` (AC-4 no-regression +
  union correctness) — `loaded_skills={"recall"}` (the `model_decided`/`read_skill` source)
  *and* `assembled_context.skill_bodies == ["web-search"]` → `row.skills_loaded == ("recall",
  "web-search")`. Sorted, deduped (overlapping name appears once).
- `test_skills_loaded_handles_missing_turn_evidence` — `ctx` with no `turn_evidence` attribute
  (or `turn_evidence=None`) → no crash, `skills_loaded` falls back to `loaded_skills` alone.

**`tests/personal_agent/orchestrator/test_skill_injection.py`** — add a class driving
`step_llm_call` (same functional-mock pattern as `TestSkillBlockFunctionalInjection`), mocking
`get_skill_bodies` to return `(_SENTINEL, ("web-search",))`:
- `test_hybrid_mode_turn_evidence_records_injected_skill` — hybrid mode → after
  `step_llm_call`, `ctx.turn_evidence.assembled_context.skill_bodies == ["web-search"]`. This
  is the "live turn" proof AC-1 asks for, one layer below the assembler union test above.
- `test_keyword_mode_turn_evidence_records_injected_skill` (AC-3) — same shape,
  `skill_routing_mode="keyword"`.

AC-5 (dedup unchanged) needs no new executor-level test: the fix touches only
`assembler.py`, a pure read-side adapter with no path back into `get_skill_bodies` or
`read_skill`'s `loaded_skills` checks. `make test` passing (specifically the existing
`test_route_skills.py`, `test_skill_injection.py` suppression-relevant cases, and
`test_read_skill.py`) is the evidence that nothing in the dedup path moved — recorded as such
in the handoff rather than as a new test, since there is no code path a new test could catch
that these don't already cover.

## Test commands

```bash
uv run pytest tests/observability/route_trace/test_assembler.py -v
uv run pytest tests/personal_agent/orchestrator/test_skill_injection.py -v
make test
make mypy
make ruff-check
```

## Acceptance criteria mapping

| AC | Evidence |
| -- | -- |
| AC-1 | `test_skills_loaded_includes_evidence_skill_bodies` + `test_hybrid_mode_turn_evidence_records_injected_skill` |
| AC-2 | `test_skills_loaded_includes_evidence_skill_bodies` run RED against pre-fix code (handoff records the failure) |
| AC-3 | `test_keyword_mode_turn_evidence_records_injected_skill` |
| AC-4 | `test_skills_loaded_unions_loaded_skills_and_evidence_skill_bodies` (model_decided's `loaded_skills` source stays in the union, untouched) |
| AC-5 | No dedup/injection code changed (assembler-only fix); `make test` green across `test_route_skills.py` / `test_skill_injection.py` / `test_read_skill.py` is the demonstration |

## Out of scope (per ticket)

No change to which skills get injected, to routing mode selection, or to dedup semantics.
No change to `SubAgentSpec`/sub-agent skill inheritance, or to segment route-trace rows.
