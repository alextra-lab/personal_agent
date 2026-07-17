# FRE-376 Phase 4b — Tighten `ctx: TraceContext` on tool executors

**Issue:** [FRE-376](https://linear.app/frenchforest/issue/FRE-376) (In Progress, Urgent, Tier-1:Opus)
**ADR:** [ADR-0074 §3.6 / I6](../../architecture_decisions/ADR-0074-end-to-end-traceability.md)
**Prior PR:** [#70 — Phase 4a](https://github.com/alextra-lab/personal_agent/pull/70)
**Date:** 2026-05-22
**Branch:** `feat/fre-376-phase-4b-tool-ctx-required`

---

## Scope (verbatim from PR #70 deferred list)

> **Deferred to Phase 4b** (documented in ADR-0074): tool executor signatures
> (`bash`, `read`, `write`, `run_python`, `web_search`, `perplexity`,
> `context7`, `linear.*`, `read_skill`) keep `ctx: TraceContext | None = None`
> for now. Tightening those changes ~57 test invocations and the
> `<truncated: no ctx>` codepath in `bash_executor` — bundling that here would
> have blown past this PR's scope.

## Why this is safe now (post-4a)

`tools.executor.execute_tool` already requires `trace_ctx: TraceContext` (post-PR #70).
It forwards `ctx=trace_ctx` to every executor whose signature accepts `ctx`
(`executor.py:407–413`). With Phase 4a tightening on the call sites, the only
way an executor can be invoked with `ctx=None` is from tests that pass it
explicitly. Phase 4b removes the optional from the type and updates those tests.

## Out of scope

- `memory_search.py`, `notes_tools.py`, `artifact_tools.py`, `personal_history.py` —
  these use `ctx: Any = None` (orchestrator `ExecutionContext`, not
  `TraceContext`). They have a distinct identity model (`user_id` is required,
  not `trace_id`). Separate ticket.
- `bus.publish` / Cypher MERGE identity-threading audit — Phase 3 of
  ADR-0074, separate phase.
- Joinability probe / CI gates — Phase 5.

---

## Implementation

### Step 1 — Tighten signatures (9 files, 12 sites)

Change `ctx: TraceContext | None = None` → `ctx: TraceContext` (no default):

| File | Line | Function |
|------|------|----------|
| `src/personal_agent/tools/primitives/bash.py` | 290 | `bash_executor` |
| `src/personal_agent/tools/primitives/read.py` | 127 | `read_executor` |
| `src/personal_agent/tools/primitives/write.py` | 112 | `write_executor` |
| `src/personal_agent/tools/primitives/run_python.py` | 99 | `run_python_executor` |
| `src/personal_agent/tools/web.py` | 157 | `web_search_executor` |
| `src/personal_agent/tools/perplexity.py` | 76 | `perplexity_executor` |
| `src/personal_agent/tools/context7.py` | 78 | `context7_query_executor` |
| `src/personal_agent/tools/linear.py` | 392, 500, 560, 610 | 4 linear `*_executor` functions |
| `src/personal_agent/tools/read_skill.py` | 61 | `read_skill_executor` |

`ctx` becomes a required positional/keyword arg. The executor layer
(`tools/executor.py:407–413`) already passes it unconditionally.

### Step 2 — Remove defensive `if ctx else …` patterns

Sites within the 9 tightened executors (per `grep "ctx else"`):

- `bash.py:327, 425` (the `if ctx is not None` branch in the overflow path)
- `read.py:172`
- `write.py:154`
- `run_python.py:132`
- `web.py:193`
- `perplexity.py:109`
- `context7.py:104`
- `linear.py:430, 525, 573, 631`
- `read_skill.py:94`

All become direct `ctx.trace_id` access (with `getattr(ctx, "user_id", None)`
kept as-is when reading optional fields — `user_id` is unrelated to I6).

### Step 3 — Drop the `<truncated: no ctx>` dead branch in `bash_executor`

`bash.py:425-446` collapses:

```python
if ctx is not None:
    scratch = ctx.scratchpad_dir()
    …
    truncated_path = str(overflow_file)
else:
    truncated_path = "<truncated: no ctx>"
```

→ keep only the `ctx is not None` branch unconditionally (de-indented).

### Step 4 — Update test invocations

`grep "_executor.*(.*).*ctx=" tests/`: ~57 call sites across:

- `tests/test_tools/test_primitives_bash.py`
- `tests/test_tools/test_primitives_read.py`
- `tests/test_tools/test_primitives_write.py`
- `tests/test_tools/test_primitives_run_python.py`
- `tests/test_tools/test_web_search_integration.py`
- `tests/test_tools/test_perplexity.py`
- `tests/test_tools/test_linear.py`
- `tests/test_tools/primitives/test_bash_shell_contract.py`
- `tests/security/test_pivot2_pentest.py`
- `tests/personal_agent/orchestrator/test_read_skill.py`

Strategy: per-file `_ctx = TraceContext.new_trace()` fixture at top of module
(or use existing `TraceContext.new_trace()` if a fixture already exists),
then pass `ctx=_ctx` on each invocation. Don't add a shared conftest fixture
unless the file count justifies it (it doesn't — local module-level constant
is fine and matches the `test_litellm_emit_payload.py` precedent from PR #69).

**Special case:** `tests/test_tools/test_primitives_bash.py:218–237` —
the test `test_bash_output_overflow_no_ctx` explicitly tests
`ctx=None` → `<truncated: no ctx>`. Delete this test; the path no longer
exists.

### Step 5 — Quality gates

```bash
# Targeted first
uv run pytest tests/test_tools/ tests/personal_agent/orchestrator/test_read_skill.py -x

# Full suite
make test

# Type check
make mypy

# Lint + format
make ruff-check && make ruff-format

# Pre-commit
pre-commit run --all-files
```

All must pass with zero new errors. (Personal-path lints in `docs/plans/`
that pre-existed PR #70 are tolerated per #70 precedent.)

---

## Acceptance Criteria

### Pre-merge

| # | Check | Verifies |
|---|-------|----------|
| 1 | All 12 signature sites carry `ctx: TraceContext` (no `\| None`) | grep `ctx: TraceContext` returns no `| None` matches in `src/personal_agent/tools/` |
| 2 | `<truncated: no ctx>` literal removed | `grep "no ctx" src/personal_agent/tools/` returns 0 hits |
| 3 | `make test` 2549+ passed | Test suite stays green |
| 4 | `make mypy` clean | Strict mode green; no `Optional[TraceContext]` warnings remain on the 9 executors |
| 5 | `make ruff-check` + `make ruff-format` clean | Style |
| 6 | `pre-commit run --all-files` clean | Identity-threading + personal-path lints |

### Post-merge (same session as deploy)

| # | Check | Verifies |
|---|-------|----------|
| 7 | `make deploy` exits 0 | VPS pulls + restarts |
| 8 | `curl https://agent.example.com/health` returns 200 + healthy body | Live verification |
| 9 | MASTER_PLAN.md updated on `main`, committed, pushed | Project state visible |
| 10 | Linear comment with PR link, deploy timestamp, curl evidence | Audit trail |
| 11 | Ticket remains `In Progress` | Multi-phase rule — Phase 5 not yet shipped |

### Future gate (not part of this PR)

| # | Item | Phase |
|---|------|-------|
| 12 | `bus.publish` / Cypher MERGE identity audit | Phase 3 |
| 13 | Joinability probe in CI + 7-day prod green | Phase 5 |
| 14 | `mypy --strict` whole-tree green with TraceContext non-optional | Phase 5 |

---

## Risks

- **Test scope creep.** ~57 invocations is an estimate; could be higher.
  Mitigation: drive from `make test` failures, not from grep.
- **Hidden non-test call site.** If anything in `src/` invokes one of these
  9 executors directly with `ctx=None`, it will fail at mypy time. PR #70's
  call-site sweep should have caught this for the `tools.executor` path;
  confirm via `make mypy` before pushing.

## Rollback

Revert single PR. No data, schema, or telemetry shape changes.
