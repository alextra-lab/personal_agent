# FRE-262 PIVOT-3 Side Plan — Resolve Eval Blockers Before Re-Run

**Date**: 2026-04-27
**Status**: Approved (in execution)
**Parent**: [FRE-262](https://linear.app/frenchforest/issue/FRE-262) — PIVOT-3 of ADR-0063
**Master plan position**: Wave 2.5 (sub-track of FRE-262)
**Migration plan**: `docs/plans/2026-04-24-primitive-tools-migration-plan.md` Phase 3

---

## Context

The first FRE-262 PIVOT-3 eval (2026-04-27, 19 prompts on `make dev` against the VPS host) was a smoke test, not a gate decision. It surfaced 12 issues across four distinct dimensions:

| Dimension | Issues |
|---|---|
| Production bugs (unrelated to PIVOT-3 but visible during eval) | FRE-270 capture UUID, FRE-271 compressor role, FRE-272 approval warning, FRE-280 MCP cascade |
| Governance / loop gate | FRE-279 consecutive-different-args terminal |
| Eval infrastructure | FRE-273 vmstat skill, FRE-274 token capture, FRE-275 docker-compose.eval.yml, FRE-276 prompts curation, FRE-278 empirical skill review |
| Eval operations | FRE-277 data cleanup, FRE-281 latency overhead investigation |

Net result: the PIVOT-3 → PIVOT-4 gate cannot be evaluated from existing data. Treatment was 83% slower than control, but token data is missing and most failures trace to host-vs-container environment skew rather than primitive capability.

This side plan sequences the resolution of those 12 issues into a defensible re-eval. **All other development is paused until this plan completes.**

---

## Definition of Done

PIVOT-3 gate decision can be made from a single re-run with these properties:

1. ≥17/19 prompts complete cleanly in a production-equivalent environment (no env-skew failures)
2. Token + cache + turn count captured per prompt × variant
3. Skill-doc recipes have been empirically tested against the actual prod container (no first-call failures from wrong index names, missing flags, etc.)
4. Loop gate terminates pathological retry loops within 5 consecutive failed calls
5. Hand-graded `report.md` with `Quality` column filled and a defensible gate verdict

Once those five hold, the result either clears PIVOT-4 (≥17/19 ✅, primitives ≤1.5× cost) or surfaces a specific per-tool deprecation block list. Either is a valid gate outcome.

---

## Wave sequencing

```text
Wave A (parallel) — Production bugs, no eval dependency
  ├─ FRE-270  capture UUID                    [High]  ← Linear issues silently dropped on auth'd reqs
  ├─ FRE-271  compressor role missing         [Med]
  ├─ FRE-272  approval warning text           [Low]
  └─ FRE-280  MCP gateway lifespan cascade    [Med]

Wave B (parallel) — Eval infrastructure
  ├─ FRE-274  token + cache + turn capture    [High]
  └─ FRE-275  docker-compose.eval.yml          [High]
       │
       ▼
Wave C (depends on B) — Skill quality + governance
  ├─ FRE-278  empirical skill-doc review      [High]  ← absorbs FRE-273
  └─ FRE-279  loop gate consecutive terminal  [High]  ← unblocks ES prompts

Wave D (depends on C) — Prompt curation
  └─ FRE-276  prompts.yaml refresh

Wave E — Re-run + analysis
  ├─ Run PIVOT-3 eval against the cloud-sim docker stack
  ├─ FRE-281  latency overhead analysis (now possible with token data)
  └─ Hand-grade report.md → PIVOT-3 gate decision

Wave F (after Wave E) — Cleanup + housekeeping
  └─ FRE-277  eval-data cleanup script
```

---

## Wave A — Production bugs (parallel, ~1–2 days)

These are independent of the eval and of each other. They are surfaced by the eval but they exist in production traffic too. Land them first because they remove noise from the eval's signal channel and fix real prod regressions.

| Issue | Scope | Test |
|---|---|---|
| FRE-270 | Add `field_validator("user_id", mode="before")` in `TaskCapture` to coerce asyncpg UUID to Python UUID | Send a `/chat` request with an authenticated session; verify capture file lands at `telemetry/captains_log/captures/<date>/<trace_id>.json` |
| FRE-271 | Add fallback in `context_compressor.py` when `compressor` role missing — degrade to no-op with warning | Run with a model config lacking `compressor`; verify request completes without `ModelConfigError` |
| FRE-272 | Rename log key `approval_required_but_not_implemented` → `approval_ui_disabled_proceeding`; update message text | Grep logs after a non-auto-approved bash call with `AGENT_APPROVAL_UI_ENABLED=false`; confirm new message text |
| FRE-280 | Sequential init in `lifespan`: complete Redis subscriptions before MCP gateway | `make dev` with `AGENT_MCP_GATEWAY_ENABLED=true` on a host without `docker mcp`; confirm service boots |

**Gate to Wave B:** all four merged to main; quick smoke test against prod gateway shows clean logs (no `capture_write_failed`, no `context_compression_failed`).

---

## Wave B — Eval infrastructure (parallel, ~2–3 days)

These two are independent. Both must land before Wave C can validate skill recipes.

### FRE-274 — Token + cache + turn capture in runner

Extend `tests/evaluation/run_primitive_tools_eval.py`:
- After each `/chat` POST, query `http://elasticsearch:9200/agent-logs-*/_search?q=trace_id:<id>` to fetch `litellm_request_complete` events
- Sum `prompt_tokens`, `completion_tokens`, `cache_read_input_tokens`, `cache_creation_input_tokens`
- Count `tool_call_started` events; collect tool names; count `model_call_started` events as `iteration_count`
- Add columns to `report.md`: Ctrl tokens, Trt tokens, Cache hit %, Ctrl turns, Trt turns
- Footer summary: total token spend, mean turns per side, total wall clock
- Add `--es-url` CLI flag (default `http://localhost:9200`)

### FRE-275 — `docker-compose.eval.yml`

New override file extending `docker-compose.cloud.yml`:
- `seshat-gateway-control` on host port 9002, `AGENT_PRIMITIVE_TOOLS_ENABLED=false`
- `seshat-gateway-treatment` on host port 9003, `AGENT_PRIMITIVE_TOOLS_ENABLED=true AGENT_PREFER_PRIMITIVES=true AGENT_APPROVAL_UI_ENABLED=false`
- Both join the `cloud-sim` Docker network → DNS resolution works for `postgres`, `neo4j`, etc.
- Both use the production seshat-gateway image (full GNU userland)
- Both mount `/app/config/models.eval.yaml`
- Update `telemetry/evaluation/EVAL-primitive-tools/README.md` with new setup commands

**Gate to Wave C:**
- `docker compose -f docker-compose.cloud.yml -f docker-compose.eval.yml up -d` succeeds; both control and treatment respond on /health
- A single test prompt run on each instance produces token data in `results.json`

---

## Wave C — Skill quality + governance (parallel, ~3–5 days)

### FRE-278 — Empirical skill-doc review (absorbs FRE-273)

Inside the running treatment container (from Wave B), execute every recipe in:
- `docs/skills/bash.md`
- `docs/skills/read-write.md`
- `docs/skills/run-python.md`
- `docs/skills/query-elasticsearch.md`
- `docs/skills/fetch-url.md`
- `docs/skills/list-directory.md`
- `docs/skills/system-metrics.md`
- `docs/skills/system-diagnostics.md`
- `docs/skills/infrastructure-health.md`

For each recipe: run it, verify output matches expectation. Catalog failures and reasons. Replace any recipe that fails with one that works.

**Required additions per skill doc:**
1. Verify `bash` pipe support; either confirm `find … | wc -l` works or document the limitation
2. Add to `system-diagnostics.md`: ABI listing (`procps-ng 3.3.x`, supported flags), bounded vmstat/iostat patterns (interval × count < 30s) — folds in FRE-273
3. Add to `query-elasticsearch.md`: enumerated indices list (run `_cat/indices` once and embed the names + counts), top-level field map for `agent-logs-*`
4. Add to `infrastructure-health.md`: clear "from container" vs "from host" section; bash quick-checks moved AFTER the run_python recipe so the model picks the working path first
5. Verify `system-metrics.md` `/proc`-based recipe works in the sandbox (sandbox has read access to `/proc` of the container, not the host — confirm what gets reported)

Output: `docs/skills/EMPIRICAL_TEST_RESULTS.md` documenting which recipes were verified, with command + expected output for each.

### FRE-279 — Loop gate consecutive-different-args terminal

Add `loop_consecutive_terminal: bool = False` field to `ToolPolicy`. When set, `WARN_CONSECUTIVE` becomes terminal at `loop_max_consecutive`.

Set it to `true` in `config/governance/tools.yaml` for high-failure-rate tools:
- `query_elasticsearch` — wrong index/column names produce 400s, model retries indefinitely
- `bash` — unknown commands fail; model retries with new commands
- `run_python` — syntax errors produce non-zero exit; model retries

Tests:
- Unit: `WARN_CONSECUTIVE` on a tool with `loop_consecutive_terminal=true` returns `block_consecutive` at threshold
- Integration: re-run es-04 prompt; confirm it terminates within `loop_max_consecutive` calls instead of timing out at 120s

**Gate to Wave D:**
- `EMPIRICAL_TEST_RESULTS.md` shows ≥95% of skill-doc recipes pass first-try in the treatment container
- Re-running es-04 (or a synthetic loop prompt) terminates within 30 seconds, not 120s timeout

---

## Wave D — Prompt curation (~1 day)

### FRE-276 — `prompts.yaml` refresh

After Wave C, the eval environment is production-equivalent and the skill recipes are empirically validated. Now revisit each prompt:

| Prompt | Action |
|---|---|
| es-01, fetch-01..03, metrics-03, diag-02, infra-01, infra-04 | Keep (validated as working) |
| es-04 | Re-enable now that FRE-279 prevents the loop |
| ls-01, ls-02, ls-03 | Rewrite to use paths that exist in the container — drop `/app/` assumption or scope to known mount points |
| infra-02, infra-03 | Keep — they now work in container env (Docker DNS resolves) |
| es-02 | Replace with a query against an index we know has data, e.g. "How many `tool_call_started` events for `query_elasticsearch` in the last day?" |
| es-03 | Either fix telemetry to capture per-call latency OR replace with a query against `litellm_request_complete.elapsed_s` events |
| diag-01, metrics-01, metrics-02 | Keep — they should work in container (procps available) |
| diag-03 | Keep — FRE-278 fixed the unbounded vmstat in the skill doc |

Document expected output shape per prompt in `prompts.yaml` or alongside in `expected_outputs.md`. The human grader needs to know what "correct" looks like.

**Gate to Wave E:**
- Updated `prompts.yaml` with 20 prompts (es-04 re-enabled, ES prompts replaced where needed)
- Each prompt has documented expected output shape
- Sanity check: run 3 prompts each through control and treatment; all produce coherent answers

---

## Wave E — Re-run + analysis (~1 day)

1. Bring up `docker-compose.eval.yml` stack
2. Run full eval:
   ```bash
   PERSONAL_AGENT_EVAL=1 uv run python tests/evaluation/run_primitive_tools_eval.py \
     --control-url http://localhost:9002 \
     --treatment-url http://localhost:9003 \
     --es-url http://localhost:9200 \
     --output-dir telemetry/evaluation/EVAL-primitive-tools/run-final-<date>/
   ```
3. Inspect `report.md`: tokens, turns, latency, cache hit % per prompt
4. **FRE-281**: Analyze cost ratio — is treatment within 1.5× control on tokens × USD?
5. Hand-grade Quality column (✅/⚠️/❌)
6. Write up gate decision in `EVAL_RESULT.md`:
   - If ≥17/19 ✅ AND cost ratio ≤1.5× → **PIVOT-4 cleared**, deprecate the 8 listed tools
   - If ≥17/19 ✅ AND cost ratio >1.5× → **partial PIVOT-4**, deprecate only the categories where treatment is cost-competitive
   - If <17/19 ✅ → **partial deprecation only for categories where treatment ≥ control**

**Gate to Wave F:**
- `EVAL_RESULT.md` written with verdict and per-tool keep/deprecate list
- FRE-262 transitioned to Done in Linear
- Master plan updated to reflect FRE-263 (PIVOT-4) status (cleared or partially blocked)

---

## Wave F — Cleanup (~half day)

### FRE-277 — `scripts/cleanup_eval_data.py`

Driver that takes one or more `results.json` files, extracts `session_id` and `trace_id` values, and:
1. `DELETE FROM messages, sessions, api_costs WHERE id/session_id/trace_id = ANY(...)` against PostgreSQL
2. `_delete_by_query` against Elasticsearch `agent-logs-*` indices for those `session_id`s
3. `mv` matching Captain's Log capture JSON files into `telemetry/captains_log/archive/eval/`

Run it once over all eval runs from this side plan to clean accumulated pollution. Optionally wire `--cleanup-after` flag into the runner for future eval iterations.

---

## Critical files

| File | Wave | Status |
|---|---|---|
| `src/personal_agent/captains_log/capture.py` | A | Modify (FRE-270) |
| `src/personal_agent/orchestrator/context_compressor.py` | A | Modify (FRE-271) |
| `src/personal_agent/tools/executor.py` | A | Modify (FRE-272) |
| `src/personal_agent/service/app.py` | A | Modify (FRE-280) — `lifespan()` |
| `tests/evaluation/run_primitive_tools_eval.py` | B | Modify (FRE-274) |
| `docker-compose.eval.yml` | B | Create (FRE-275) |
| `config/models.eval.yaml` | B | Modify — add `compressor` role |
| `docs/skills/*.md` (9 files) | C | Modify (FRE-278) |
| `docs/skills/EMPIRICAL_TEST_RESULTS.md` | C | Create (FRE-278) |
| `src/personal_agent/orchestrator/loop_gate.py` | C | Modify (FRE-279) |
| `src/personal_agent/governance/policy.py` | C | Modify (FRE-279) — add field |
| `config/governance/tools.yaml` | C | Modify (FRE-279) — set per-tool flags |
| `telemetry/evaluation/EVAL-primitive-tools/prompts.yaml` | D | Modify (FRE-276) |
| `telemetry/evaluation/EVAL-primitive-tools/expected_outputs.md` | D | Create (FRE-276) |
| `telemetry/evaluation/EVAL-primitive-tools/EVAL_RESULT.md` | E | Create (gate decision) |
| `scripts/cleanup_eval_data.py` | F | Create (FRE-277) |

---

## Effort estimate

| Wave | Issues | Approx. effort |
|---|---|---|
| A | 4 prod bugs | 1–2 days (parallel) |
| B | 2 eval infra | 2–3 days (parallel) |
| C | 2 quality | 3–5 days (parallel; FRE-278 dominates) |
| D | 1 curation | 1 day |
| E | Re-run + grade | 1 day |
| F | Cleanup | 0.5 day |
| **Total** | | **8–12 days** elapsed |

If subagents are dispatched per-issue, much of this can be compressed.

---

## Out of scope for this side plan

These are explicitly NOT in this side plan and remain on the master plan untouched:

- FRE-249 / ADR-0059 (Context Quality Stream) — Wave 3 of master plan
- FRE-250 / ADR-0060 (KG Quality Stream) — Wave 3 of master plan
- FRE-251 / ADR-0061 (Within-Session Compression) — Wave 4 of master plan
- FRE-226 phase 2 (agent self-updating skills) — depends on FRE-248 (already done)
- FRE-263 / PIVOT-4 (deprecation) — gated by this plan's Wave E outcome
- FRE-265 / PIVOT-6 (delete legacy code) — 2-week window after PIVOT-4

---

## References

- Migration plan: `docs/plans/2026-04-24-primitive-tools-migration-plan.md`
- ADR-0063: `docs/architecture_decisions/ADR-0063-primitive-tools-action-boundary-governance.md`
- Original FRE-262 plan: `plans/complete-the-next-task-fancy-pebble.md`
- Master plan: `docs/plans/MASTER_PLAN.md`
- Linear issues filed during eval analysis (12): FRE-270 through FRE-281

---

## Sign-off

This plan is the work item. When Wave E produces a defensible gate verdict and FRE-262 closes, normal master-plan sequencing resumes (Wave 3: FRE-249, FRE-250).
