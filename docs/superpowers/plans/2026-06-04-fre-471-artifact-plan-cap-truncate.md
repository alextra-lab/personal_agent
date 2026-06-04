# FRE-471 — artifact_draft plan cap: truncate-with-warning instead of terminal hard-fail

**Ticket:** FRE-471 (Approved, Tier-2:Sonnet, Bug) · Project: Turn Reliability Hardening (2026-06-04 incident)
**Refs:** post-mortem `docs/postmortems/2026-06-04-artifact-turn-failure-cache-control.md`; related FRE-391 (dynamic max_tokens).

## Problem

`artifact_draft_executor` raises a terminal `ToolExecutionError` when `len(plan) > _MAX_PLAN_CHARS`
(`src/personal_agent/tools/artifact_tools.py:1042-1046`, `_MAX_PLAN_CHARS = 8000`). The plan is a
*spec* the sub-agent expands into HTML (`_DRAFT_MAX_TOKENS = 16384`). 8000 chars is too tight, and a
hard-fail on the last tool round leaves the turn with no artifact (incident trace `c216bd40`).

## Fix (both levers) — revised per codex review

1. **Raise the ceiling.** `_MAX_PLAN_CHARS = 16000`. Rationale: ~16k chars ≈ ~4k input tokens; with
   the system prompt + title/summary the sub-agent input stays well within context budget. (`_DRAFT_MAX_TOKENS`
   is the *output* budget — not a true proxy, but a convenient round ceiling that comfortably fits.)
2. **Truncate-with-warning above the ceiling.** Instead of raising:
   - **Boundary-aware trim** (codex #2): cut at the last paragraph/line boundary (`\n`) at or before the
     limit so a section isn't severed mid-sentence; fall back to a hard char cut only if no boundary exists.
   - Append a **strong** truncation notice (codex #1, #5) that tells the sub-agent the spec is *incomplete*,
     to build a coherent artifact from the sections present, **not** to fabricate omitted requirements, and
     to favor visible completeness. The notice is part of the effective plan, kept within `_MAX_PLAN_CHARS`.
   - `log.warning("artifact_draft_plan_truncated", trace_id=…, session_id=…, slug=…, task_id=…,
     original_length=…, truncated_length=…)` — full trace context (codex #4).
   - thread `plan_truncated: bool` + `plan_original_length: int` into the returned dict.
   - Empty/whitespace plan still raises `ToolExecutionError` (genuinely unrecoverable).

## Atomic steps

### Step 1 — constants + helper (artifact_tools.py)
- File: `src/personal_agent/tools/artifact_tools.py`
- Change `_MAX_PLAN_CHARS = 8000` → `16000`.
- Add `_PLAN_TRUNCATION_NOTICE` constant — a strong instruction block appended to a truncated plan:
  states the spec was trimmed and is incomplete, instructs the generator to build a coherent artifact
  from the sections present, to NOT invent omitted requirements, and to favor visible completeness.
- Add a pure helper `_truncate_plan(plan: str) -> tuple[str, bool, int]` returning
  `(effective_plan, was_truncated, original_length)`:
  - if `len(plan) <= _MAX_PLAN_CHARS`: return `(plan, False, len(plan))`.
  - else: `budget = _MAX_PLAN_CHARS - len(_PLAN_TRUNCATION_NOTICE)`; cut at the last `\n` at or before
    `budget` (boundary-aware); if none, hard-cut at `budget`; append the notice; return `(…, True, len(plan))`.
  - Result is always ≤ `_MAX_PLAN_CHARS`.

### Step 2 — wire into executor
- Replace the `if len(plan) > _MAX_PLAN_CHARS: raise …` block with a call to `_truncate_plan`.
- Keep the empty-plan `raise ToolExecutionError`.
- Use the returned `effective_plan` in `user_prompt`; log warning when truncated; set
  `result["plan_truncated"]` / `result["plan_original_length"]` before return.

### Step 3 — update tool param description
- Plan param description currently says "Max ~8000 chars." → "Max ~16000 chars; longer plans are
  truncated with a notice rather than rejected."

### Step 4 — tests (TDD: write first, confirm fail)
- File: `tests/personal_agent/tools/test_artifact_tools.py`
- Rewrite `test_artifact_draft_oversized_plan_raises` → `test_artifact_draft_oversized_plan_truncates`:
  plan of `"section\n" * 4000` (~32k chars, with `\n` boundaries) produces an artifact,
  `out["plan_truncated"] is True`, `out["plan_original_length"] == len(plan)`, sub-agent prompt contains
  the truncation notice, effective plan sent ≤ `_MAX_PLAN_CHARS`, and (boundary check) the trimmed plan
  body ends on a line boundary (no mid-word cut) — assert it does not end mid-"section".
- Add `test_artifact_draft_plan_within_cap_not_truncated`: normal plan → `out["plan_truncated"] is False`,
  `out["plan_original_length"] == len(plan)`, notice absent from prompt.
- Keep `test_artifact_draft_empty_plan_raises` unchanged.
- Helper to read the prompt: `client.respond_calls[0]["messages"][1]["content"]`.

## Test commands

```bash
make test-file FILE=tests/personal_agent/tools/test_artifact_tools.py   # module
make test            # full suite
make mypy
make ruff-check && make ruff-format
pre-commit run --all-files
```

Expected: new truncation test passes; oversized-raises test removed; full suite green.

## Out of scope

- Dynamic `max_tokens` by task context (FRE-391).
- The cache_control 400 sibling issue (separate ticket).
