# FRE-961 — turn_status emits ABSENT (not 0) for unresolved ceilings

Backing: **ADR-0123** Decision §5 + 2026-07-24 acceptance note. Server half of the
absent-vs-zero rule (client half = FRE-935, merged). Parent FRE-933.

## Problem

`observability/topology/projector.py` `TurnObservation` defaults `tool_iteration_max`
and `context_max` to `int = 0`. `handle()` calls `_emit(obs)` at the end of *every*
branch (TopologyEntered, ModelCallCompleted, SubAgentProgress, Compaction markers) — so
an emission that fires **before** the resolving `TurnProgressEvent` carries `0` for these
ceilings. `0` is a live value the (fixed) client cannot distinguish from a resolved
ceiling — the same fabricated-signal harm §5 outlaws in the client, one layer down.

## Design decision — explicit `null`, not omit

Both are AC-acceptable. Choosing **explicit `null`** (key always present, value `None`):
- Keeps the payload shape stable — every `turn_status` carries the same keys, so no
  consumer that does `payload["context_max"]` can `KeyError`. (Confirmed: no server-side
  hard reads of these payload keys; `emit_turn_status` is projector-only per ADR-0088 D4.)
- Client handles it: `TurnStatusBar.presentNum(null) → null → "—"` for
  `tool_iteration_max`; `safeNum(null) → 0` for `context_max`. `types.ts` already types
  `tool_iteration_max: number | null`.
- Type gate lives on the projector field (`int | None`), so a future contributor cannot
  reintroduce a `0`-as-resolved default without changing the type.

Scope is **backend-only** (projector) per the ticket — no PWA change.

## Steps

1. **Test first** (`tests/observability/topology/test_projector.py`) — add 3 AC tests,
   confirm the AC-a / AC-c ones FAIL against current code:
   - `test_unresolved_ceilings_emit_absent_not_zero` (AC-a): after `TopologyEnteredEvent`
     with no prior progress, `emitted[-1]["context_max"] is None` AND
     `emitted[-1]["tool_iteration_max"] is None` — explicitly `is not 0`.
   - `test_resolved_ceilings_both_emit` (AC-b): a `TurnProgressEvent` with
     `tool_iteration_max=25, context_max=131072` → emitted `tool_iteration_max == 25`
     AND `context_max == 131072` (assert BOTH).
   - `test_counter_zero_distinct_from_absent_ceiling` (AC-c): the discriminator — in the
     pre-resolution emission, `tool_iteration == 0` and `context_tokens == 0` (counters
     present as real `0`) while `tool_iteration_max is None` and `context_max is None`.
2. **Implement** (`projector.py`):
   - `TurnObservation.tool_iteration_max: int | None = None` (was `int = 0`);
     `context_max: int | None = None` (was `int = 0`). Update the two field docstrings.
   - `_emit`: aggregate absent-preserving —
     `tool_iteration_max = None if obs.tool_iteration_max is None else obs.tool_iteration_max + sum(obs.sub_agent_iteration_max.values())`.
     Emit `obs.context_max` and this `tool_iteration_max` (both `int | None`).
   - Counters unchanged: `tool_iteration` / `context_tokens` stay `int`.
   - `handle()` TurnProgress branch unchanged — it *sets* both from the event (resolves).
3. **Verify**: `make test-file FILE=tests/observability/topology/test_projector.py`
   (module) → full `make test` · `make mypy` · `make ruff-check` · `make ruff-format`.

## AC → proof map

| AC | Proof |
|----|-------|
| (a) absent not zero | `test_unresolved_ceilings_emit_absent_not_zero` (both fields `is None`) |
| (b) both resolved emit | `test_resolved_ceilings_both_emit` (25 AND 131072) |
| (c) counter 0 distinct | `test_counter_zero_distinct_from_absent_ceiling` (0 vs None same emission) |

## Seam

FRE-935 (client, merged) renders absent-as-unknown; this makes the server send absent.
End-to-end closed only when both land — FRE-935 already did, so this verifies E2E, not
re-implements.
