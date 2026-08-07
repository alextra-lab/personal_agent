# FRE-1065 — Bridge `TraceContext` to OTel context without breaking identity-scoped behaviour

**Ticket:** FRE-1065 (Approved, `Tier-1:Opus`, `stream:build2`)
**Backing ADR:** ADR-0129 D1 (design intent only — its criteria belong to its seam ticket FRE-1073, ADR-0130 D1/D2)
**Related:** ADR-0064 (per-user scoping) · FRE-229 / FRE-673 (visibility filter) · FRE-375 (eval substrate isolation) · FRE-1064 (B1, already merged + deployed)

---

> **Revision 2 (2026-08-07)** — reworked after adversarial codex plan-review returned
> *"Needs rework"*. Changes: the `span_id` property gains a same-trace consistency guard (§3 D-d,
> was a real cross-trace defect); the format-change claim is corrected and its observable call
> sites enumerated (§3 D-c, §5 R4); docstring updates are made explicit (§4 step 5); test coverage
> extended to span exit / nesting / async / direct construction (§4 step 2); provider isolation
> stated (§4 step 2); the scope sentence corrected (§1). Codex's OTel-absence concern is **closed as
> moot** — verified below.

## 1. What is actually being changed

One **source** file: `src/personal_agent/telemetry/trace.py`. Also added: one test file, and an
eight-line `ast-grep` rule that makes AC-7's census reproducible at master's gate and for the two
downstream chain tickets (FRE-1067, FRE-1069) that inherit the same "do not remove signatures"
constraint. No other source file is touched.

`TraceContext` stops minting its own trace identity and instead **reads it from the active
OpenTelemetry span** when one exists. It **retains** `user_id`, `session_id`, `kind`, `eval_mode`
and `authenticated` verbatim — those five fields are load-bearing outside telemetry and preserving
them is this ticket's own work.

Nothing else moves. No signature is changed or removed. This is a bridge, not a flag day (AC-7).

---

## 2. Findings from the pre-implementation census (all verified, not assumed)

| # | Finding | How it was verified | Consequence for the design |
|---|---|---|---|
| F1 | `ast-grep run -p 'trace_ctx: TraceContext'` returns **0** matches — it parses as an `assignment`, not a `typed_parameter` | `--debug-query=ast` dump | The census must be **rule-based** (`kind: typed_parameter`). The naive pattern is the exact wrong-node-kind trap `.claude/CLAUDE.md` §3b warns about |
| F2 | The real census is **24 signatures**, not the ticket's 19 | rule-based `ast-grep scan` on `origin/main` @ `6a721464` | The ticket's 19 was censused 2026-07-30; the tree moved. AC-7's operative test is *"the same set as on `origin/main`"*, so **24 is the baseline** |
| F3 | `TraceContext(...)` is constructed **11×in `src/`, 59× in `tests/`** | rule-based `ast-grep scan` | `trace_id` **must remain a dataclass field**. Converting it to a property is a flag day and fails AC-7 |
| F4 | `trace_id` is a Postgres **`UUID`** column in 8+ tables (`api_costs`, `metrics`, `route_traces`, `captains_log_captures` *with an FK*, `budget_reservations`, `consolidation_attempts`) | `docker/postgres/init.sql` | The chosen string form must be UUID-coercible |
| F5 | Postgres **accepts undashed 32-hex** as `UUID` and normalizes it to dashed on read | live `psql` on `build-postgres-test-1`: `'4bf92f3577b34da6a3ce929d0e0e4736'::uuid` → `4bf92f35-77b3-4da6-a3ce-929d0e0e4736` | Writes and parameterized `WHERE trace_id = $1` both coerce, so hex is safe on the Postgres side |
| F6 | An OTel **invalid** span context yields an all-zero trace id, which coerces to the **nil UUID** | same `psql` probe | The no-span path must **never** adopt the zero id — it would collide on every row and break `captains_log_captures.trace_id NOT NULL UNIQUE` |
| F7 | FRE-1064's already-merged structlog processor writes **undashed 32-hex** (`format(tid, "032x")`) to every log record and **overwrites** any explicitly-bound `trace_id` | `telemetry/logger.py:147-153`, processor is first in the chain | ES already carries hex today. Choosing hex makes `TraceContext` **agree** with the log record; choosing dashed re-creates the divergence AC-6 exists to end |
| F8 | `span_id` appears in **no** Postgres column — it reaches ES only | `grep` over `init.sql` + `migrations/` | 16-hex span ids carry no schema risk |
| F9 | `SystemTraceContext.new()` has **44 call sites**, ~20 inside served HTTP handlers (`service/`, `gateway/`) that now run under FRE-1064's root span | `grep` by file | Adopting the ambient span there makes several system contexts in one request share a trace id — assessed in §5 R2 |
| F10 | `opentelemetry-sdk>=1.44.0` is a **hard dependency** (`pyproject.toml:65`), imported unconditionally at module scope in `service/app.py:1558` | file read | The OTel API is guaranteed present. **Closes codex's "package absence" concern as moot** — but the `trace.py` module docstring's claim of working *"without requiring the full OTel SDK"* is now false and must be corrected (§4 step 5) |
| F11 | Python's `uuid.UUID()` parses **undashed** 32-hex | live `python3` probe | `_to_uuid()`-style coercion (`walk.py:418`) is unaffected by the format choice |
| F12 | Exactly **two** call sites are sensitive to the dashed→undashed rendering, both cosmetic: `captains_log/manager.py:101` (`trace_id[:8]` display prefix) and `ui/cli.py:100` (`trace_id[:36]` display truncation; 32-hex never truncates) | `grep` for slicing/regex/length assumptions over `trace_id` | Neither breaks. No validator, regex or dash-splitting exists anywhere over `trace_id` |

---

## 3. Design decisions

**D-a — `trace_id` stays a field.** Per F3. The bridge happens at the *mint points*, which are the
only places identity is created.

**D-b — Both factories read the active span.** `TraceContext.new_trace()` and
`SystemTraceContext.new()` adopt the active span's trace id when the span context `is_valid`, and
mint otherwise. Reading in both places is what stops the divergence: the structlog processor stamps
records from the *active span* (F7), so a factory that mints alongside a live span guarantees the
context and its own log records disagree — precisely the failure AC-6 names.

**D-c — The string form is lowercase 32-hex, everywhere.** `format(trace_id, "032x")`, matching
FRE-1064's processor and Tempo (F7). The mint fallback uses `uuid.uuid4().hex` so a *read* id and a
*minted* id are indistinguishable in shape, and there is no second flag day when FRE-1069 gives
background paths real spans.

**The rendering change is observable and is being made deliberately** — minted ids go from dashed
`str(uuid.uuid4())` to undashed hex. Codex was right to reject the earlier "no caller can tell"
phrasing. What is actually claimed, and verified: no caller *breaks*. Postgres accepts and coerces
both forms on write and in parameterized predicates (F5); `uuid.UUID()` parses both (F11); and the
only two format-sensitive call sites in the tree are display-only and unaffected (F12).

**D-d — Add a read-only `span_id` property, guarded to the same trace.** AC-6 asserts
`TraceContext.span_id` equals the active span's. There is no `span_id` field today (only
`parent_span_id`), so this is additive.

A naive live-reading property is **wrong**, and codex's review caught it: `trace_id` is captured at
factory time while a live `span_id` is read at access time, so a context that outlives its span,
crosses into an unrelated span, or was directly constructed (11× in `src/`) would report a
`trace_id` and a `span_id` **belonging to different traces** — manufacturing exactly the divergence
AC-6 exists to end.

The property therefore returns the active span's id **only when that span's trace id equals
`self.trace_id`**, and `None` otherwise. Consistency becomes structural rather than a caller's
obligation: the pair is either from one trace or the span half is absent. `None` is the honest
answer for "this context is not currently inside its own trace", and ADR-0129 D8 drops sentinels
rather than inventing one.

**`parent_span_id` is left untouched** — its existing "current span" convention
(`llm_client/telemetry.py:40-43`) is relied on by callers and re-defining it is not this ticket's
decision.

**D-e — The five identity fields are not touched at all.** No field is renamed, reordered, defaulted
differently or dropped. This is what AC-1…AC-5 assert, and the strongest form of the guarantee is
that the diff simply does not touch them.

**D-f — Never adopt an invalid span context.** Per F6, `is_valid` is checked before reading;
otherwise mint. This is the guard that keeps the nil UUID out of the database.

---

## 4. Implementation steps (TDD — failing test first, confirm it fails, then implement)

| # | Step | Verify |
|---|---|---|
| 1 | Commit the census rule file `.ast-grep/trace_ctx_census.yml` (F1's correct instrument) and snapshot the `origin/main` baseline | `ast-grep scan -r .ast-grep/trace_ctx_census.yml src/` → 24 matches |
| 2 | Write `tests/test_telemetry/test_trace_otel_bridge.py` (coverage list below) | `make test-file FILE=tests/test_telemetry/test_trace_otel_bridge.py` → **fails** |
| 3 | Implement D-a…D-f in `trace.py` | the new file passes |
| 4 | Re-run the pre-existing trace suite unchanged | `make test-file FILE=tests/test_telemetry/test_trace.py` → all pass, **no test edited** |
| 5 | Correct the now-false docstrings in `trace.py`: the module header's *"without requiring the full OTel SDK"* (false per F10), `new_trace`'s *"generated trace_id"* and `SystemTraceContext.new`'s *"freshly generated trace_id"* (both now read-then-mint), and document the new `span_id` property + its same-trace guard | `make ruff-check`; docstrings match behaviour |
| 6 | Run the behavioural anchor suites for AC-1…AC-5 (visibility, scoping, session isolation, eval isolation, kind) | listed in §6 → all pass |
| 7 | AC-7 census diff: run the rule on the branch, diff against the `origin/main` baseline | empty diff |
| 8 | Full `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files` | all clean |

Step 4 carries real weight: **if a pre-existing test needs editing to pass, the bridge changed
behaviour and the design is wrong.** Not editing them is the check.

### Step 2 — required test coverage

Codex correctly flagged that asserting only "access immediately inside one span" would not exercise
the lifetime cases where the D-d guard earns its place. The file must cover:

1. **AC-6 happy path** — inside an active span, `trace_id` and `span_id` both equal that span's.
2. **Direct construction under an active span** — a hand-built `TraceContext(trace_id="…")` whose id
   is *not* the ambient span's reports `span_id is None`, never the foreign span's id.
3. **After the originating span closes** — `trace_id` is retained; `span_id` becomes `None`.
4. **Nested spans** — inside a child span of the same trace, `span_id` tracks the *current* span and
   `trace_id` is unchanged; the pair stays same-trace.
5. **Unrelated sibling trace** — inside a span of a *different* trace, `span_id is None`.
6. **Async task boundary** — the context read inside an `asyncio` task started outside the span
   behaves per (3), confirming OTel's contextvar propagation is respected rather than assumed.
7. **D-f invalid context** — with no span active, neither factory adopts the all-zero id; each mints
   a valid, unique, 32-hex id.
8. **D-c format** — read and minted ids are both 32 lowercase hex chars and `uuid.UUID()`-parseable.
9. **Field retention (AC-1…AC-5)** — a context built inside a span still carries `user_id`,
   `session_id`, `kind`, `eval_mode`, `authenticated`; and `SystemTraceContext.new()` still yields
   `kind="system:<source>"` with `is_system` true.

**Provider isolation** (codex's C6): tests construct a local `TracerProvider` with an
`InMemorySpanExporter` and take a tracer from it directly. `trace.set_tracer_provider()` is **never**
called, so no global state is mutated and no ordering dependence on `configure_tracing()` is
introduced — `start_as_current_span` sets the ambient context regardless of which provider minted the
tracer, which is the only mechanism the bridge reads.

---

## 5. Risks

**R1 — Postgres renders trace ids dashed on read; ES stores them hex.** So a value *read back from
Postgres* will not string-equal the same trace's ES value. This affects the joinability probe's
`unknown_in_es = es_trace_ids - trace_ids` comparison (`observability/joinability/walk.py:672`).

- **This is pre-existing, and not caused by this ticket** — FRE-1064 already put hex into ES while
  `api_costs` kept dashed UUIDs. The bridge does not widen it.
- It is a **yellow, non-escalating** informational orphan by that code's own design (walk.py:667-683),
  not a red gate failure — so it does not trip the "joinability probe finds orphans" halt condition.
- Postgres-side equality is unaffected: `= ANY($1::uuid[])` (walk.py:418) coerces both sides (F5).
- **Disposition: file a `Backlog` ticket** for cross-substrate `trace_id` normalization. It is a real
  finding, it belongs to the ADR-0129 correlation work (AC-2 trace-to-logs), and it is not needed to
  make this build function — so per build skill §5 it is filed, not folded in.

**R2 — System contexts inside one request now share the request's trace id.** Under D-b, the ~20
`SystemTraceContext.new()` call sites in served handlers (F9) adopt the root span's id instead of
minting distinct ones. Checked for collision against the two `UNIQUE` constraints on `trace_id`:

- `route_traces UNIQUE NULLS NOT DISTINCT (trace_id, task_id)` — the only writer is
  `observability/route_trace/ledger.py:42`, reached from the per-turn `topology/seam.py` path with
  the **turn's** context, once per turn. Not reachable twice from the handler system-context paths.
- `captains_log_captures.trace_id NOT NULL UNIQUE` — written from the captain's-log turn path, not
  from `knowledge_api` / `route_trace_api` / `auth` / `app` handlers (verified: those files
  reference neither table).

Sharing the id is also the **correct** outcome — it is genuinely the same trace — and `kind` still
separates system from organic, which is exactly what AC-5 asserts.

**R3 — `test_new_each_call_yields_unique_trace_id` (`test_trace.py:126`).** Passes unchanged: no
span is active in unit tests, so both calls mint. Confirms the fallback path stays live.

**R4 — the minted-id rendering change is user-visible in two display paths.** Per F12,
`captains_log/manager.py:101` builds a `trace_id[:8]` prefix and `ui/cli.py:100` truncates at 36
chars. With 32-hex the prefix is still 8 distinct chars and the truncation never fires. Cosmetic
only; no correctness or storage consequence. Recorded because "observable but harmless" is a claim
that has to be enumerated to be believed, not asserted.

---

## 6. Acceptance criteria → proof mapping

| AC | Proven by |
|---|---|
| AC-1 `authenticated` still gates group-visibility memory | `tests/test_tools/test_memory_search.py`, `tests/test_memory/test_structural_arm.py` pass **unedited**, + a bridge-level test that a context built inside a span retains `authenticated` |
| AC-2 `user_id` still scopes rows | `tests/personal_agent/orchestrator/test_identity_precedence.py` + `test_executor.py` pass unedited, + field-retention test |
| AC-3 `session_id` still isolates history | existing session-isolation suites pass unedited, + field-retention test |
| AC-4 `eval_mode` still isolates substrate | the FRE-375 pre-commit substrate guard + `tests/test_orchestrator/test_sub_agent_eval_provenance.py` pass unedited |
| AC-5 `kind` still separates scheduled from organic | `test_trace.py::TestSystemTraceContext` passes unedited; `is_system` unchanged; bridge test asserts `kind` survives span adoption |
| AC-6 reads identity rather than minting it | new test with an **in-memory span exporter**: inside an active span, `ctx.trace_id == format(span.trace_id,"032x")` and `ctx.span_id == format(span.span_id,"016x")`; plus §4 step-2 cases 2–6, which prove the pair can never be sourced from two different traces |
| AC-7 bridge, not a flag day | rule-based `ast-grep` census diff vs `origin/main` → empty (24 = 24) |

---

## 7. Out of scope (stated, not silently dropped)

- Creating real OTel spans for steps / model calls / tool calls — **FRE-1067 (B3)**.
- Root spans on background entrypoints — **FRE-1069 (B4)**.
- Retiring `RequestTimer` — FRE-1067.
- Removing any `trace_ctx: TraceContext` signature — explicitly forbidden by AC-7.
- Cross-substrate `trace_id` normalization — R1, filed to `Backlog`.
