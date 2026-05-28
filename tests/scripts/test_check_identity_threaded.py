# ruff: noqa: D103
"""Unit tests for the identity-threading AST lint (FRE-376 Phase 3).

The lint enforces ADR-0074 §I3 (every async boundary preserves identity) and
§I5 (memory writes carry origination). It flags any of:
  - log.* calls in src/personal_agent/ missing trace_id kwarg
  - bus.publish calls missing trace_id / session_id in the payload dict
  - Cypher MERGE writes on :Turn/:Entity/:Relationship/:DescriptionVersion that
    don't also set originating_trace_id / originating_session_id.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.check_identity_threaded import lint_file


def test_log_info_without_trace_id_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def handle(ctx) -> None:
                log.info("did a thing", model="claude")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    log_violations = [v for v in violations if v.kind == "log_missing_trace_id"]
    assert len(log_violations) == 1


def test_log_info_with_trace_id_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def handle(ctx) -> None:
                log.info("did a thing", trace_id=ctx.trace_id, model="claude")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    log_violations = [v for v in violations if v.kind == "log_missing_trace_id"]
    assert log_violations == []


def test_connection_scope_requires_session_id(tmp_path: Path) -> None:
    """A log in a connection-scope fn (session_id reachable, no trace) must carry session_id."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def ws_session(websocket, session_id) -> None:
                log.info("ws.connected")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "log_missing_session_id" for v in violations)


def test_connection_scope_with_session_id_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def ws_session(websocket, session_id) -> None:
                log.info("ws.connected", session_id=session_id)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert [v for v in violations if v.kind.startswith("log_missing")] == []


def test_boot_scope_with_no_identity_is_exempt(tmp_path: Path) -> None:
    """A lifespan/boot log with no identity reachable is exempt (no trace exists)."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def lifespan(app) -> None:
                log.info("service_starting")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert [v for v in violations if v.kind.startswith("log_missing")] == []


def test_request_scope_via_trace_ctx_carrier_is_flagged(tmp_path: Path) -> None:
    """A fn holding a trace_ctx carrier must thread trace_id even without .trace_id deref."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def step(trace_ctx) -> None:
                log.info("did a thing", model="claude")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "log_missing_trace_id" for v in violations)


def test_trace_scope_takes_priority_over_session(tmp_path: Path) -> None:
    """When both trace_id and session_id are reachable, trace_id is required (more specific)."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            async def handle(ctx, session_id) -> None:
                log.info("did a thing", session_id=session_id)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "log_missing_trace_id" for v in violations)


def test_bus_publish_without_identity_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit(bus, payload) -> None:
                await bus.publish("stream:x", {"foo": "bar"})
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bus_publish_missing_identity" for v in violations)


def test_bus_publish_with_inline_identity_payload_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit(bus, ctx) -> None:
                await bus.publish(
                    "stream:x",
                    {"foo": "bar", "trace_id": ctx.trace_id, "session_id": ctx.session_id},
                )
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    bus_violations = [v for v in violations if v.kind == "bus_publish_missing_identity"]
    assert bus_violations == []


def test_bus_publish_typed_event_inline_is_clean(tmp_path: Path) -> None:
    """Inline typed event constructor (Pydantic enforces identity at runtime)."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit(bus, ctx) -> None:
                await bus.publish(
                    "stream:x",
                    RequestCapturedEvent(trace_id=ctx.trace_id, session_id=ctx.session_id),
                )
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    bus_violations = [v for v in violations if v.kind == "bus_publish_missing_identity"]
    assert bus_violations == []


def test_bus_publish_typed_event_via_local_var_is_clean(tmp_path: Path) -> None:
    """Variable assigned from a typed Event constructor in same file."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit(bus, ctx) -> None:
                event = MemoryAccessedEvent(trace_id=ctx.trace_id, session_id=ctx.session_id)
                await bus.publish("stream:x", event)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    bus_violations = [v for v in violations if v.kind == "bus_publish_missing_identity"]
    assert bus_violations == []


def test_bus_publish_typed_event_via_function_param_is_clean(tmp_path: Path) -> None:
    """Variable bound by an Event-typed function parameter (e.g. nested closure)."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def _publish_safe(bus, evt: MetricsSampledEvent) -> None:
                await bus.publish("stream:x", evt)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    bus_violations = [v for v in violations if v.kind == "bus_publish_missing_identity"]
    assert bus_violations == []


def test_bus_publish_opaque_var_is_still_flagged(tmp_path: Path) -> None:
    """Variable not traceable to a typed Event constructor stays flagged."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def emit(bus, payload) -> None:
                await bus.publish("stream:x", payload)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bus_publish_missing_identity" for v in violations)


def test_self_bus_publish_is_recognized(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            class Thing:
                async def emit(self, ctx) -> None:
                    await self._event_bus.publish("stream:x", {"foo": "bar"})
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bus_publish_missing_identity" for v in violations)


def test_cypher_merge_turn_without_origination_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def write_turn(session, turn_id):
                await session.run(
                    "MERGE (t:Turn {turn_id: $turn_id}) SET t.created_at = datetime()"
                )
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "cypher_merge_missing_origination" for v in violations)


def test_cypher_merge_turn_with_origination_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def write_turn(session, turn_id, tid, sid):
                await session.run(
                    "MERGE (t:Turn {turn_id: $turn_id}) "
                    "SET t.created_at = datetime(), "
                    "t.originating_trace_id = $originating_trace_id, "
                    "t.originating_session_id = $originating_session_id"
                )
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    cypher_violations = [v for v in violations if v.kind == "cypher_merge_missing_origination"]
    assert cypher_violations == []


def test_cypher_merge_session_is_not_flagged(tmp_path: Path) -> None:
    """:Session/:Agent/:Person are out of §I5 scope."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def write_session(session, sid):
                await session.run("MERGE (s:Session {session_id: $session_id})")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    cypher_violations = [v for v in violations if v.kind == "cypher_merge_missing_origination"]
    assert cypher_violations == []


def test_cypher_merge_via_join_is_flagged(tmp_path: Path) -> None:
    """memory/service.py:663-style dynamic concat with .join must be caught."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            async def write_entity(session):
                set_clauses = ["e.visibility = $vis", "e.created_at = datetime()"]
                query = (
                    "MERGE (e:Entity {name: $name})\\n"
                    "ON CREATE SET e.visibility = $visibility\\n"
                    "SET " + ",\\n    ".join(set_clauses) + "\\n"
                    "RETURN e.name as entity_id"
                )
                await session.run(query)
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "cypher_merge_missing_origination" for v in violations)


def test_allowlisted_violations_are_suppressed(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            log = structlog.get_logger(__name__)

            def lifecycle() -> None:
                log.info("startup")
            """
        )
    )
    allowlist = [{"path": str(src), "line": 6, "pattern": "log.info", "reason": "lifecycle"}]
    violations = lint_file(src, allowlist=allowlist)
    log_violations = [v for v in violations if v.kind == "log_missing_trace_id"]
    assert log_violations == []
