# FRE-452 — Route-Trace Ledger (implementation plan)

> **Ticket:** FRE-452 (Approved · Tier-2:Sonnet · project: Observability Foundation)
> **Governing ADR:** ADR-0088 — Execution Topology Observability Contract (**Accepted** 2026-06-06; L0 keystone)
> **Taxonomy:** `docs/specs/RESULT_TYPE_TAXONOMY_SPEC.md` (FRE-451, Done) → ADR-0084 §D4
> **Identity:** ADR-0074 (joinability / emit-site discipline)
> **Author session:** build (`worktree-build`) — stops at PR.

---

## 1. Scope decision (read of FRE-452 vs ADR-0088)

ADR-0088 owns the **contract + the full spine** (the `observe_topology` seam threaded through
every topology, the live projector, `report_degradation`, the `stream:turn.observed` bus event,
and the eventual removal of FRE-501's per-loop accumulation). FRE-452 owns **one piece of that
spine**: the **route-trace ledger** — its schema, its row model, the programmatic
orchestration-event classifier, and the **direct durable write** (ADR-0088 D6 sink 1, D8: a
bus-independent Postgres write).

**This plan builds the ledger instrument only (one phase = one PR):**

- ✅ Ledger schema (Postgres `route_traces`) — settles ADR-0088's deferred storage open-decision.
- ✅ `RouteTraceRow` frozen model + `OrchestrationEvent` literal (taxonomy §3).
- ✅ Pure `assemble_route_trace(ctx, authoritative_cost)` + programmatic
  `classify_orchestration_event(ctx)`.
- ✅ `RouteTraceLedger` service (asyncpg): identity-enforced `write` + `get_by_trace_id` read.
- ✅ ONE durable-write site at `execute_task` terminal (success **and** error/cancel paths).
- ✅ The **critical shell-boundary field**: gateway label captured alongside the actual
  orchestration event (the comparison ADR-0088 §"critical field" wants exposed).
- ⛔ **Deferred to other ADR-0088 spine phases (NOT this PR):** `observe_topology` context-manager
  threading through sub-agent/decompose topologies, the live projector, `report_degradation`,
  the bus stream, and FRE-501 removal.

**Seam-neutrality (codex P2):** to make ADR-0088 D6 ("the FRE-452 ledger writer is the seam's
direct write") rework-free, the **writer takes a stable DTO**: `RouteTraceLedger.write(row:
RouteTraceRow)` never depends on `ExecutionContext`. `assemble_route_trace(ctx, …)` is the
**interim primary-turn adapter** that builds the DTO from `ctx`; the `execute_task` call site is
explicitly an interim primary-turn emission. When the seam lands, `observe_topology` calls the
*same* `write` with a DTO it assembles per topology — only the adapter/call-site is replaced, not
the writer or the schema. (Claim narrowed from "no throwaway work" to: the **writer + schema are
seam-neutral; the adapter is interim**.)
- ⛔ **Pedagogical-outcome layer is NOT computed.** Taxonomy §6: those outcomes are
  human-rubric/hybrid and not programmatic until the M3 pedagogical layer emits substrate writes.
  The ledger stores them as a **nullable `pedagogical_outcomes JSONB` slot** for M2/M3/rubric to
  fill — it does not fabricate them.

**Why Option A (ledger only), not the full seam:** the full seam bundles multiple ADR-0088 phases
into one PR (a `/build` halt condition), and FRE-505 (sub-agent auditability) / FRE-506
(gate-decision telemetry) are the ADR's other spine consumers with their own tickets. FRE-452's
own description is "build the route-trace ledger … a programmatic instrument that captures
per-turn."

---

## 2. What the ledger captures (FRE-452 per-turn list → fields)

Everything is read from `ExecutionContext` at turn completion + a `SUM(api_costs)` query. No new
threading is required — `ctx` already carries it all.

| FRE-452 capture | Source on `ctx` | Ledger field |
|---|---|---|
| Stimulus | `ctx.user_message`, `ctx.messages` | `user_message_preview` (bounded), `user_message_chars`, `message_count` |
| TaskType + Complexity + decomp reason | `ctx.gateway_output.intent.{task_type,complexity,confidence}`, `.decomposition.{strategy,reason}` | `task_type`, `complexity`, `intent_confidence`, `decomposition_strategy`, `decomposition_reason` |
| Routing decisions (stages 1–7) | `ctx.gateway_output.degraded_stages`, `.governance.mode` | `degraded_stages TEXT[]`, `mode` |
| Tools/skills considered + selected | `ctx.tool_results`, `ctx.tool_iteration_count`, `ctx.loaded_skills` | `tool_iteration_count`, `tools_used TEXT[]`, `skills_loaded TEXT[]` |
| Model tier + thinking | `ctx.selected_model_role`, `ctx.routing_history` | `model_role`, `thinking_enabled`, `routing_history JSONB` |
| Delegation + delegate shape | `ctx.sub_agent_results`, `ctx.expansion_phase_results`, `ctx.expansion_strategy` | `sub_agent_count`, `sub_agents JSONB` (per-sub: model/tools/tokens/cost/success/summary_chars/output_chars), `expansion_strategy` |
| Primary synthesis behavior | `ctx.final_reply` length, orchestration event | `final_reply_chars`, `orchestration_event` |
| Final result type(s) | classifier (orchestration) + nullable pedagogical slot | `orchestration_event`, `pedagogical_outcomes JSONB NULL` |
| Latency breakdown | `ctx.request_timer.to_trace_summary()` (phase buckets) + `.get_total_ms()` | `latency_total_ms`, `latency_breakdown JSONB` |
| Token + cost breakdown | `ctx.turn_cost_usd` (live) + `SUM(api_costs WHERE trace_id)` (authoritative) | `cost_live_usd`, `cost_authoritative_usd`, `cost_reconciled`, `input_tokens`, `output_tokens` |
| Fallback / error path | `ctx.error`, `ctx.classified_error`, phase/sub failures | `fallback_triggered`, `error_type`, `error_class` |
| **Shell boundary (ADR-0088 critical field)** | gateway label vs orchestration event | `gateway_label` (`"<task_type>/<strategy>"`) + `orchestration_event` |

**PII handling (CLAUDE.md "never log PII") — gated, not just truncated (codex P1):** truncation
alone is **not** a privacy boundary, and the ledger row is a new durable PII-bearing surface
(Postgres backups, replication, log lines). Therefore:

- **Default OFF.** The ledger stores only `user_message_chars`, `message_count`, and a
  `user_message_sha256` (16-hex prefix) — **no raw text** by default. The full stimulus already
  lives in `agent-captains-captures-*` keyed by `trace_id`; the ledger joins to it, never
  re-copies it.
- **Opt-in preview.** A bounded `user_message_preview` (≤ `settings.route_trace_preview_chars`,
  default 280) is stored **only** when `settings.route_trace_store_preview = False` is flipped on;
  otherwise the column is `NULL`. The exposure surface (where this field would then propagate) is
  documented inline in `ledger.py`.

---

## 3. Orchestration-event classifier (taxonomy §3, programmatic layer)

`classify_orchestration_event(ctx) -> OrchestrationEvent` where
`OrchestrationEvent = Literal["primary_handled","delegate_called","delegate_result_used",
"delegate_result_discarded","fallback_triggered"]`.

Rules (taxonomy §3 + §5.2 *single best terminal event* convention, flagged `[proposed — M2 validates]`):

```
subs   = ctx.sub_agent_results or []
phases = ctx.expansion_phase_results or []

# fallback: a phase failed and primary took over, or all sub-agents failed
if any(not p.success for p in phases) or (subs and all(not s.success for s in subs)):
    return "fallback_triggered"

# no sub-agent contribution at all → primary handled end-to-end (SINGLE or assessed-no-spawn)
if not subs:
    return "primary_handled"

# sub-agents ran; used-vs-discarded is HYBRID (taxonomy §3.3/§3.4) — not a pure flag.
# Programmatic floor = delegate_called; raw disposition signals are stored in `sub_agents`
# JSONB so M2's rubric can refine to used/discarded.
return "delegate_called"
```

**Honest detection boundary (taxonomy §6):** `delegate_result_used` / `delegate_result_discarded`
are *hybrid* — there is no harness flag for genuine incorporation, so the programmatic classifier
returns `delegate_called` and persists the disposition signals for later rubric refinement. The
classifier never invents a hybrid label. This matches the spec rather than overclaiming.

**Structural pass-through signal (codex P2.3).** To stop the deferral window from *systematically*
under-labeling — sub-agent summaries *are* fed into the synthesis context on both enforced and
autonomous paths (`executor.py:1746`, `:2778`) — the row carries a programmatic boolean
`delegate_result_passed_to_synthesis` alongside the per-sub disposition in `sub_agents JSONB`
(`success`, `summary_chars`, `output_chars`). This is the *structural* fact (result reached the
synthesis step), distinct from the *hybrid* judgement (genuinely incorporated). M2's rubric
refines `delegate_called` → `used`/`discarded` from this signal without re-running turns.

---

## 4. Files

### New module: `src/personal_agent/observability/route_trace/`
(sits beside existing `observability/{joinability,slm_health,cache_erosion}`)

1. **`types.py`** — `OrchestrationEvent` literal; frozen `@dataclass(frozen=True) RouteTraceRow`
   with all §2 fields + `schema_version: int = 1`. **Null/unknown semantics are explicit
   (codex P2.6):** path-dependent fields are `Optional` (`task_type`, `complexity`,
   `decomposition_strategy`, `model_role`, `latency_*` are `None` when their producer didn't run);
   `gateway_label` is `"unknown/unknown"` when `gateway_output is None`. Google docstrings.
2. **`classifier.py`** — `classify_orchestration_event(ctx) -> OrchestrationEvent` (pure, §3);
   tolerates missing `gateway_output`/expansion fields (no subs → `primary_handled`).
3. **`assembler.py`** — `assemble_route_trace(ctx, *, authoritative_cost_usd, input_tokens,
   output_tokens, store_preview, preview_chars) -> RouteTraceRow` (pure, no I/O; **the interim
   primary-turn adapter** — see §1 seam-neutrality). Defensively handles `None` `gateway_output`,
   `selected_model_role`, `request_timer`, expansion fields; sets `gateway_label`,
   `cost_reconciled = abs(live-authoritative) <= 0.0005`, latency breakdown from `request_timer`
   (or `None`), `user_message_sha256`, and `user_message_preview` only when `store_preview`.
4. **`ledger.py`** — `RouteTraceLedger` service:
   - `connect()/disconnect()` (asyncpg pool, mirrors `CostTrackerService`).
   - **`write(row: RouteTraceRow) -> None`** — **takes the DTO, never `ctx`** (seam-neutral, §1);
     raises `MissingIdentityError` if `trace_id`/`session_id` is None (ADR-0074); INSERT with
     **`ON CONFLICT (trace_id) DO NOTHING`** (idempotent against double-writes — codex P2.5).
   - `get_by_trace_id(trace_id: UUID) -> RouteTraceRow | None` — read-back.
   - module-level singleton `route_trace_ledger` (mirrors `cost_tracker` pattern).
5. **`__init__.py`** — exports.

### Emit site: `src/personal_agent/orchestrator/executor.py` (terminal-path rigor — codex P1)
- Add `_write_route_trace(ctx) -> None`: queries `SUM(cost_usd), SUM(input_tokens),
  SUM(output_tokens)` from `api_costs WHERE trace_id`, calls `assemble_route_trace`, then
  `route_trace_ledger.write(...)`. **Best-effort:** wrapped in `try/except Exception` (catches
  `Exception`, **not** `BaseException`/`CancelledError`, so cancellation still propagates) with
  `log.warning("route_trace_write_failed", trace_id=...)` — never breaks the turn; bus-independent
  (ADR-0088 D8). Docstring marks it the ADR-0088 D6 *direct durable sink (interim primary-turn
  adapter)*.
- **Single emit site, `finally`-guarded.** First confirm `execute_task`'s actual structure
  (multiple `return ctx` sites exist; the state loop is wrapped in `try/except Exception`, and the
  *public wrapper* has its own fatal `except` near `executor.py:3560`). Wrap the state-machine
  body so the write runs in a `finally` that fires on **success, `Exception`, and
  `asyncio.CancelledError`** (finally runs on cancellation; the write itself must complete
  promptly — it is a single fast INSERT, no inner `await` that re-suspends on the cancelled task;
  if needed, `asyncio.shield` the write). All `return ctx` sites inside the wrapped region pass
  through that one `finally`.
- **Wrapper-level fatal path (`executor.py:3560`):** if a failure escapes `execute_task` *before*
  the wrapped region (degenerate — `ctx` not yet populated), no row is written; this is noted as a
  known gap, not silently claimed covered.
- **One row per turn:** single terminal call + `ON CONFLICT (trace_id) DO NOTHING` (codex P2.5) —
  idempotent even if a future seam adds a second write site at turn granularity. (When the seam
  emits *per-topology* rows, the key migrates to `(trace_id, task_id)` — follow-up, §8.)

### Schema: `docker/postgres/`
- `migrations/0009_route_trace_ledger.sql` — `CREATE TABLE IF NOT EXISTS route_traces (...)` with
  a **`UNIQUE (trace_id)`** constraint (turn-level; backs `ON CONFLICT` — codex P2.5), nullable
  `task_id UUID` column (forward slot for the future per-topology `(trace_id, task_id)` key),
  `pedagogical_outcomes JSONB NULL`, + indexes on `session_id`, `created_at DESC`, `task_type`,
  `orchestration_event`.
- `init.sql` — same `route_traces` block (canonical fresh-DB schema; mirror the migration).
  **No Alembic** (project rule).
- **Source-of-truth boundary (codex P3):** Postgres `route_traces` is the ledger of record
  (synchronous, bus-independent — ADR-0088 D6/D8). Any future ES/Kibana view is a *derived
  analytics surface*, not the ledger — out of scope here.

### Lifecycle wiring: `src/personal_agent/service/` (app lifespan)
- Locate where `cost_tracker.connect()` is called at startup/shutdown; add
  `route_trace_ledger.connect()/disconnect()` alongside (TDD/grep during impl).

### Config: `src/personal_agent/config/`
- Add `route_trace_store_preview: bool = False` (PII gate — default OFF, codex P1) and
  `route_trace_preview_chars: int = 280` to `AppConfig` (`AGENT_` prefix).

---

## 5. Tests (TDD — write first, watch fail, implement)

Mirror `src/` layout under `tests/personal_agent/observability/route_trace/`.

1. **`test_classifier.py`** — synthetic `ctx` for each event: no subs → `primary_handled`;
   healthy subs → `delegate_called` (+ `delegate_result_passed_to_synthesis` set); phase failure
   / all-subs-failed → `fallback_triggered`. Asserts the honest programmatic floor (no
   used/discarded fabrication) and tolerates `gateway_output=None`.
2. **`test_assembler.py`** — field mapping from a fully-populated fake `ctx`; `gateway_label`
   format; `cost_reconciled` tolerance; latency breakdown from a stub `RequestTimer`;
   `pedagogical_outcomes is None`. **PII gate:** `store_preview=False` → `user_message_preview is
   None` + `user_message_sha256` set; `store_preview=True` → preview truncated at `preview_chars`.
   **Null-path cases (codex P2.6):** `gateway_output=None` (pre-gateway), `selected_model_role=None`
   (pre-LLM), `request_timer=None` (failure-before-synthesis) all assemble a valid row with
   explicit `None`/`"unknown"` fields, no exception.
3. **`test_ledger.py`** — `write` raises `MissingIdentityError` on null `trace_id`/`session_id`
   (unit, mocked pool asserting the guard fires before SQL); INSERT SQL shape + `ON CONFLICT
   (trace_id) DO NOTHING` via a mocked `asyncpg` connection. `@pytest.mark.integration` round-trip
   (write → `get_by_trace_id`; double-write → one row) against the test substrate (Postgres :5433,
   FRE-375) — **not run in-agent**.
4. **`test_executor_route_trace.py`** — `execute_task` writes exactly one row on (a) success and
   (b) a forced-`Exception` path (mock `route_trace_ledger.write`; assert row's
   `orchestration_event`/`fallback_triggered`). **(c) `asyncio.CancelledError` path:** the
   `finally` still writes and the `CancelledError` still propagates. **(d) best-effort:** a
   `route_trace_ledger.write` that raises `Exception` does not break the turn (turn result intact).

**Test commands (exact):**
```bash
make test-file FILE=tests/personal_agent/observability/route_trace/test_classifier.py
make test-file FILE=tests/personal_agent/observability/route_trace/test_assembler.py
make test-file FILE=tests/personal_agent/observability/route_trace/test_ledger.py
make test-file FILE=tests/personal_agent/orchestrator/test_executor_route_trace.py
make test            # full unit suite (one pytest at a time — hook enforces)
```
Expected: all selected tests pass; full suite green (no new failures).

---

## 6. Quality gates (all before PR)
```bash
make mypy            # uv run mypy src/  → no new errors
make ruff-check
make ruff-format
pre-commit run --all-files   # check_no_personal_paths + no-direct-substrate guards
```

## 7. ADR status edit (per owner instruction this session)
ADR-0088 `Status: Proposed → Accepted` already applied to
`docs/architecture_decisions/ADR-0088-execution-topology-observability-contract.md`. The README
index + MASTER_PLAN status references are **master's** domain on merge — flagged in the PR, not
edited here.

## 8. Follow-up tickets to file (Needs Approval, Observability Foundation)
- **ADR-0088 spine — `observe_topology` seam + projector + `report_degradation`** (the rest of
  the contract; calls this ledger's `write` at the seam; removes FRE-501 accumulation).
- **Route-trace REST read surface** (expose `get_by_trace_id` via the gateway observations API).
- **Hybrid `delegate_result_used`/`discarded` refinement** (M2 rubric + any harness incorporate
  flag) — depends on FRE-453 eval set.
- One-line ADR-0084 §D4 → taxonomy-spec back-reference (ADR session; noted in taxonomy §8).

## 9. PR — then STOP
Open PR with `.github/PULL_REQUEST_TEMPLATE.md`, **pre-merge checklist only** (post-deploy /
telemetry items go in a Linear comment, per lifecycle rules). Push branch. **Do not merge,
deploy, close the ticket, or edit MASTER_PLAN** — master's role.

---

## Acceptance criteria

### Pre-merge
- [ ] `route_traces` in `init.sql` + `migrations/0009_route_trace_ledger.sql` with `UNIQUE(trace_id)` + nullable `task_id`; no Alembic.
- [ ] `RouteTraceRow` frozen; `OrchestrationEvent` literal matches taxonomy §3 exactly; path-dependent fields `Optional` with explicit null/unknown semantics.
- [ ] `classify_orchestration_event` returns only the programmatic floor; `delegate_result_passed_to_synthesis` structural signal stored; no fabricated hybrid labels.
- [ ] `assemble_route_trace` pure (no I/O), the interim primary-turn adapter; `pedagogical_outcomes` nullable & uncomputed; survives `None` gateway/model/timer.
- [ ] Ledger `write` takes the DTO (never `ctx`, seam-neutral); identity-guarded (`MissingIdentityError`); `ON CONFLICT (trace_id) DO NOTHING`; direct Postgres write (bus-independent).
- [ ] PII gate: preview default OFF (`route_trace_store_preview=False`), only `sha256`+counts stored unless flipped; exposure documented in `ledger.py`.
- [ ] One write per turn at `execute_task` terminal across success / `Exception` / `CancelledError`; best-effort (never breaks the turn); wrapper-fatal gap noted.
- [ ] Gateway label + orchestration event both stored (shell-boundary field).
- [ ] Tests 1–4 (incl. cancellation + null-path + conflict cases) pass; full unit suite green; mypy/ruff/pre-commit clean.
- [ ] ADRs/spec linked in commit + PR; ADR-0088 status edit noted.

### Post-merge (Linear comment — master)
- [ ] After deploy, confirm a live turn writes a `route_traces` row joinable to `api_costs` on `trace_id`.
- [ ] Joinability probe finds no orphan route-trace rows.

### Future-gated (follow-up tickets, §8)
- [ ] `observe_topology` seam calls this writer; sub-agent/decompose topologies emit rows.
- [ ] Hybrid used/discarded refinement once FRE-453 eval set + rubric exist.
