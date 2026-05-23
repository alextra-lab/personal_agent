# FRE-376 Phase 3 Implementation Plan — Identity Threading + Back-Compat Cleanup

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Thread `(trace_id, session_id, span_id, parent_span_id)` through every `log.*`, `bus.publish`, and Cypher `MERGE` site (ADR-0074 §I3, §I5); drop the Phase 2 back-compat aliases; reconcile the `executor.py:1640` two-event-name collision.

**Architecture:** Build an AST-based identity-threading lint (`scripts/check_identity_threaded.py`, pulled forward from Phase 5) — the lint becomes the definition of done. Fix every violation it flags (with a small frontmatter-style allowlist for legitimate lifecycle/sensor sites). Rename the orchestrator's step-planning event so `model_call_*` is exclusively model-client telemetry. Remove the legacy emit helpers and dashboard fields in one atomic PR so visibility never breaks.

**Tech Stack:** Python 3.12 · `ast` (stdlib) · structlog · Redis Streams (event bus) · Neo4j (Cypher MERGE writes) · Elasticsearch index templates · Kibana NDJSON dashboards.

---

## Context

ADR-0074 phases 1, 2, 4a, 4b shipped. Phase 1 made schema columns NOT NULL; Phase 2 equalized `LocalLLMClient` and `LiteLLMClient` telemetry through a shared helper (`src/personal_agent/llm_client/telemetry.py`); 4a/4b tightened `TraceContext` non-optional at API boundaries.

Phase 2 deliberately left three forms of debt behind, each labeled "Phase 3 removes":

1. **Field aliases** in canonical emitters: `model_id`, `prompt_tokens`, `completion_tokens` co-emitted alongside `model`, `input_tokens`, `output_tokens` (`telemetry.py:84-85`, `136-138`).
2. **Legacy event names**: `litellm_request_start` / `litellm_request_complete` still co-emit so existing Kibana dashboards keep returning hits (`telemetry.py:146-216`, called from `litellm_client.py:309-319` and `486-503`). Includes secondary aliases: `tokens` (= `total_tokens`), `cache_write_tokens` (= `cache_creation_input_tokens`).
3. **Event-name collision**: `executor.py:1640` emits `MODEL_CALL_STARTED` with a thin orchestrator-step shape (`trace_id`, `span_id`, `model_role`, `channel`) — the same event name the model clients now emit with the full canonical shape. Two unrelated emit sites under one event name = ambiguous Kibana queries.

Phase 3 also begins the cross-substrate identity work that Phase 5's `joinability_probe.py` will verify. ADR-0074 §I3 ("Every async boundary preserves identity") and §I5 ("Memory writes carry origination") require:
- Every `bus.publish` carries `trace_id` + `session_id` in the event envelope.
- Every Cypher `MERGE`/`CREATE` on `:Turn`, `:Entity`, `:Relationship`, future `:DescriptionVersion` writes `originating_trace_id`, `originating_session_id`, `extractor_model` properties.
- Every request-scoped `log.*` call carries `trace_id` (and `session_id` where relevant) so logs join cleanly to traces in Kibana.

The audit surface is large — **21** `bus.publish` sites (mix of `bus.publish`, `self._bus.publish`, `self._event_bus.publish`, `get_event_bus().publish`), 13 Cypher `MERGE` writes in `memory/service.py` (12 are AST string literals; 1 at `:663` is dynamically concatenated), and ~285 `log.*` calls across hot paths (executor 72, memory 60, brainstem/scheduler 29, plus llm_client, captains_log, second_brain, gateway, events, telemetry, service). Manual sweep won't be reliable. The plan builds the AST lint first, then uses its output as the work-list.

**Out of scope (separate Phase 2 debt, leave for a follow-up FRE):**
- `captains_log/capture.py:61` — `CaptureEntry` dataclass has `prompt_tokens` / `completion_tokens` field names. This is internal schema for Captain's Log capture, not event-bus telemetry. Renaming is a substrate-schema change with replay implications — out of Phase 3 scope.
- `executor.py:1889` — reads `usage.prompt_tokens` / `usage.completion_tokens` directly off the upstream LLM provider response (LiteLLM/OpenAI native field names). These are NOT Phase 2 aliases — they're how the provider returns usage. Leave alone.

---

## File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `scripts/check_identity_threaded.py` | Create | AST lint: flag `log.*`/`bus.publish`/Cypher `MERGE` missing identity kwargs. Reads allowlist. |
| `scripts/identity_threading_allowlist.yaml` | Create | Frontmatter-style allowlist: `path:line` entries with `reason:` field. |
| `tests/scripts/test_check_identity_threaded.py` | Create | Unit tests for the lint (good/bad fixture files). |
| `src/personal_agent/llm_client/telemetry.py` | Modify | Remove `emit_legacy_litellm_*` (146-216); strip `model_id`/`prompt_tokens`/`completion_tokens` aliases from canonical emitters (84-85, 135-138). |
| `src/personal_agent/llm_client/litellm_client.py` | Modify | Drop calls to `emit_legacy_litellm_start`/`emit_legacy_litellm_complete` (309-319, 486-503) and their imports. |
| `src/personal_agent/orchestrator/executor.py` | Modify | Rename `MODEL_CALL_STARTED` emit at 1639-1643 to new `STEP_PLANNING_STARTED`; add matching `STEP_PLANNING_COMPLETED` at end of step; thread `session_id`. Then thread identity into the rest of the 66 `log.*` calls flagged by the lint. |
| `src/personal_agent/telemetry/events.py` | Modify | Add `STEP_PLANNING_STARTED`, `STEP_PLANNING_COMPLETED` constants. |
| `src/personal_agent/memory/service.py` | Modify | Add `originating_trace_id`, `originating_session_id`, `extractor_model` to `:Turn` (368), `:Entity` (428, 665) MERGE statements; thread identity into 65 `log.*` calls flagged by the lint. |
| `src/personal_agent/orchestrator/executor.py:911`, `events/pipeline_handlers.py:155,181,771`, `brainstem/scheduler.py:361,562,670`, `brainstem/mode_manager.py:241`, `brainstem/jobs/freshness_review.py:411`, `captains_log/{manager,promotion}.py`, `second_brain/consolidator.py`, `telemetry/{error_monitor,within_session_compression,context_quality}.py`, `service/app.py:1420`, `gateway/chat_api.py:114`, `brainstem/sensors/metrics_daemon.py:146` | Modify | Thread identity into the **21** `bus.publish` sites. |
| `config/kibana/setup_dashboards.py:46,272,294,305,366,375,390,399` | Modify | Update the dashboard **generator** (not just the NDJSON output) so regeneration doesn't regress canonical field names. |
| `tests/evaluation/run_primitive_tools_eval.py:221-300,446-453` | Modify | Eval harness aggregates on `litellm_request_complete`, `prompt_tokens`, `completion_tokens`, `cache_write_tokens`. Re-point to `model_call_completed`, `input_tokens`, `output_tokens`, `cache_creation_input_tokens`. |
| `tests/personal_agent/llm_client/test_litellm_gate_wiring.py:135`, `tests/test_orchestrator/test_routing_delegation.py:50`, `tests/personal_agent/orchestrator/test_skill_injection.py:46` | Modify | Update tests that mock or assert on legacy field names beyond the two already named. |
| `src/personal_agent/orchestrator/executor.py:862` | Modify | Capability extraction reads `meta.get("prompt_tokens")` / `meta.get("completion_tokens")` / `meta.get("tokens")` from step metadata. Re-point to canonical names after confirming step metadata is now canonical (Phase 2 emits both, but step metadata may still carry only legacy). |
| `tests/personal_agent/llm_client/test_telemetry_parity.py` | Modify | Delete `TestLegacyBackCompat` class (305-373) and `test_completed_helper_keeps_legacy_token_aliases` (121-139). |
| `tests/personal_agent/llm_client/test_litellm_emit_payload.py` | Modify | Delete tests asserting on `prompt_tokens`, `completion_tokens`, legacy `tokens`, `cache_write_tokens`. |
| `docker/elasticsearch/index-template.json` | Modify | Remove explicit mappings for `model_id` (96), `prompt_tokens` (99), `completion_tokens` (100); keep `input_tokens`/`output_tokens`/`cache_creation_input_tokens`/`cache_read_tokens`. |
| `config/kibana/dashboards/llm_performance.ndjson`, `request_traces.ndjson`, `request_timing.ndjson`, `data_views.ndjson` | Modify | Rewrite aggregations: `model_id.keyword` → `model.keyword`; sum `prompt_tokens` → `input_tokens`; sum `completion_tokens` → `output_tokens`. Update saved queries from `event:litellm_request_*` → `event:model_call_*`. |
| `.pre-commit-config.yaml` | Modify | Add `check-identity-threaded` hook. |
| `docs/architecture_decisions/ADR-0074-cross-substrate-traceability.md` | Modify | Mark Phase 3 as Shipped (status table). |
| `docs/plans/MASTER_PLAN.md` | Modify | Update header + Last updated line on ship (per memory `feedback_update_master_plan.md`). |

---

## Acceptance Criteria

| Layer | Criterion |
|-------|-----------|
| Pre-merge | `make test` passes (including new `test_check_identity_threaded.py`). |
| Pre-merge | `make mypy` + `make ruff-check` clean. |
| Pre-merge | `python scripts/check_identity_threaded.py src/personal_agent/` exits 0 with current allowlist. |
| Pre-merge | `grep -r "litellm_request_start\|litellm_request_complete\|emit_legacy" src/` returns no hits. |
| Pre-merge | `grep -rn '"model_id"\|"prompt_tokens"\|"completion_tokens"' src/personal_agent/llm_client/` returns no hits. |
| Pre-merge | Test parity contract still passes; no test asserts on removed aliases. |
| Pre-merge | All 4 Kibana dashboards open in Kibana dev without "field not found" errors against the rewritten template. |
| Post-deploy | ES query `event:litellm_request_complete AND @timestamp:[now-1h TO now]` returns 0 hits (legacy emit fully gone). |
| Post-deploy | ES query `event:model_call_completed AND _missing_:trace_id` returns 0 hits. |
| Post-deploy | Cypher `MATCH (n:Turn) WHERE n.originating_trace_id IS NULL AND n.created_at > datetime() - duration('PT1H') RETURN count(n)` returns 0 (new writes carry the property). |
| Post-deploy | `event:step_planning_started` shows up in Kibana with non-zero count; `event:model_call_started` count drops to roughly = number of actual model calls (no longer inflated by orchestrator emits). |
| Post-deploy | Run-once probe `scripts/check_identity_threaded.py --strict src/personal_agent/` over a clean checkout = green. |
| Future gate | Phase 5 acceptance still depends on `scripts/monitors/joinability_probe.py`; Phase 3 lint is the static counterpart. |

---

## Task 1: Build the AST identity-threading lint

**Files:**
- Create: `scripts/check_identity_threaded.py`
- Create: `scripts/identity_threading_allowlist.yaml`
- Create: `tests/scripts/test_check_identity_threaded.py`

The lint flags any of these AST patterns missing required identity kwargs:

| Pattern | Required kwargs |
|---------|-----------------|
| `log.{info,debug,warning,error,exception,critical}(...)` inside an async def or a function reachable from request-scoped code | `trace_id=` |
| `bus.publish(...)` or `await bus.publish(...)` | `trace_id=`, `session_id=` (both in the event payload dict OR as kwargs) |
| String literal containing `MERGE (`, `MERGE (n:Turn`, `MERGE (n:Entity`, `MERGE (n:Relationship)`, `MERGE (n:DescriptionVersion)` | Must be followed within the same string by `SET n.originating_trace_id = $originating_trace_id` (or equivalent property assignment) |

Allowlist entry shape:

```yaml
- path: src/personal_agent/brainstem/sensors/platforms/apple.py
  line: 142
  pattern: log.info
  reason: macOS sensor lifecycle log — runs outside any request context
- path: src/personal_agent/service/app.py
  line: 87
  pattern: log.info
  reason: FastAPI startup log — no trace context exists yet
```

- [ ] **Step 1: Write the failing test for the lint**

```python
# tests/scripts/test_check_identity_threaded.py
from pathlib import Path
import textwrap
from scripts.check_identity_threaded import lint_file, Violation

def test_log_info_without_trace_id_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(textwrap.dedent("""
        import structlog
        log = structlog.get_logger(__name__)

        async def handle(ctx) -> None:
            log.info("did a thing", model="claude")
    """))
    violations = lint_file(src, allowlist=[])
    assert len(violations) == 1
    assert violations[0].kind == "log_missing_trace_id"
    assert violations[0].line == 6

def test_log_info_with_trace_id_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(textwrap.dedent("""
        import structlog
        log = structlog.get_logger(__name__)

        async def handle(ctx) -> None:
            log.info("did a thing", trace_id=ctx.trace_id, model="claude")
    """))
    assert lint_file(src, allowlist=[]) == []

def test_bus_publish_without_identity_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(textwrap.dedent("""
        async def emit(bus, payload) -> None:
            await bus.publish("stream:x", {"foo": "bar"})
    """))
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bus_publish_missing_identity" for v in violations)

def test_cypher_merge_turn_without_origination_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(textwrap.dedent('''
        async def write_turn(session, turn_id):
            await session.run("MERGE (t:Turn {turn_id: $turn_id}) SET t.created_at = datetime()")
    '''))
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "cypher_merge_missing_origination" for v in violations)

def test_allowlisted_violations_are_suppressed(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(textwrap.dedent("""
        import structlog
        log = structlog.get_logger(__name__)
        def lifecycle() -> None:
            log.info("startup")
    """))
    allowlist = [{"path": str(src), "line": 4, "pattern": "log.info", "reason": "lifecycle"}]
    assert lint_file(src, allowlist=allowlist) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/scripts/test_check_identity_threaded.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'scripts.check_identity_threaded'`

- [ ] **Step 3: Implement the lint**

Create `scripts/check_identity_threaded.py`:

```python
"""AST lint: flag log/bus.publish/Cypher MERGE sites missing identity kwargs.

Pulled forward from FRE-376 Phase 5. Becomes the definition-of-done for Phase 3.

Usage:
    python scripts/check_identity_threaded.py src/personal_agent/
    python scripts/check_identity_threaded.py --strict src/personal_agent/
"""
from __future__ import annotations

import argparse
import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import yaml

LOG_METHODS = {"info", "debug", "warning", "error", "exception", "critical"}
REQUIRED_LOG_KWARGS = {"trace_id"}
REQUIRED_BUS_KWARGS = {"trace_id", "session_id"}
ORIGIN_NODE_LABELS = ("Turn", "Entity", "Relationship", "DescriptionVersion")
ORIGIN_PROPS = ("originating_trace_id", "originating_session_id")


@dataclass(frozen=True)
class Violation:
    path: Path
    line: int
    kind: str
    detail: str


def _kwarg_names(call: ast.Call) -> set[str]:
    names: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None:
            # **kwargs spread — be permissive
            return {"<spread>"}
        names.add(kw.arg)
    # also accept identity present in a dict-literal first positional arg
    if call.args and isinstance(call.args[0], ast.Dict):
        for k in call.args[0].keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                names.add(k.value)
    return names


def _is_log_call(call: ast.Call) -> bool:
    # log.info(...) — receiver identifier must be literally `log` (convention)
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in LOG_METHODS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "log"
    )


def _is_bus_publish(call: ast.Call) -> bool:
    # Match any *.publish — covers `bus.publish`, `self._bus.publish`,
    # `self._event_bus.publish`, `get_event_bus().publish(...)`.
    # False-positive surface (e.g. ws.publish, queue.publish) is acceptable —
    # the lint scope is `src/personal_agent/` where the only `.publish(` callers
    # are the event bus. Add explicit allowlist entries if that changes.
    return isinstance(call.func, ast.Attribute) and call.func.attr == "publish"


def _bus_publish_identity_kwargs(call: ast.Call) -> set[str]:
    """Identity field names visible on a bus.publish call.

    Signature is `await bus.publish(stream, event_dict)` — payload is the
    SECOND positional arg, not the first. Also accept explicit kwargs.
    """
    names: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None:
            return {"<spread>"}
        names.add(kw.arg)
    payload_arg = call.args[1] if len(call.args) >= 2 else None
    if isinstance(payload_arg, ast.Dict):
        for k in payload_arg.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                names.add(k.value)
    elif isinstance(payload_arg, ast.Name):
        # Payload is a variable (e.g. `event`). Can't statically prove
        # identity is set. Flag — caller can refactor to dict-literal or add
        # an allowlist entry with reason.
        names.add("<opaque-var>")
    return names


_MERGE_RE = re.compile(
    r"MERGE\s*\(\s*\w+\s*:\s*(" + "|".join(ORIGIN_NODE_LABELS) + r")\b"
)


def _string_chunks(node: ast.AST) -> Iterable[str]:
    """Yield every str chunk reachable from `node` (constants + f-strings + concat + join).

    Covers dynamic Cypher built via:
      - `+ "..." +` BinOp concat
      - f-strings (`ast.JoinedStr`)
      - `"\\n".join([...])` method call (memory/service.py:663 case)
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _string_chunks(node.left)
        yield from _string_chunks(node.right)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        # "sep".join([...]) — best-effort: walk any List/Tuple arg and yield
        # constant string elements. Method-call args won't be inspected, but
        # for static query fragments this catches the common case.
        sep = node.func.value.value
        if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            elements: list[str] = []
            for elt in node.args[0].elts:
                elements.extend(_string_chunks(elt))
            yield sep.join(elements)


def _cypher_violations_for_query(text: str, lineno: int, path: Path) -> list[Violation]:
    if not _MERGE_RE.search(text):
        return []
    if all(prop in text for prop in ORIGIN_PROPS):
        return []
    return [Violation(path, lineno, "cypher_merge_missing_origination", text[:80])]


def lint_file(path: Path, allowlist: Iterable[dict]) -> list[Violation]:
    src = path.read_text()
    tree = ast.parse(src)
    violations: list[Violation] = []

    # Track Cypher strings encountered via concat (binop) so we only walk them once.
    visited_binops: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_log_call(node):
                kwargs = _kwarg_names(node)
                if "<spread>" not in kwargs and not REQUIRED_LOG_KWARGS.issubset(kwargs):
                    violations.append(Violation(path, node.lineno, "log_missing_trace_id", ""))
            elif _is_bus_publish(node):
                kwargs = _bus_publish_identity_kwargs(node)
                if "<spread>" not in kwargs and not REQUIRED_BUS_KWARGS.issubset(kwargs):
                    violations.append(Violation(path, node.lineno, "bus_publish_missing_identity", ""))
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            violations.extend(_cypher_violations_for_query(node.value, node.lineno, path))
        elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add) and id(node) not in visited_binops:
            visited_binops.add(id(node))
            joined = "".join(_string_chunks(node))
            if joined:
                violations.extend(_cypher_violations_for_query(joined, node.lineno, path))

    allow = {(item["path"], item["line"]) for item in allowlist}
    return [v for v in violations if (str(v.path), v.line) not in allow]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--allowlist", type=Path, default=Path("scripts/identity_threading_allowlist.yaml"))
    ap.add_argument("--strict", action="store_true", help="ignore allowlist")
    args = ap.parse_args()

    allowlist: list[dict] = []
    if not args.strict and args.allowlist.exists():
        allowlist = yaml.safe_load(args.allowlist.read_text()) or []

    total: list[Violation] = []
    for root in args.paths:
        files = [root] if root.is_file() else root.rglob("*.py")
        for f in files:
            total.extend(lint_file(f, allowlist))

    for v in total:
        print(f"{v.path}:{v.line}: {v.kind} {v.detail}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
```

Create empty allowlist:

```yaml
# scripts/identity_threading_allowlist.yaml
# Each entry exempts a single (path, line) violation. Add `reason:` so future
# auditors know why this site can't carry identity.
[]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/scripts/test_check_identity_threaded.py -v`
Expected: PASS — all 5 tests green.

- [ ] **Step 5: Commit**

```bash
git add scripts/check_identity_threaded.py scripts/identity_threading_allowlist.yaml tests/scripts/test_check_identity_threaded.py
git commit -m "feat: FRE-376 Phase 3 — add identity-threading AST lint (ADR-0074 §I3/§I5)"
```

---

## Task 2: Run lint on src/, baseline current violations

**Files:**
- Modify: `scripts/identity_threading_allowlist.yaml`

- [ ] **Step 1: Run lint and capture output**

```bash
uv run python scripts/check_identity_threaded.py src/personal_agent/ > /tmp/identity-violations.txt; echo "exit=$?"
wc -l /tmp/identity-violations.txt
sort /tmp/identity-violations.txt | awk -F: '{print $1}' | sort -u | head -40  # files
awk -F: '{print $3}' /tmp/identity-violations.txt | awk '{print $1}' | sort | uniq -c  # by kind
```

Expected: non-zero exit, ~350+ violations across hot-path modules (orchestrator, memory, brainstem, llm_client tests already clean).

- [ ] **Step 2: Triage**

Categorize every violation:
- **Fix**: request-scoped code where `trace_ctx` / `trace_id` is in scope or can be plumbed through. (Expected majority in `orchestrator/`, `memory/`, `events/`, `gateway/`, `request_gateway/`, top-level `bus.publish` sites.)
- **Allowlist**: legitimate context-free sites (startup, shutdown, periodic sensors not tied to a request). Each entry MUST have a `reason:` field.

Skip orchestrator/llm_client logs that ARE flagged but whose enclosing function takes `ctx: TraceContext` — those are fixable by threading.

- [ ] **Step 3: Populate the allowlist with the legitimate context-free sites only**

Reference candidates (based on exploration):
- `src/personal_agent/brainstem/sensors/platforms/apple.py` — 37 sensor lifecycle logs
- `src/personal_agent/brainstem/sensors/sensors.py` — 9 lifecycle logs
- `src/personal_agent/brainstem/sensors/platforms/base.py` — 5 lifecycle logs
- `src/personal_agent/service/app.py` — FastAPI startup/shutdown logs
- `src/personal_agent/brainstem/sensors/metrics_daemon.py` lifecycle calls (the bus.publish at :146 is request-adjacent — must thread, not allowlist)

Do NOT allowlist anything in `orchestrator/`, `memory/`, `llm_client/`, `gateway/`, `request_gateway/`, `events/`, `captains_log/`, `second_brain/`, `telemetry/` unless there is a concrete reason no identity is available.

- [ ] **Step 4: Commit the allowlist baseline**

```bash
git add scripts/identity_threading_allowlist.yaml
git commit -m "chore: FRE-376 Phase 3 — baseline identity-threading allowlist"
```

---

## Task 3: Fix all 21 bus.publish sites

**Files:** (full enumeration verified against current `src/`)
- `captains_log/manager.py:227`, `captains_log/promotion.py:365`
- `brainstem/mode_manager.py:241` (uses `self._event_bus.publish`)
- `brainstem/scheduler.py:361` (uses `get_event_bus().publish`), `:562`, `:670`
- `brainstem/jobs/freshness_review.py:411` (uses `get_event_bus().publish`)
- `brainstem/sensors/metrics_daemon.py:146`
- `telemetry/error_monitor.py:199` (uses `self._bus.publish`)
- `telemetry/within_session_compression.py:168`, `telemetry/context_quality.py:197`
- `orchestrator/executor.py:911` (uses `get_event_bus().publish` inside `_run_bg`)
- `memory/service.py:1283,1442`
- `gateway/chat_api.py:114`, `service/app.py:1420`
- `events/pipeline_handlers.py:155,181,771` (line 771 uses `get_event_bus().publish`)
- `second_brain/consolidator.py:222,242`

The pattern: every `bus.publish` payload (a dict) gets `trace_id` and `session_id` keys. Where the surrounding function already has a `TraceContext` parameter, use it. Where it doesn't, plumb `TraceContext` through the call chain — DO NOT pass `None` or insert `"unknown"` placeholders.

**Where payload is built as a variable** (e.g. `event = {...}; await bus.publish(stream, event)`), the lint flags `<opaque-var>` — the fix is to ensure the dict literal that constructs `event` has `trace_id`/`session_id` keys. The lint will pass once the lint enforcer is updated to recurse into the variable assignment in the same function scope (out of scope for v1 of the lint — for now, refactor to inline-dict OR add an allowlist entry with the assignment site referenced in `reason:`).

**Background-task publishes** (`executor.py:911` wraps `_run_bg(...)`) — identity must be captured at scheduling time, not via context-var lookup inside the background coroutine. Confirm `ctx.trace_id` / `ctx.session_id` are closed over before `_run_bg`.

- [ ] **Step 1: Write a failing contract test**

Create `tests/personal_agent/events/test_bus_publish_carries_identity.py`:

```python
"""Contract test: every bus.publish site in src/ carries trace_id+session_id."""
from pathlib import Path
import subprocess

SRC = Path("src/personal_agent")

def test_no_bus_publish_missing_identity() -> None:
    result = subprocess.run(
        ["uv", "run", "python", "scripts/check_identity_threaded.py", "--strict", str(SRC)],
        capture_output=True, text=True,
    )
    bus_violations = [
        line for line in result.stdout.splitlines()
        if "bus_publish_missing_identity" in line
    ]
    assert not bus_violations, "bus.publish sites missing identity:\n" + "\n".join(bus_violations)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/personal_agent/events/test_bus_publish_carries_identity.py -v`
Expected: FAIL — at least 21 bus.publish violations.

- [ ] **Step 3: Fix each bus.publish site**

For each of the 21 sites:
1. Open the file at the listed line.
2. Inspect the enclosing function signature — is `TraceContext` already a parameter?
3. If yes: add `"trace_id": ctx.trace_id, "session_id": ctx.session_id` to the publish payload dict.
4. If no: plumb `TraceContext` through the call chain (signatures + callers). Reuse the patterns established in Phase 4a/4b. Never default to `None`.

Concrete examples:

`gateway/chat_api.py:114` — already inside a FastAPI handler that has a request body with `session_id` and a generated `trace_id`. Add to payload.

`memory/service.py:1283,1442` — these are inside async methods that already accept identity. Add the kwargs.

`second_brain/consolidator.py:222,242` — consolidator runs on a schedule but is dispatched by a `consolidation_started` event; mint a `TraceContext` from the dispatch event (precedent: `request_gateway/pipeline.py` minting trace at entry) and propagate.

- [ ] **Step 4: Run contract test + smoke test**

```bash
uv run pytest tests/personal_agent/events/test_bus_publish_carries_identity.py -v
make test
```

Expected: PASS — zero bus.publish violations remain; full test suite green.

- [ ] **Step 5: Commit**

```bash
git add -p   # stage only the bus.publish fixes
git commit -m "feat: FRE-376 Phase 3 — thread identity through bus.publish sites (ADR-0074 §I3)"
```

---

## Task 4: Add originating_* properties to :Turn and :Entity MERGE writes (§I5)

**Files:**
- Modify: `src/personal_agent/memory/service.py:368` (`:Turn` MERGE inside `create_conversation()`), `:428` (`:Entity` MERGE — string literal), `:663` (`:Entity` MERGE inside `create_entity()` — built via string concat across multiple lines, ending around `:665`).

Relationship/edge MERGEs (`:PARTICIPATED_IN`, `:DISCUSSES`, `:CONTAINS`, `:NEXT`, `:OPERATED_BY`) inherit identity from the surrounding node creation context and do NOT need their own `originating_*` properties — the lint is keyed on node-label MERGEs only.

The `:Session`, `:Agent`, `:Person` MERGE sites are NOT in §I5 scope (those represent stable subjects, not request artifacts) — the lint label allowlist (`Turn|Entity|Relationship|DescriptionVersion`) skips them.

No `:Relationship`-labeled node writes exist in the current code (verified). §I5 mention of `:Relationship` covers future code; no action this phase.

The `create_entity()` query at `memory/service.py:655-665` is built dynamically:
```python
query = (
    "MERGE (e:Entity {name: $name})\n"
    "ON CREATE SET e.visibility = $visibility\n"
    "SET " + ",\n    ".join(set_clauses) + "\n"
    "RETURN e.name as entity_id"
)
```
Fix by appending `e.originating_trace_id = $originating_trace_id`, `e.originating_session_id = $originating_session_id`, `e.extractor_model = $extractor_model` to `set_clauses`. The lint's BinOp string-walker (Task 1) will catch this if it regresses.

- [ ] **Step 1: Write the failing contract test**

Create `tests/personal_agent/memory/test_neo4j_origination_properties.py`:

```python
"""Contract test: :Turn and :Entity writes carry §I5 origination properties."""
import pytest
from personal_agent.memory.service import MemoryService
from personal_agent.observability.trace_context import TraceContext

@pytest.mark.asyncio
async def test_turn_node_carries_originating_trace_id(memory_service: MemoryService) -> None:
    ctx = TraceContext.new(source="test")
    # Public API is create_conversation(turn: TurnNode, ...) — match the actual signature.
    turn = TurnNode(turn_id="turn-1", session_id="sess-x", role="user", content="hi")
    await memory_service.create_conversation(turn, trace_ctx=ctx)
    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (t:Turn {turn_id: 'turn-1'}) RETURN t.originating_trace_id AS tid, "
            "t.originating_session_id AS sid"
        )
        row = await result.single()
    assert row["tid"] == ctx.trace_id
    assert row["sid"] == "sess-x"

@pytest.mark.asyncio
async def test_entity_node_carries_originating_metadata(memory_service: MemoryService) -> None:
    ctx = TraceContext.new(source="test")
    # Public API is create_entity(entity: Entity, ...).
    entity = Entity(name="Acme Corp", entity_type="Organization")
    await memory_service.create_entity(entity, trace_ctx=ctx, extractor_model="qwen3-8b")
    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (e:Entity {name: 'Acme Corp'}) RETURN e.originating_trace_id AS tid, "
            "e.extractor_model AS em"
        )
        row = await result.single()
    assert row["tid"] == ctx.trace_id
    assert row["em"] == "qwen3-8b"
```

`create_conversation()` and `create_entity()` currently do NOT accept `trace_ctx`/`extractor_model` parameters — Task 4 extends their signatures. Plumb identity from callers (already passing `TraceContext` per Phase 4a) and `extractor_model` from the qwen3-8b extraction call site in `second_brain/entity_extraction.py`.

(Reuses the existing test substrate redirect from `tests/conftest.py` per FRE-375 — runs against :7688 Neo4j when `make test-infra-up`.)

- [ ] **Step 2: Run test to verify it fails**

```bash
make test-infra-up
uv run pytest tests/personal_agent/memory/test_neo4j_origination_properties.py -v
```

Expected: FAIL — properties don't exist on the nodes yet.

- [ ] **Step 3: Update the three node-MERGE statements**

In `memory/service.py:368` (`:Turn`):
```cypher
MERGE (t:Turn {turn_id: $turn_id})
ON CREATE SET
    t.role = $role,
    t.content = $content,
    t.created_at = datetime(),
    t.originating_trace_id = $originating_trace_id,
    t.originating_session_id = $originating_session_id
```
Add `originating_trace_id`, `originating_session_id` to the query parameters dict at the call site — read them from `trace_ctx`. `extractor_model` is N/A for `:Turn` (turns aren't extracted, they're observed).

In `memory/service.py:428` and `:665` (`:Entity`):
```cypher
MERGE (e:Entity {name: $name})
ON CREATE SET
    e.entity_type = $entity_type,
    e.created_at = datetime(),
    e.originating_trace_id = $originating_trace_id,
    e.originating_session_id = $originating_session_id,
    e.extractor_model = $extractor_model
```

For each callsite that doesn't currently pass identity/extractor through, plumb it. Phase 2 of FRE-374 (shipped) already passes `trace_ctx` through entity write paths — confirm and reuse.

If a relationship-property MERGE exists (line 552: `(s)-[r:DISCUSSES]->(e)`) and the relationship is conceptually an instance of a `Relationship` node, leave it for a follow-up — the lint covers `:Relationship` labeled MERGEs only when explicitly labeled with that node type. None of the 13 current sites label a node `:Relationship`.

- [ ] **Step 4: Run contract test**

```bash
uv run pytest tests/personal_agent/memory/test_neo4j_origination_properties.py -v
```
Expected: PASS.

- [ ] **Step 5: Run full memory test suite + the AST lint**

```bash
uv run pytest tests/personal_agent/memory/ -v
uv run python scripts/check_identity_threaded.py --strict src/personal_agent/memory/
```
Expected: green; lint reports 0 `cypher_merge_missing_origination` violations.

- [ ] **Step 6: Commit**

```bash
git add src/personal_agent/memory/service.py tests/personal_agent/memory/test_neo4j_origination_properties.py
git commit -m "feat: FRE-376 Phase 3 — Neo4j :Turn/:Entity carry origination (ADR-0074 §I5)"
```

---

## Task 5: Reconcile executor.py:1640 — rename the orchestrator step-planning event

**Files:**
- Modify: `src/personal_agent/telemetry/events.py`
- Modify: `src/personal_agent/orchestrator/executor.py:1638-1643`

The orchestrator emits `MODEL_CALL_STARTED` at the moment it's about to call into a model client — but the model client itself emits `model_call_started` (canonical, full shape) from inside `respond()`. Two emits, one event name, ambiguous semantics.

Resolution: orchestrator emit gets renamed to `step_planning_started` (and a matching `step_planning_completed` is added at end of step). `model_call_*` is now exclusively client-side.

- [ ] **Step 1: Add new event constants**

In `src/personal_agent/telemetry/events.py`, after the existing MODEL_CALL constants:

```python
STEP_PLANNING_STARTED = "step_planning_started"
"""Orchestrator step boundary: about to dispatch to a model client.

Carries the orchestrator's step-level intent (model_role, channel) but does NOT
duplicate the model_call_started fields, which the client emits itself.
"""

STEP_PLANNING_COMPLETED = "step_planning_completed"
"""Orchestrator step boundary: dispatched call completed (success or fail)."""
```

- [ ] **Step 2: Write a failing test for the rename**

`tests/personal_agent/orchestrator/test_step_planning_events.py`:

```python
import pytest
import structlog
from personal_agent.telemetry.events import (
    MODEL_CALL_STARTED, STEP_PLANNING_STARTED, STEP_PLANNING_COMPLETED,
)

@pytest.mark.asyncio
async def test_orchestrator_step_emits_step_planning_not_model_call(
    orchestrator_with_capture, sample_request,
) -> None:
    events = orchestrator_with_capture.events
    await orchestrator_with_capture.run(sample_request)
    event_names = [e["event"] for e in events]

    # Orchestrator emits step_planning_*, NOT model_call_*
    assert STEP_PLANNING_STARTED in event_names
    assert STEP_PLANNING_COMPLETED in event_names

    # MODEL_CALL_STARTED in the captured stream comes only from the client emit
    # — verify by checking the event payload carries `model` (which the
    # orchestrator emit never had).
    for e in events:
        if e["event"] == MODEL_CALL_STARTED:
            assert "model" in e, "MODEL_CALL_STARTED must only originate from client emit"
            assert "endpoint" in e
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/personal_agent/orchestrator/test_step_planning_events.py -v`
Expected: FAIL.

- [ ] **Step 4: Rename the orchestrator emit at executor.py:1639-1643**

Before:
```python
span_ctx, span_id = trace_ctx.new_span()
step_start_time = time.time()
log.info(
    MODEL_CALL_STARTED,
    trace_id=ctx.trace_id,
    span_id=span_id,
    model_role=model_role.value,
    channel=ctx.channel.value,
)
```

After:
```python
span_ctx, span_id = trace_ctx.new_span()
step_start_time = time.time()
log.info(
    STEP_PLANNING_STARTED,
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
    span_id=span_id,
    parent_span_id=trace_ctx.span_id,
    model_role=model_role.value,
    channel=ctx.channel.value,
)
```

At the end of the same step (after the model client returns and bookkeeping completes — locate the matching block; if no symmetric completion log exists today, add one):

```python
log.info(
    STEP_PLANNING_COMPLETED,
    trace_id=ctx.trace_id,
    session_id=ctx.session_id,
    span_id=span_id,
    parent_span_id=trace_ctx.span_id,
    model_role=model_role.value,
    channel=ctx.channel.value,
    duration_ms=int((time.time() - step_start_time) * 1000),
    status="success",
)
```

**Error-path symmetry (executor.py:2042 except block, terminates at `:2071 return TaskState.FAILED`).** The existing `except Exception as e:` block currently emits only `MODEL_CALL_ERROR` (at :2050) and then builds an error step record before returning. Add a `STEP_PLANNING_COMPLETED` emit with `status="error"` immediately **before** the `return TaskState.FAILED` line — do NOT change the return-vs-raise control flow:

```python
except Exception as e:
    duration_ms = int((time.time() - step_start_time) * 1000)
    # ... existing timer.end_span + log.error(MODEL_CALL_ERROR, ...) ...
    # ... existing ctx.error / sanitize / ctx.steps.append(...) ...
    log.info(
        STEP_PLANNING_COMPLETED,
        trace_id=ctx.trace_id,
        session_id=ctx.session_id,
        span_id=span_id,
        parent_span_id=trace_ctx.span_id,
        model_role=model_role.value,
        channel=ctx.channel.value,
        duration_ms=duration_ms,
        status="error",
        error_type=type(e).__name__,
    )
    return TaskState.FAILED   # preserve existing control flow
```

Without this, error-path traces will have an unmatched `STEP_PLANNING_STARTED` with no completion — same shape of bug the rename was supposed to fix on the model_call side.

Update the import at the top of `executor.py` to include `STEP_PLANNING_STARTED, STEP_PLANNING_COMPLETED`.

- [ ] **Step 5: Run the test**

Run: `uv run pytest tests/personal_agent/orchestrator/test_step_planning_events.py -v`
Expected: PASS.

- [ ] **Step 6: Update Kibana dashboards that reference the orchestrator-shape MODEL_CALL_STARTED**

Search dashboard NDJSON for `MODEL_CALL_STARTED` filters that depend on `model_role` or `channel` (i.e. orchestrator-shape consumers):
```bash
grep -l "model_role\|channel.*MODEL_CALL_STARTED" config/kibana/dashboards/*.ndjson
```
Update any matching saved searches to filter on `step_planning_started` instead.

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/telemetry/events.py src/personal_agent/orchestrator/executor.py \
    tests/personal_agent/orchestrator/test_step_planning_events.py \
    config/kibana/dashboards/
git commit -m "refactor: FRE-376 Phase 3 — separate step_planning_* from model_call_* (executor.py collision)"
```

---

## Task 6: Thread identity into log.* calls flagged by the lint

**Files:** Every file flagged by `scripts/check_identity_threaded.py` for `log_missing_trace_id` minus the allowlist. Verified actual counts: executor 72, memory/service 60, brainstem/scheduler 29, plus llm_client, captains_log, second_brain, gateway, events, telemetry, service (~285 total).

This is the largest task. Split into five sub-tasks (one commit each) so each diff stays reviewable.

### Task 6a: `src/personal_agent/orchestrator/`

- [ ] Lint the module: `uv run python scripts/check_identity_threaded.py src/personal_agent/orchestrator/ | grep log_missing_trace_id`
- [ ] For each flagged line, thread `trace_id=ctx.trace_id` (and `session_id=` where request-scoped). `ctx: TraceContext` is non-optional in this module per Phase 4a — always in scope.
- [ ] Re-run lint, expect 0 violations.
- [ ] `make test` green; `make mypy` green.
- [ ] Commit: `feat: FRE-376 Phase 3 — thread trace_id into orchestrator logs (ADR-0074 §I3)`

### Task 6b: `src/personal_agent/memory/`

- [ ] Lint, fix, re-lint as in 6a.
- [ ] `make test` green.
- [ ] Commit: `feat: FRE-376 Phase 3 — thread trace_id into memory logs (ADR-0074 §I3)`

### Task 6c: `src/personal_agent/llm_client/`

- [ ] Lint, fix, re-lint as in 6a. Phase 2 already threaded the model-emit helpers — remaining sites are mostly in `cost_tracker.py`, `concurrency.py`, `tool_call_parser.py`, `history_sanitiser.py`.
- [ ] `make test` green.
- [ ] Commit: `feat: FRE-376 Phase 3 — thread trace_id into llm_client logs (ADR-0074 §I3)`

### Task 6d: `src/personal_agent/brainstem/` (minus sensors)

- [ ] Lint scoped excluding sensor lifecycle code: `uv run python scripts/check_identity_threaded.py src/personal_agent/brainstem/scheduler.py src/personal_agent/brainstem/mode_manager.py src/personal_agent/brainstem/consumers/ src/personal_agent/brainstem/jobs/`. Scheduled jobs run without a TraceContext — mint one per job (`TraceContext.new(source="scheduler")`) and propagate.
- [ ] `make test` green.
- [ ] Commit: `feat: FRE-376 Phase 3 — thread trace_id into brainstem logs (ADR-0074 §I3)`

### Task 6e: Remaining modules

`src/personal_agent/{captains_log,second_brain,events,telemetry,gateway,service,request_gateway}/`

- [ ] Lint, fix, re-lint as in 6a.
- [ ] `make test` green.
- [ ] Commit: `feat: FRE-376 Phase 3 — thread trace_id into remaining logs (ADR-0074 §I3)`

### Task 6f: Final whole-tree sweep

- [ ] `uv run python scripts/check_identity_threaded.py --strict src/personal_agent/ | grep log_missing_trace_id` — expect empty.
- [ ] `uv run python scripts/check_identity_threaded.py src/personal_agent/; echo "exit=$?"` — expect `exit=0`.
- [ ] `make test` green.

---

## Task 7: Remove back-compat aliases from canonical emitters

**Files:**
- Modify: `src/personal_agent/llm_client/telemetry.py:84-85,135-138,146-216`
- Modify: `src/personal_agent/llm_client/litellm_client.py:22-27,309-319,486-503`

- [ ] **Step 1: Update the parity test FIRST (TDD red)**

In `tests/personal_agent/llm_client/test_telemetry_parity.py`:

Delete `test_completed_helper_keeps_legacy_token_aliases` (lines 121-139).

Delete the entire `TestLegacyBackCompat` class (lines 305-373).

Add a new test that asserts the aliases are GONE:

```python
class TestNoLegacyAliases:
    """Phase 3: canonical emitters must not co-emit legacy aliases or event names."""

    def test_started_does_not_emit_model_id_alias(self, captured_log) -> None:
        ctx = TraceContext.new(source="test")
        emit_model_call_started(
            log=captured_log.log, role="user_facing", model="m", endpoint="local",
            trace_ctx=ctx, span_id="s1",
        )
        kwargs = captured_log.last_call.kwargs
        assert "model_id" not in kwargs
        assert kwargs["model"] == "m"

    def test_completed_does_not_emit_token_aliases(self, captured_log) -> None:
        ctx = TraceContext.new(source="test")
        emit_model_call_completed(
            log=captured_log.log, role="user_facing", model="m", endpoint="local",
            trace_ctx=ctx, span_id="s1", latency_ms=100,
            input_tokens=100, output_tokens=50, total_tokens=150,
        )
        kwargs = captured_log.last_call.kwargs
        assert "model_id" not in kwargs
        assert "prompt_tokens" not in kwargs
        assert "completion_tokens" not in kwargs
        assert "tokens" not in kwargs
        assert "cache_write_tokens" not in kwargs

    def test_no_legacy_event_names_emitted(self, captured_events) -> None:
        # Drive a LiteLLM respond() with mocked completion; assert no
        # litellm_request_start / litellm_request_complete events appear.
        ...
        assert "litellm_request_start" not in captured_events.names
        assert "litellm_request_complete" not in captured_events.names
```

In `tests/personal_agent/llm_client/test_litellm_emit_payload.py`, delete:
- `test_litellm_emit_includes_completion_tokens` (lines 129-138)
- `test_litellm_emit_backward_compat_tokens_field_still_present` (lines 192-200)
- `test_litellm_emit_includes_cache_creation_input_tokens_field` (lines 180-188) — verify it references the alias `cache_write_tokens`; if it only references the canonical `cache_creation_input_tokens` field, KEEP it.

- [ ] **Step 2: Run the parity tests — expect failure**

Run: `uv run pytest tests/personal_agent/llm_client/test_telemetry_parity.py -v`
Expected: FAIL — the new "no legacy aliases" tests fail because the aliases are still emitted.

- [ ] **Step 3: Delete the legacy emit helpers**

In `src/personal_agent/llm_client/telemetry.py`:

1. Delete `emit_legacy_litellm_start` (lines 146-172).
2. Delete `emit_legacy_litellm_complete` (lines 175-216).
3. Strip `"model_id": model,` from `emit_model_call_started` body (lines 84-85).
4. Strip `"model_id": model,` and `"prompt_tokens": input_tokens,` and `"completion_tokens": output_tokens,` from `emit_model_call_completed` body (lines 135-138). Update the surrounding Back-compat comment block.

In `src/personal_agent/llm_client/litellm_client.py`:

1. Remove `emit_legacy_litellm_complete` and `emit_legacy_litellm_start` from imports (lines 22-27).
2. Delete the legacy emit calls + their comments at lines 309-319 and 486-503.

- [ ] **Step 4: Run parity tests — expect PASS**

Run: `uv run pytest tests/personal_agent/llm_client/ -v`
Expected: PASS.

- [ ] **Step 5: Audit downstream consumers of the legacy fields**

```bash
grep -rn "litellm_request_start\|litellm_request_complete\|cache_write_tokens" src/ tests/ scripts/
grep -rn '"model_id"\|"prompt_tokens"\|"completion_tokens"' src/personal_agent/ tests/
```

Update the following sites flagged by Codex audit:

- `src/personal_agent/orchestrator/executor.py:862-864` — capability extraction:
  ```python
  cap_prompt_tokens += meta.get("prompt_tokens", 0)
  cap_completion_tokens += meta.get("completion_tokens", 0)
  cap_total_tokens += meta.get("tokens", 0)
  ```
  Confirm step metadata is now populated with canonical names (`input_tokens` / `output_tokens` / `total_tokens`) after Phase 2 — if step metadata still uses legacy names, rename here AND at the population site. (Likely population: `orchestrator/skills.py` or wherever step results are recorded.)

- `tests/personal_agent/llm_client/test_litellm_gate_wiring.py:135`, `tests/test_orchestrator/test_routing_delegation.py:50`, `tests/personal_agent/orchestrator/test_skill_injection.py:46` — update mocks/asserts to canonical names.

- `src/personal_agent/second_brain/entity_extraction.py:205,219,285,286` — structlog kwargs use legacy names:
  - `:205`, `:219`: `model_id=model_def.id if model_def else None` → rename to `model=model_def.id if model_def else None`
  - `:285`, `:286`: `prompt_tokens=llm_response.get("usage", {}).get("prompt_tokens"), completion_tokens=llm_response.get("usage", {}).get("completion_tokens")` → rename kwargs to `input_tokens=...`, `output_tokens=...`. The `usage.get("prompt_tokens")` part is the LiteLLM provider response field — keep that (provider-native), but rename the structlog kwarg.

**Out of scope** (document but do not touch):
- `executor.py:1889` reads `usage.prompt_tokens` / `usage.completion_tokens` directly off the LiteLLM provider response — those are the provider's own field names, not our Phase 2 aliases. Leave alone.
- `captains_log/capture.py:61` (`CaptureEntry` dataclass field names) — internal substrate schema, replay-affecting. Track as a follow-up FRE; out of Phase 3 scope.

- [ ] **Step 6: Sanity-check the codebase**

```bash
grep -rn "emit_legacy_litellm" src/ tests/
grep -rn '"litellm_request_start"\|"litellm_request_complete"' src/ tests/
grep -rn '"model_id"\|"prompt_tokens"\|"completion_tokens"' src/personal_agent/llm_client/
```
Expected: zero hits in `src/`; tests that previously asserted on these names are gone.

- [ ] **Step 7: Commit**

```bash
git add src/personal_agent/llm_client/telemetry.py \
        src/personal_agent/llm_client/litellm_client.py \
        src/personal_agent/orchestrator/executor.py \
        tests/personal_agent/llm_client/test_telemetry_parity.py \
        tests/personal_agent/llm_client/test_litellm_emit_payload.py \
        tests/personal_agent/llm_client/test_litellm_gate_wiring.py \
        tests/test_orchestrator/test_routing_delegation.py \
        tests/personal_agent/orchestrator/test_skill_injection.py
git commit -m "refactor: FRE-376 Phase 3 — remove Phase 2 back-compat aliases (model_id, prompt/completion_tokens, litellm_request_*)"
```

---

## Task 8: Update Kibana dashboard generator + ES template + regenerated NDJSON

**Files:**
- Modify: `docker/elasticsearch/index-template.json:96,99-102`
- Modify: `config/kibana/setup_dashboards.py:46,272,279,294,305,366,375,390,399` — the **generator** that produces the NDJSON. Updating only the NDJSON regresses on the next regen run.
- Modify (regenerate from the updated script): `config/kibana/dashboards/llm_performance.ndjson`, `request_traces.ndjson`, `request_timing.ndjson`, `data_views.ndjson`

- [ ] **Step 1: Update ES template**

In `docker/elasticsearch/index-template.json`, remove these field mappings:
- `"model_id": { "type": "keyword" }` (line 96)
- `"prompt_tokens": { "type": "long" }` (line 99)
- `"completion_tokens": { "type": "long" }` (line 100)

Keep `input_tokens`, `output_tokens`, `cache_read_tokens`, `cache_creation_input_tokens`.

Also remove the `cache_write_tokens` mapping if present (it was an alias for `cache_creation_input_tokens`).

- [ ] **Step 2: Rewrite dashboard aggregations in the generator**

Update `config/kibana/setup_dashboards.py` first (verified hot lines: 46 [field mapping list], 272 [Prompt Tokens viz], 279 [Completion Tokens viz], 294 [model_id description], 305 [groupby field], 366/375/390/399 [prompt_tokens aggregations]). Then re-run the generator to regenerate NDJSON.

For both the generator and the resulting NDJSON, the substitutions are:

| Old | New |
|-----|-----|
| `model_id.keyword` | `model.keyword` |
| `model_id` (in aggregations) | `model` |
| `prompt_tokens` | `input_tokens` |
| `completion_tokens` | `output_tokens` |
| `event:litellm_request_start` | `event:model_call_started` |
| `event:litellm_request_complete` | `event:model_call_completed` |
| `cache_write_tokens` | `cache_creation_input_tokens` |

Use `jq` or sed/awk per-file. Verify each NDJSON object still parses:

```bash
for f in config/kibana/dashboards/*.ndjson; do
    while IFS= read -r line; do
        echo "$line" | jq -c . > /dev/null || { echo "broken: $f"; break; }
    done < "$f"
done
```

- [ ] **Step 3: Local Kibana smoke test**

```bash
make up SERVICE=kibana
# wait for Kibana healthy
curl -s localhost:5601/api/status | jq .status.overall.state
# import a dashboard
curl -X POST -H "kbn-xsrf: true" -F "file=@config/kibana/dashboards/llm_performance.ndjson" \
    http://localhost:5601/api/saved_objects/_import?overwrite=true | jq
# open localhost:5601 → Dashboards → load each → confirm no field-not-found banners
```

Expected: each dashboard loads; visualizations show data using the canonical field names.

- [ ] **Step 4: Commit**

```bash
git add docker/elasticsearch/index-template.json config/kibana/dashboards/
git commit -m "chore: FRE-376 Phase 3 — rewrite ES template + Kibana dashboards on canonical fields"
```

---

## Task 8b: Migrate the evaluation harness off legacy event names

**Files:**
- Modify: `tests/evaluation/run_primitive_tools_eval.py:221-300,446-453`

The eval harness aggregates per-run token cost by reading events from Captain's Log / ES. It still pivots on `litellm_request_complete` and reads `prompt_tokens`, `completion_tokens`, `cache_write_tokens`. Removing the canonical-side aliases breaks the harness silently — it will simply find zero events and report all-zero token counts.

- [ ] **Step 1: Identify the event-name pivot and field reads**

```bash
grep -n "litellm_request_complete\|prompt_tokens\|completion_tokens\|cache_write_tokens" tests/evaluation/run_primitive_tools_eval.py
```

Confirms hits at lines 221-300 (per-run accumulator) and 446-453 (comparator math).

- [ ] **Step 2: Re-point the harness**

Substitute:
- `ev_type == "litellm_request_complete"` → `ev_type == "model_call_completed"`
- `event.get("prompt_tokens")` → `event.get("input_tokens")`
- `event.get("completion_tokens") or (total - pt)` → `event.get("output_tokens")` (derive from canonical)
- `event.get("cache_write_tokens")` → `event.get("cache_creation_input_tokens")`
- Per-row keys returned by `_summarise()` keep their existing names (`prompt_tokens`, `completion_tokens`, `cache_write_tokens`) — those are the harness's *output* shape consumed by report rendering. Renaming the *output* schema is out of scope; rename only the *input* sourcing.

- [ ] **Step 3: Smoke-run the harness against a recent eval run**

```bash
make eval-infra-up
uv run python tests/evaluation/run_primitive_tools_eval.py --dry-run-from-run <recent-eval-id>
```

Expected: non-zero token counts; comparator runs without KeyError.

- [ ] **Step 4: Commit**

```bash
git add tests/evaluation/run_primitive_tools_eval.py
git commit -m "chore: FRE-376 Phase 3 — eval harness reads canonical model_call_completed event"
```

---

## Task 9: Wire the lint into pre-commit + run final full verification

**Files:**
- Modify: `.pre-commit-config.yaml`

- [ ] **Step 1: Add pre-commit hook**

In `.pre-commit-config.yaml`:

```yaml
  - repo: local
    hooks:
      - id: check-identity-threaded
        name: ADR-0074 identity threading
        entry: uv run python scripts/check_identity_threaded.py
        language: system
        files: ^src/personal_agent/.*\.py$
        pass_filenames: false
        args: [src/personal_agent/]
```

- [ ] **Step 2: Verify hook fires correctly**

```bash
pre-commit run check-identity-threaded --all-files
```
Expected: PASS (after Tasks 3–6 are done).

Then deliberately break it:
```bash
echo 'log.info("nope")' >> src/personal_agent/orchestrator/executor.py
pre-commit run check-identity-threaded --all-files
```
Expected: FAIL with the synthetic line flagged. Revert the edit.

- [ ] **Step 3: Final full verification**

```bash
make test
make mypy
make ruff-check
make ruff-format
uv run python scripts/check_identity_threaded.py src/personal_agent/; echo "exit=$?"
grep -rn "emit_legacy_litellm\|litellm_request_start\|litellm_request_complete" src/ tests/
```

Expected:
- All four make targets clean.
- Lint exits 0.
- Final grep returns no hits in `src/` (test fixtures, if any, must use canonical names).

- [ ] **Step 4: Commit**

```bash
git add .pre-commit-config.yaml
git commit -m "ci: FRE-376 Phase 3 — wire identity-threaded lint into pre-commit"
```

---

## Task 10: Ship — ADR + MASTER_PLAN, PR, deploy, post-deploy probe

**Files:**
- Modify: `docs/architecture_decisions/ADR-0074-cross-substrate-traceability.md`
- Modify: `docs/plans/MASTER_PLAN.md`

- [ ] **Step 1: Update ADR + MASTER_PLAN**

In ADR-0074, mark Phase 3 status row as "Shipped (PR #XX, <commit-sha>)". In MASTER_PLAN.md, update the FRE-376 entry header and `Last updated:` line per memory `feedback_update_master_plan.md`.

Commit:
```bash
git add docs/architecture_decisions/ADR-0074-cross-substrate-traceability.md docs/plans/MASTER_PLAN.md
git commit -m "docs: FRE-376 Phase 3 shipped (ADR-0074 §I3/§I5 enforced)"
```

- [ ] **Step 2: Open PR**

Per memory `feedback_branch_pr_for_code.md`, code goes through a feature branch + PR.

```bash
git push -u origin <branch>
gh pr create --title "FRE-376 Phase 3 — identity threading + back-compat cleanup" \
    --body "$(cat <<'EOF'
## Summary
- Adds `scripts/check_identity_threaded.py` AST lint (ADR-0074 §I3/§I5), wired into pre-commit.
- Threads `trace_id`/`session_id` through all 21 `bus.publish` sites and 13 Cypher `MERGE` writes; adds `originating_trace_id`, `originating_session_id`, `extractor_model` to `:Turn` and `:Entity` nodes.
- Renames the orchestrator step emit at `executor.py:1640` from `MODEL_CALL_STARTED` to `STEP_PLANNING_STARTED`; adds matching `STEP_PLANNING_COMPLETED`. Resolves the two-event-name collision.
- Removes Phase 2 back-compat aliases: `model_id`, `prompt_tokens`, `completion_tokens`, legacy `litellm_request_start`/`litellm_request_complete` events, and helper functions `emit_legacy_litellm_*`.
- Rewrites 4 Kibana dashboards + ES index template on canonical field names.

## Test plan
- [ ] `make test` green
- [ ] `make mypy` clean
- [ ] `python scripts/check_identity_threaded.py src/personal_agent/` exits 0
- [ ] Kibana dashboards open without field-not-found banners
- [ ] Post-deploy: ES query `event:litellm_request_complete AND @timestamp:[now-1h TO now]` returns 0
- [ ] Post-deploy: ES query `event:model_call_completed AND _missing_:trace_id` returns 0
- [ ] Post-deploy: Cypher origination probe returns 0 nulls on new Turn/Entity writes
- [ ] Post-deploy: `event:step_planning_started` shows up in Kibana
EOF
)"
```

- [ ] **Step 3: After PR merges — deploy**

```bash
make deploy
ENV=cloud make health
```

- [ ] **Step 4: Post-deploy verification (same session — see memory `feedback_plans_acceptance_criteria.md`)**

Capture the deploy timestamp before driving traffic — the probe window MUST start AFTER the rolling restart finishes, not `now-1h` (which would include pre-deploy legacy events and create spurious failures during the mixed-state cutover per Caveat #1):

```bash
DEPLOY_TS=$(date -u +%Y-%m-%dT%H:%M:%SZ)
echo "Probe window begins: $DEPLOY_TS"
# Wait ~60s for rolling restart to drain in-flight requests
sleep 60
```

Drive a single chat turn through the deployed VPS (cloud) endpoint, then:

```bash
# 1. Legacy event names should NOT appear after deploy timestamp
curl -s "http://localhost:9201/logs-*/_search" -H "Content-Type: application/json" -d "{
  \"size\": 0,
  \"query\": {\"bool\": {\"must\": [
    {\"terms\": {\"event\": [\"litellm_request_start\", \"litellm_request_complete\"]}},
    {\"range\": {\"@timestamp\": {\"gte\": \"$DEPLOY_TS\"}}}
  ]}}
}" | jq '.hits.total.value'   # expect 0

# 2. New canonical events should carry trace_id (after deploy)
curl -s "http://localhost:9201/logs-*/_search" -H "Content-Type: application/json" -d "{
  \"size\": 0,
  \"query\": {\"bool\": {\"must\": [
    {\"term\": {\"event\": \"model_call_completed\"}},
    {\"range\": {\"@timestamp\": {\"gte\": \"$DEPLOY_TS\"}}}
  ], \"must_not\": [{\"exists\": {\"field\": \"trace_id\"}}]}}
}" | jq '.hits.total.value'   # expect 0

# 3. Step planning events visible (after deploy)
curl -s "http://localhost:9201/logs-*/_search" -H "Content-Type: application/json" -d "{
  \"size\": 0,
  \"query\": {\"bool\": {\"must\": [
    {\"term\": {\"event\": \"step_planning_started\"}},
    {\"range\": {\"@timestamp\": {\"gte\": \"$DEPLOY_TS\"}}}
  ]}}
}" | jq '.hits.total.value'   # expect >0

# 4. New Turn nodes carry originating_trace_id
docker exec -i seshat-neo4j cypher-shell -u neo4j -p <pw> "
MATCH (t:Turn) WHERE t.created_at > datetime() - duration('PT1H')
  AND t.originating_trace_id IS NULL
RETURN count(t) AS missing_origination
"   # expect 0

# 5. New Entity nodes carry origination + extractor_model
docker exec -i seshat-neo4j cypher-shell -u neo4j -p <pw> "
MATCH (e:Entity) WHERE e.created_at > datetime() - duration('PT1H')
  AND (e.originating_trace_id IS NULL OR e.extractor_model IS NULL)
RETURN count(e) AS missing_origination
"   # expect 0
```

If any check fails, file a follow-up FRE before closing — per memory `feedback_multi_phase_tickets_stay_in_progress.md`, the FRE-376 Linear ticket stays In Progress until Phase 5 ships, regardless.

---

## Risks and caveats (called out by Codex review)

1. **Rolling-restart mixed-state window.** Removing the legacy emit + dashboard fields in one PR is atomic for the static artifacts but the docker restart is rolling. For ~30 seconds during deploy, one model client process may be emitting canonical-only while another still emits legacy. The ES index template tolerates both shapes (dynamic mapping still accepts the removed fields). The post-deploy probe runs ~5 min after deploy to give the window time to flush. If post-deploy probe step 1 (`event:litellm_request_complete` count over last 1h) returns >0 hits with timestamps *after* the deploy boundary, that's a regression — investigate before closing.

2. **Lint allowlist line-drift.** Allowlist entries key on `(path, line)`. Unrelated edits that shift line numbers will silently invalidate suppressions (the lint may flag a new line, or skip a now-moved offender). Mitigation: after every merge that touches an allowlisted file, re-run `scripts/check_identity_threaded.py --strict` and resync the allowlist. Future hardening (out of scope): switch allowlist to content-hash keys.

3. **Cypher dynamic-query coverage is best-effort.** The lint's BinOp walker catches simple string concat (e.g. `memory/service.py:663`). It does NOT catch:
   - Queries built via `"\n".join([...])` over a variable list
   - Queries assembled via repeated `.append()` on a list
   - Queries passed through a helper function
   Mitigation: code review for these patterns; prefer single-template Cypher when possible.

4. **Bus.publish opaque-payload false positives.** When the payload dict is built into a local variable before publish, the lint can't statically prove identity is present. Either (a) inline the dict at the publish site, (b) use a structured event dataclass with required `trace_id`/`session_id` (cleanest long-term fix — defer to a follow-up FRE), or (c) allowlist with the assignment site in `reason:`. Kwarg-only invocations (`bus.publish(stream=X, event=Y)`) are also not deep-inspected for payload identity in v1 of the lint — all 21 current sites use positional payloads, so this isn't an issue today; if a kwarg-only site is introduced later, extend `_bus_publish_identity_kwargs` to inspect `kw.value` for the `event=` kwarg.

5. **Step-metadata field-name dependency** (executor.py:862). The orchestrator's capability accumulator reads `meta.get("prompt_tokens")` from each step's recorded metadata. If step metadata is populated from the model-call response (which Phase 2's canonical emit uses `input_tokens` / `output_tokens`), the existing code is already silently zero-summing. Confirm during Task 7 Step 5 whether step metadata uses canonical or legacy names today; rename the read accordingly.

6. **Captain's Log capture schema (`capture.py:61`) deferred.** `CaptureEntry.prompt_tokens` / `.completion_tokens` are NOT touched by this phase. Captain's Log readers and any downstream consumers continue to see those field names. File a follow-up FRE titled "Captain's Log capture schema canonicalization" to rename + replay.

---

## Self-review checklist

- [x] **Spec coverage**: All three Phase 3 user-stated work items have explicit tasks. §I3 covered in Tasks 1, 3, 6. §I5 covered in Task 4. Back-compat removal in Task 7. Executor collision in Task 5. Dashboard/template in Task 8. Lint enforcement in Tasks 1 + 9.
- [x] **No placeholders**: Every code change shows the actual code. The single intentionally open-ended scope is "iterate one module at a time" in Task 6 — that's by design; the lint is the spec, and listing 250+ individual line-by-line edits would be noise.
- [x] **Type consistency**: `Violation` dataclass used identically in lint impl + test; `STEP_PLANNING_STARTED` / `STEP_PLANNING_COMPLETED` referenced consistently; `originating_trace_id` / `originating_session_id` / `extractor_model` consistent throughout (matches ADR-0074 §I5 lines 49-54).
- [x] **Memory-conditioned constraints honored**: post-deploy probe runs in same session as deploy (`feedback_plans_acceptance_criteria.md`); ticket stays In Progress (`feedback_multi_phase_tickets_stay_in_progress.md`); branch + PR for code (`feedback_branch_pr_for_code.md`); MASTER_PLAN updated on ship (`feedback_update_master_plan.md`).
