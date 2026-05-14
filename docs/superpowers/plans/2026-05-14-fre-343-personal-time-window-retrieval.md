# FRE-343 — Personal Time-Window Retrieval — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship personal time-window retrieval — a `recall_personal_history` tool scoped by `ctx.user_id` via a new `(:Person)-[:PARTICIPATED_IN]->(:Turn)` provenance edge, with a complementary SKILL.md and a one-shot backfill of existing turns.

**Architecture:** W3C-PROV provenance edge written at Turn-save time inside `MemoryService.create_conversation`. New native Tier-1 tool reads via that edge. `TaskCapture.user_id` tightens to non-optional UUID (verified `get_request_user` always resolves one). Default `search_memory` behavior is unchanged.

**Tech Stack:** Python 3.12 · Pydantic v2 · Neo4j 5.26 (async driver) · PostgreSQL (asyncpg) · pytest · structlog · uv.

**Spec:** `docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md`

---

## File Structure

**Files to create:**

| Path | Responsibility |
|---|---|
| `src/personal_agent/tools/personal_history.py` | Tool definition + async executor |
| `docs/skills/personal-history-recall.md` | SKILL.md with semantic-XML body (FRE-337 pilot extension) |
| `scripts/backfill_participated_in.py` | One-shot idempotent backfill script |
| `tests/personal_agent/memory/test_participated_in_edge.py` | Unit tests for the new MERGE Cypher |
| `tests/personal_agent/memory/test_create_conversation_user_id_propagation.py` | Mock-driver test that the Cypher reaches the driver |
| `tests/personal_agent/tools/test_recall_personal_history.py` | Tool executor unit tests |
| `tests/personal_agent/captains_log/test_capture_user_id_required.py` | TaskCapture validation tightening |
| `tests/scripts/test_backfill_participated_in.py` | Backfill script tests (Postgres + Neo4j fixtures) |
| `tests/integration/test_personal_history_e2e.py` | Full-stack scope-leak test (marker `integration`) |

**Files to modify:**

| Path | Change |
|---|---|
| `src/personal_agent/captains_log/capture.py` | `user_id: UUID \| None = None` → `user_id: UUID`; drop `None` branch in validator |
| `src/personal_agent/orchestrator/executor.py:848` | `getattr(ctx, "user_id", None)` → `ctx.user_id` |
| `src/personal_agent/memory/service.py:306-398` | Add `user_id: UUID` param to `create_conversation`; MERGE PARTICIPATED_IN inside the same session |
| `src/personal_agent/second_brain/consolidator.py:459-481` | Drop visibility `None` branch; pass `user_id=capture.user_id` |
| `src/personal_agent/tools/__init__.py` | Import + register the new tool |
| `config/governance/tools.yaml` | Add `recall_personal_history` entry under `tools:` |
| `tests/personal_agent/memory/test_visibility.py` | Three test functions get `user_id=test_uid` added; `test_create_conversation_default_public` refactored or removed |
| Other test files using `TaskCapture(...)` w/o `user_id` | Add `user_id=uuid4()` to each construction site |
| `Makefile` | Add `backfill-participated-in` target |
| `docs/architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md` | Append §Update 2026-05-14 — Personal history retrieval |
| `docs/plans/MASTER_PLAN.md` | Add to Recently Completed; bump Last updated; remove FRE-343 from Immediately Actionable |

---

## Project conventions (apply throughout)

- `from personal_agent.config import settings` — never `os.getenv()`.
- `structlog` via `get_logger(__name__)` — never `print()`. Always include `trace_id`.
- Modern type syntax: `str | None`, never `Optional[str]` or `Union`.
- Google-style docstrings on all public functions/classes (Args, Returns, Raises).
- Async for all I/O.
- One pytest at a time — the hook `.claude/hooks/check-pytest-lock.sh` enforces this. Run named tests, not the full suite, between tasks.
- Never use Alembic. Schema changes go in `docker/postgres/init.sql` + `docker/postgres/migrations/`. (FRE-343 needs no Postgres schema change.)
- After each commit, run only the file-scoped tests. The full `make test` runs at the end of Task 13.

---

## Task 1: Tighten `TaskCapture.user_id` to non-optional UUID

**Files:**
- Modify: `src/personal_agent/captains_log/capture.py:65-74`
- Modify: `src/personal_agent/orchestrator/executor.py:848`
- Create: `tests/personal_agent/captains_log/test_capture_user_id_required.py`
- Modify: `tests/test_captains_log/test_capture.py:50` (remove `user_id=None` case)
- Modify: `tests/test_second_brain/test_consolidation_e2e.py` (8 `TaskCapture(...)` sites — add `user_id=uuid4()`)
- Modify: `tests/test_second_brain/test_session_summary.py:30` (1 site)
- Modify: `tests/personal_agent/memory/test_memory_access_events.py:479,575` (2 sites)

- [ ] **Step 1: Write failing test for the tightening**

Create `tests/personal_agent/captains_log/test_capture_user_id_required.py`:

```python
"""TaskCapture.user_id is required (FRE-343 — non-optional after FRE-213).

Background: get_request_user (service/auth.py) always resolves a user_id —
either from Cf-Access-Authenticated-User-Email or from the
settings.agent_owner_email fallback (or raises 401). user_id=None at write
time is now a real bug, not a silent fallback.
"""

from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from personal_agent.captains_log.capture import TaskCapture


def _base_kwargs() -> dict:
    return {
        "trace_id": "trace-1",
        "session_id": "sess-1",
        "timestamp": datetime.now(timezone.utc),
        "user_message": "hi",
        "outcome": "completed",
    }


def test_user_id_required_raises_when_missing() -> None:
    """Constructing TaskCapture without user_id raises a Pydantic ValidationError."""
    with pytest.raises(ValidationError):
        TaskCapture(**_base_kwargs())  # type: ignore[call-arg]


def test_user_id_required_raises_when_none() -> None:
    """Explicit user_id=None is rejected after FRE-343."""
    with pytest.raises(ValidationError):
        TaskCapture(**_base_kwargs(), user_id=None)  # type: ignore[arg-type]


def test_user_id_accepts_uuid_instance() -> None:
    """A UUID instance is accepted (happy path)."""
    uid = uuid4()
    capture = TaskCapture(**_base_kwargs(), user_id=uid)
    assert capture.user_id == uid


def test_user_id_coerces_string() -> None:
    """Pydantic coerces a UUID string to a UUID instance."""
    uid = uuid4()
    capture = TaskCapture(**_base_kwargs(), user_id=str(uid))
    assert capture.user_id == uid
```

- [ ] **Step 2: Run the new test — verify it fails**

Run: `uv run pytest tests/personal_agent/captains_log/test_capture_user_id_required.py -v`
Expected: First two cases FAIL (currently `user_id` is `UUID | None = None` and the validator returns `None`); the last two PASS.

- [ ] **Step 3: Modify the model**

In `src/personal_agent/captains_log/capture.py:65-74`, replace:

```python
    # FRE-229: owning user UUID — None for CLI/unauthenticated paths; used by consolidator to set visibility
    user_id: UUID | None = None

    @field_validator("user_id", mode="before")
    @classmethod
    def _coerce_user_id(cls, v: Any) -> UUID | None:
        if v is None:
            return None
        if type(v) is UUID:
            return v
        return UUID(str(v))
```

with:

```python
    # FRE-343: user_id is non-optional. get_request_user always resolves one
    # (CF Access header or settings.agent_owner_email fallback or 401),
    # so user_id=None at write time is a real bug, not a fallback.
    user_id: UUID

    @field_validator("user_id", mode="before")
    @classmethod
    def _coerce_user_id(cls, v: Any) -> UUID:
        if type(v) is UUID:
            return v
        return UUID(str(v))
```

- [ ] **Step 4: Run the new test — verify it passes**

Run: `uv run pytest tests/personal_agent/captains_log/test_capture_user_id_required.py -v`
Expected: All 4 PASS.

- [ ] **Step 5: Drop defensive read in executor**

In `src/personal_agent/orchestrator/executor.py:848`, replace:

```python
                    user_id=getattr(ctx, "user_id", None),
```

with:

```python
                    user_id=ctx.user_id,
```

- [ ] **Step 6: Update existing test fixtures**

For each `TaskCapture(...)` site listed under **Files** above that does not already pass `user_id`, add `user_id=uuid4()` (import `from uuid import uuid4`). The sites are:

- `tests/test_captains_log/test_capture.py:50` — DELETE the entire `c3 = TaskCapture(**_base, user_id=None)` test case (no longer a valid scenario).
- `tests/test_second_brain/test_consolidation_e2e.py` — 8 construction sites at lines 46, 87, 128, 138, 171, 204, 231, 268. For each, add `user_id=uuid4()` to the kwargs.
- `tests/test_second_brain/test_session_summary.py:30` — add `user_id=uuid4()` to the fixture.
- `tests/personal_agent/memory/test_memory_access_events.py:479` and `:575` — add `user_id=uuid4()`.

Use grep to verify nothing was missed:

```bash
grep -rn "TaskCapture(" tests/ | grep -v user_id
```

Expected: no output (every site now has `user_id`).

- [ ] **Step 7: Run the affected test files**

Run each separately (pytest lock):

```bash
uv run pytest tests/test_captains_log/test_capture.py -v
uv run pytest tests/test_second_brain/test_consolidation_e2e.py -v
uv run pytest tests/test_second_brain/test_session_summary.py -v
uv run pytest tests/personal_agent/memory/test_memory_access_events.py -v
uv run pytest tests/personal_agent/captains_log/test_capture_user_id_required.py -v
```

Expected: all PASS.

- [ ] **Step 8: Commit**

```bash
git add src/personal_agent/captains_log/capture.py \
        src/personal_agent/orchestrator/executor.py \
        tests/personal_agent/captains_log/test_capture_user_id_required.py \
        tests/test_captains_log/test_capture.py \
        tests/test_second_brain/test_consolidation_e2e.py \
        tests/test_second_brain/test_session_summary.py \
        tests/personal_agent/memory/test_memory_access_events.py
git commit -m "feat(fre-343): TaskCapture.user_id non-optional UUID

get_request_user always resolves a user_id (CF Access header or
settings.agent_owner_email fallback or 401). The user_id=None branch
was a defensive holdover, not a real production case — making it a
bug rather than a silent fallback prevents PARTICIPATED_IN edge
writes from being skipped."
```

---

## Task 2: Add PARTICIPATED_IN MERGE to `create_conversation`

**Files:**
- Modify: `src/personal_agent/memory/service.py:306-398`
- Create: `tests/personal_agent/memory/test_create_conversation_user_id_propagation.py`

- [ ] **Step 1: Write the failing test (mock-driver style)**

Create `tests/personal_agent/memory/test_create_conversation_user_id_propagation.py`:

```python
"""FRE-343: create_conversation MERGEs the PARTICIPATED_IN edge.

Verifies that the Cypher reaches the Neo4j driver — does not exercise
a live Neo4j (that's covered by test_participated_in_edge.py).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from personal_agent.memory.models import TurnNode
from personal_agent.memory.service import MemoryService


def _make_service_with_mock() -> tuple[MemoryService, AsyncMock]:
    """Build a MemoryService whose driver yields a mock async session."""
    service = MemoryService.__new__(MemoryService)
    service.connected = True
    mock_driver = AsyncMock()
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)
    mock_driver.session = lambda: mock_session  # not awaited
    service.driver = mock_driver
    return service, mock_session


@pytest.mark.asyncio
async def test_create_conversation_merges_participated_in_edge() -> None:
    """The PARTICIPATED_IN MERGE Cypher is issued after the Turn MERGE."""
    service, mock_session = _make_service_with_mock()

    captured_cypher: list[str] = []
    captured_kwargs: list[dict] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured_cypher.append(cypher)
        captured_kwargs.append(dict(kwargs))
        return AsyncMock()

    mock_session.run = AsyncMock(side_effect=capture_run)

    uid = uuid4()
    turn = TurnNode(
        turn_id="turn-fre-343",
        timestamp=datetime.now(timezone.utc),
        user_message="hi",
    )

    await service.create_conversation(turn, user_id=uid, visibility="group")

    # Exactly one statement must be the PARTICIPATED_IN MERGE.
    participated = [c for c in captured_cypher if "PARTICIPATED_IN" in c]
    assert len(participated) == 1, f"expected one PARTICIPATED_IN MERGE, got {len(participated)}"

    cypher = participated[0]
    assert "MATCH (p:Person {user_id: $user_id})" in cypher
    assert "MATCH (t:Turn {turn_id: $turn_id})" in cypher
    assert "MERGE (p)-[r:PARTICIPATED_IN]->(t)" in cypher
    assert "ON CREATE SET r.created_at = $timestamp" in cypher

    # The user_id was passed through.
    idx = captured_cypher.index(cypher)
    assert captured_kwargs[idx].get("user_id") == str(uid)


@pytest.mark.asyncio
async def test_create_conversation_participated_in_after_turn_merge() -> None:
    """Order: Turn MERGE first, PARTICIPATED_IN second (before entity loop)."""
    service, mock_session = _make_service_with_mock()

    captured_cypher: list[str] = []

    async def capture_run(cypher: str, **kwargs: object):
        captured_cypher.append(cypher)
        return AsyncMock()

    mock_session.run = AsyncMock(side_effect=capture_run)

    turn = TurnNode(
        turn_id="turn-order",
        timestamp=datetime.now(timezone.utc),
        user_message="ping",
        key_entities=["Berlin"],
    )
    await service.create_conversation(turn, user_id=uuid4(), visibility="group")

    indices = {
        "turn_merge": next(i for i, c in enumerate(captured_cypher) if "MERGE (t:Turn {turn_id:" in c),
        "participated": next(i for i, c in enumerate(captured_cypher) if "PARTICIPATED_IN" in c),
        "entity_loop": next(i for i, c in enumerate(captured_cypher) if "DISCUSSES" in c),
    }
    assert indices["turn_merge"] < indices["participated"] < indices["entity_loop"]
```

- [ ] **Step 2: Run the new tests — verify they fail**

Run: `uv run pytest tests/personal_agent/memory/test_create_conversation_user_id_propagation.py -v`
Expected: FAIL — `create_conversation` doesn't take `user_id` yet; `TypeError: create_conversation() got an unexpected keyword argument 'user_id'`.

- [ ] **Step 3: Modify `create_conversation` signature and body**

In `src/personal_agent/memory/service.py`, change the signature at line 306:

```python
    async def create_conversation(
        self,
        conversation: TurnNode,
        user_id: UUID,
        visibility: str = "public",
    ) -> bool:
```

Update the docstring to document `user_id` (Google style):

```python
        """Create a Turn node in the graph and link the participating user.

        Args:
            conversation: Turn node to create (accepts TurnNode or legacy ConversationNode).
            user_id: UUID of the connected user. MUST exist as :Person {user_id}
                in the graph (created by get_or_provision_user_person at first
                request per FRE-213). MATCH (not MERGE) — missing :Person is a
                logic bug and will be caught by the bool return path.
            visibility: Visibility scope for the Turn node (FRE-229). Defaults
                to "public" for backward compatibility; callers should pass
                "group" for authenticated sessions.

        Returns:
            True if successful, False otherwise.
        """
```

After the Turn MERGE block (currently lines 332-357), and before the entity loop (`# Create Turn→Entity DISCUSSES edges.` comment at line 359), insert:

```python
                # FRE-343: provenance edge linking the user to this Turn.
                # MATCH (not MERGE) on :Person — the node must exist
                # (get_or_provision_user_person runs on first auth request).
                await session.run(
                    """
                    MATCH (p:Person {user_id: $user_id})
                    MATCH (t:Turn {turn_id: $turn_id})
                    MERGE (p)-[r:PARTICIPATED_IN]->(t)
                    ON CREATE SET r.created_at = $timestamp
                    """,
                    user_id=str(user_id),
                    turn_id=turn_id,
                    timestamp=conversation.timestamp.isoformat(),
                )
                log.info(
                    "participated_in_edge_written",
                    turn_id=turn_id,
                    user_id=str(user_id),
                    was_backfilled=False,
                )
```

Ensure `from uuid import UUID` is at the top of the file (it likely already is — verify with grep).

- [ ] **Step 4: Run the new tests — verify they pass**

Run: `uv run pytest tests/personal_agent/memory/test_create_conversation_user_id_propagation.py -v`
Expected: both PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/memory/service.py \
        tests/personal_agent/memory/test_create_conversation_user_id_propagation.py
git commit -m "feat(fre-343): MERGE PARTICIPATED_IN edge in create_conversation

After Turn MERGE, before the entity loop, write the W3C-PROV provenance
edge linking :Person {user_id} to the Turn. MATCH on :Person — node
must exist (get_or_provision_user_person bootstraps it per FRE-213).
Emits participated_in_edge_written log event with trace_id."
```

---

## Task 3: Wire `user_id` through the consolidator

**Files:**
- Modify: `src/personal_agent/second_brain/consolidator.py:459-481`
- Modify: `tests/personal_agent/memory/test_visibility.py` (3 test cases)

- [ ] **Step 1: Update the existing visibility tests**

In `tests/personal_agent/memory/test_visibility.py:308-408`:

For `test_create_conversation_passes_visibility` (line 308), `test_create_conversation_entity_loop_cypher_order` (line 335), and `test_create_conversation_default_public` (line 387), each call to `service.create_conversation(turn, …)` becomes `service.create_conversation(turn, user_id=uuid4(), …)`. Add `from uuid import uuid4` at the top of the file if not present.

For `test_create_conversation_default_public` specifically — the test asserts `visibility="public"` is the default. The default still exists (only the user_id parameter is added), so the test still passes once `user_id=uuid4()` is added. **Keep the test**; don't delete it.

- [ ] **Step 2: Run the updated visibility tests — verify they pass**

Run: `uv run pytest tests/personal_agent/memory/test_visibility.py -v`
Expected: all PASS.

- [ ] **Step 3: Update the consolidator call-site**

In `src/personal_agent/second_brain/consolidator.py:459`, replace:

```python
        # FRE-229: set visibility based on whether the capture has an owning user.
        # Authenticated sessions produce "group" nodes (visible to all CF Access users);
        # CLI/unauthenticated captures (user_id=None) produce "public" nodes.
        visibility = "group" if getattr(capture, "user_id", None) else "public"
```

with:

```python
        # FRE-343: all captures have user_id (TaskCapture.user_id is non-optional).
        # Authenticated sessions produce "group"-visibility nodes.
        visibility = "group"
```

At line 481, change:

```python
        await self.memory_service.create_conversation(turn, visibility=visibility)
```

to:

```python
        await self.memory_service.create_conversation(
            turn, user_id=capture.user_id, visibility=visibility
        )
```

- [ ] **Step 4: Run consolidator tests**

```bash
uv run pytest tests/test_second_brain/test_consolidation_e2e.py -v
```

Expected: PASS (test fixtures already have `user_id` set from Task 1 Step 6).

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/second_brain/consolidator.py \
        tests/personal_agent/memory/test_visibility.py
git commit -m "feat(fre-343): consolidator passes user_id to create_conversation

visibility is unconditionally 'group' now that user_id is non-optional.
Drops the FRE-229 None-branch which is unreachable after TaskCapture
tightening."
```

---

## Task 4: Live Neo4j test for the edge

**Files:**
- Create: `tests/personal_agent/memory/test_participated_in_edge.py`

This test exercises a real Neo4j instance. It needs `make up` infra running, and uses the same fixture pattern as `test_memory_service.py`.

- [ ] **Step 1: Inspect existing live-Neo4j fixture pattern**

Run: `grep -n "memory_service\b\|clean_test_data\|@pytest.fixture" tests/test_memory/test_memory_service.py | head -20`
Goal: confirm the fixture name and conftest location so the new test can reuse them.

- [ ] **Step 2: Write the live test**

Create `tests/personal_agent/memory/test_participated_in_edge.py`:

```python
"""Live Neo4j test for the PARTICIPATED_IN provenance edge (FRE-343).

Requires `make up` infra. Marked `integration` so it stays out of
the unit-only `make test` run.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest

from personal_agent.memory.models import TurnNode

pytestmark = pytest.mark.integration


@pytest.fixture
async def seeded_user(memory_service, clean_test_data) -> UUID:
    """Provision a :Person node for a test user (mirrors FRE-213 path)."""
    uid = uuid4()
    await memory_service.get_or_provision_user_person(user_id=uid)
    return uid


@pytest.mark.asyncio
async def test_participated_in_edge_is_created(memory_service, seeded_user) -> None:
    """create_conversation writes a (:Person)-[:PARTICIPATED_IN]->(:Turn) edge."""
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello world",
    )
    ok = await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")
    assert ok is True

    async with memory_service.driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN r.created_at AS created_at
            """,
            uid=str(seeded_user),
            tid=turn.turn_id,
        )
        record = await result.single()
    assert record is not None
    assert record["created_at"] == turn.timestamp.isoformat()


@pytest.mark.asyncio
async def test_participated_in_edge_is_idempotent(memory_service, seeded_user) -> None:
    """Calling create_conversation twice produces exactly one edge."""
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello again",
    )
    await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")
    await memory_service.create_conversation(turn, user_id=seeded_user, visibility="group")

    async with memory_service.driver.session() as session:
        result = await session.run(
            """
            MATCH (p:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN count(r) AS cnt
            """,
            uid=str(seeded_user),
            tid=turn.turn_id,
        )
        record = await result.single()
    assert record["cnt"] == 1


@pytest.mark.asyncio
async def test_participated_in_skipped_when_person_missing(memory_service, clean_test_data) -> None:
    """MATCH on a non-existent :Person silently writes no edge; Turn still created."""
    bogus_uid = uuid4()  # never provisioned — no :Person node exists
    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="orphan",
    )
    # create_conversation returns True (Turn was created); just no edge written.
    ok = await memory_service.create_conversation(turn, user_id=bogus_uid, visibility="group")
    assert ok is True

    async with memory_service.driver.session() as session:
        result = await session.run(
            """
            MATCH ()-[r:PARTICIPATED_IN]->(t:Turn {turn_id: $tid})
            RETURN count(r) AS cnt
            """,
            tid=turn.turn_id,
        )
        record = await result.single()
    assert record["cnt"] == 0
```

- [ ] **Step 3: Run the live tests**

Run: `uv run pytest tests/personal_agent/memory/test_participated_in_edge.py -v -m integration`
Expected: 3 PASS. Requires `make up` to be running.

- [ ] **Step 4: Commit**

```bash
git add tests/personal_agent/memory/test_participated_in_edge.py
git commit -m "test(fre-343): live Neo4j tests for PARTICIPATED_IN edge

Verifies idempotency (MERGE not duplicating) and that missing :Person
results in no edge being written (matches loose-MATCH behavior — Turn
itself is still created)."
```

---

## Task 5: Create the `recall_personal_history` tool

**Files:**
- Create: `src/personal_agent/tools/personal_history.py`
- Create: `tests/personal_agent/tools/test_recall_personal_history.py`

- [ ] **Step 1: Write the failing tool tests**

Create `tests/personal_agent/tools/test_recall_personal_history.py`:

```python
"""Unit tests for recall_personal_history (FRE-343)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.personal_history import recall_personal_history_executor


def _ctx(user_id):
    return SimpleNamespace(trace_id="trace-1", user_id=user_id)


def _mock_memory_service(records: list[dict]) -> MagicMock:
    """Build a connected MemoryService whose driver yields fixed records."""
    svc = MagicMock()
    svc.connected = True
    mock_session = AsyncMock()
    mock_session.__aenter__ = AsyncMock(return_value=mock_session)
    mock_session.__aexit__ = AsyncMock(return_value=None)

    result = AsyncMock()
    result.data = AsyncMock(return_value=records)
    mock_session.run = AsyncMock(return_value=result)

    svc.driver = MagicMock()
    svc.driver.session = lambda: mock_session
    return svc


@pytest.mark.asyncio
async def test_missing_user_id_raises(monkeypatch) -> None:
    """ctx.user_id=None raises ToolExecutionError with the 'bug' marker."""
    with pytest.raises(ToolExecutionError, match="missing_user_id"):
        await recall_personal_history_executor(days_ago=7, ctx=_ctx(None))


@pytest.mark.asyncio
async def test_returns_user_scoped_turns(monkeypatch) -> None:
    """Happy path — turns returned by the driver are surfaced unchanged."""
    uid = uuid4()
    now = datetime.now(timezone.utc)
    records = [
        {
            "turn_id": "t1",
            "timestamp": (now - timedelta(days=2)).isoformat(),
            "session_id": "s1",
            "user_message": "Athens trip planning",
            "summary": "discussed Athens itinerary",
            "entities": ["Athens", "Acropolis"],
        },
    ]
    svc = _mock_memory_service(records)
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    out = await recall_personal_history_executor(days_ago=7, ctx=_ctx(uid))

    assert out["total"] == 1
    assert out["window_days"] == 7
    assert out["user_id"] == str(uid)
    assert out["turns"][0]["turn_id"] == "t1"
    assert out["turns"][0]["entities"] == ["Athens", "Acropolis"]


@pytest.mark.asyncio
async def test_days_ago_out_of_range_raises(monkeypatch) -> None:
    """days_ago must be 1..365."""
    uid = uuid4()
    with pytest.raises(ToolExecutionError, match="days_ago"):
        await recall_personal_history_executor(days_ago=0, ctx=_ctx(uid))
    with pytest.raises(ToolExecutionError, match="days_ago"):
        await recall_personal_history_executor(days_ago=400, ctx=_ctx(uid))


@pytest.mark.asyncio
async def test_limit_clamped_to_1_50(monkeypatch) -> None:
    """limit is clamped, not rejected."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, limit=999, ctx=_ctx(uid))
    # Last call kwargs include limit=50
    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("limit") == 50

    await recall_personal_history_executor(days_ago=7, limit=0, ctx=_ctx(uid))
    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("limit") == 1


@pytest.mark.asyncio
async def test_cypher_contains_topic_filter_when_set(monkeypatch) -> None:
    """topic substring appears as a Cypher parameter."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, topic="Athens", ctx=_ctx(uid))

    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("topic") == "Athens"
    assert called_kwargs.get("user_id") == str(uid)


@pytest.mark.asyncio
async def test_topic_none_passes_null(monkeypatch) -> None:
    """When topic is unset, the Cypher parameter is None (drives WHERE branch)."""
    uid = uuid4()
    svc = _mock_memory_service([])
    monkeypatch.setattr("personal_agent.tools.personal_history._get_memory_service", lambda: svc)

    await recall_personal_history_executor(days_ago=7, ctx=_ctx(uid))

    called_kwargs = svc.driver.session().__aenter__.return_value.run.call_args.kwargs  # type: ignore[union-attr]
    assert called_kwargs.get("topic") is None
```

- [ ] **Step 2: Run the new tests — verify they fail**

Run: `uv run pytest tests/personal_agent/tools/test_recall_personal_history.py -v`
Expected: ImportError / ModuleNotFoundError on `personal_agent.tools.personal_history`.

- [ ] **Step 3: Write the tool**

Create `src/personal_agent/tools/personal_history.py`:

```python
"""FRE-343 — recall_personal_history tool.

Retrieves the connected user's own past turns within a time window via the
(:Person)-[:PARTICIPATED_IN]->(:Turn) provenance edge. Use only when the
user explicitly refers to their personal history; general knowledge
questions stay on search_memory.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from personal_agent.telemetry import get_logger
from personal_agent.tools.executor import ToolExecutionError
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


recall_personal_history_tool = ToolDefinition(
    name="recall_personal_history",
    description=(
        "Retrieve the connected user's own past turns within a time window. "
        "Use ONLY when the user explicitly refers to their personal history — "
        "phrasing like 'we talked about', 'what did I ask', 'remind me what I said', "
        "'my conversation last week'. For general knowledge questions "
        "('what do we know about X', 'tell me about Y'), use search_memory instead — "
        "that searches the full shared graph."
    ),
    category="memory",
    parameters=[
        ToolParameter(
            name="days_ago",
            type="number",
            description=(
                "How many days back to look. 1 = last 24 hours, 7 = past week. "
                "Range 1..365."
            ),
            required=True,
        ),
        ToolParameter(
            name="topic",
            type="string",
            description=(
                "Optional substring filter applied to user_message (case-insensitive). "
                "Example: topic='Athens' narrows to turns whose message contains 'athens'."
            ),
            required=False,
            default=None,
            json_schema=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Max turns to return (1..50, default 10).",
            required=False,
            default=None,
            json_schema=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=30,
    rate_limit_per_hour=None,
)


def _get_memory_service():
    """Resolve the global MemoryService at call time.

    Indirection makes the dependency monkeypatch-able in tests.
    """
    try:
        from personal_agent.service.app import memory_service as global_memory_service
    except (ImportError, AttributeError):
        return None
    return global_memory_service


async def recall_personal_history_executor(
    days_ago: int,
    topic: str | None = None,
    limit: int | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Retrieve the connected user's own past turns within a time window.

    Args:
        days_ago: How many days back to look (1..365).
        topic: Optional case-insensitive substring filter on user_message.
        limit: Max turns to return (1..50, default 10).
        ctx: Trace context. ``ctx.user_id`` must be present — it identifies
            the :Person node whose PARTICIPATED_IN edges anchor the query.

    Returns:
        Dict with turns, total, window_days, and user_id (for trace correlation).

    Raises:
        ToolExecutionError: ctx.user_id missing, days_ago out of range, or
            memory service unavailable.
    """
    user_id = getattr(ctx, "user_id", None) if ctx else None
    trace_id = getattr(ctx, "trace_id", "unknown") if ctx else "unknown"

    if user_id is None:
        raise ToolExecutionError(
            "missing_user_id — this is a bug; report it (FRE-343). "
            "recall_personal_history requires ctx.user_id."
        )

    days_ago_int = int(days_ago)
    if days_ago_int < 1 or days_ago_int > 365:
        raise ToolExecutionError(
            f"days_ago must be between 1 and 365, got {days_ago_int}"
        )

    effective_limit = min(max(int(limit) if limit else 10, 1), 50)
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days_ago_int)).isoformat()

    log.info(
        "recall_personal_history_called",
        trace_id=trace_id,
        user_id=str(user_id),
        days_ago=days_ago_int,
        topic_set=topic is not None,
        limit=effective_limit,
    )

    svc = _get_memory_service()
    if svc is None or not getattr(svc, "connected", False):
        raise ToolExecutionError("Memory service unavailable or not connected.")

    cypher = """
        MATCH (p:Person {user_id: $user_id})-[:PARTICIPATED_IN]->(t:Turn)
        WHERE t.timestamp >= $cutoff
          AND ($topic IS NULL OR toLower(t.user_message) CONTAINS toLower($topic))
        OPTIONAL MATCH (t)-[:DISCUSSES]->(e:Entity)
        WITH t, collect(DISTINCT e.name) AS entities
        RETURN t.turn_id      AS turn_id,
               t.timestamp    AS timestamp,
               t.session_id   AS session_id,
               t.user_message AS user_message,
               t.summary      AS summary,
               entities       AS entities
        ORDER BY t.timestamp DESC
        LIMIT $limit
    """

    async with svc.driver.session() as session:
        result = await session.run(
            cypher,
            user_id=str(user_id),
            cutoff=cutoff,
            topic=topic,
            limit=effective_limit,
        )
        records = await result.data()

    turns = [
        {
            "turn_id": r["turn_id"],
            "timestamp": r["timestamp"],
            "session_id": r["session_id"],
            "user_message": (r.get("user_message") or "")[:300],
            "summary": r.get("summary") or "",
            "entities": r.get("entities") or [],
        }
        for r in records
    ]

    log.info(
        "personal_history_recalled",
        trace_id=trace_id,
        turn_count=len(turns),
        days_ago=days_ago_int,
        topic_set=topic is not None,
        user_id=str(user_id),
    )

    return {
        "turns": turns,
        "total": len(turns),
        "window_days": days_ago_int,
        "user_id": str(user_id),
    }
```

- [ ] **Step 4: Run the new tests — verify they pass**

Run: `uv run pytest tests/personal_agent/tools/test_recall_personal_history.py -v`
Expected: all 6 PASS.

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/tools/personal_history.py \
        tests/personal_agent/tools/test_recall_personal_history.py
git commit -m "feat(fre-343): recall_personal_history tool

Native Tier-1 Python tool. ctx.user_id required (loud failure if missing —
that is a bug after FRE-343). Cypher traverses :Person->PARTICIPATED_IN->
:Turn with optional substring filter on user_message; results sorted
descending by timestamp; limit clamped to 1..50.

Emits recall_personal_history_called + personal_history_recalled log
events with trace_id."
```

---

## Task 6: Register the tool and add governance entry

**Files:**
- Modify: `src/personal_agent/tools/__init__.py`
- Modify: `config/governance/tools.yaml`

- [ ] **Step 1: Register the tool**

In `src/personal_agent/tools/__init__.py`, find the existing search_memory imports/registration (lines 28-29 and ~81) and add parallel entries for `recall_personal_history`:

After the `search_memory_executor, search_memory_tool` import block, add:

```python
from personal_agent.tools.personal_history import (
    recall_personal_history_executor,
    recall_personal_history_tool,
)
```

After `registry.register(search_memory_tool, search_memory_executor)` (line ~81), add:

```python
    registry.register(recall_personal_history_tool, recall_personal_history_executor)
```

Update the module docstring's tool list (around line 62, where `- search_memory:` appears) to include:

```
- recall_personal_history: Personal time-window retrieval scoped to ctx.user_id (FRE-343)
```

- [ ] **Step 2: Add governance entry**

In `config/governance/tools.yaml`, find the `recall:` or `search_memory` entry under the `tools:` block (use `grep -n "^  search_memory\|^  read:" config/governance/tools.yaml` to locate it). Add this entry near `search_memory`:

```yaml
  recall_personal_history:
    category: "read_only"
    allowed_in_modes: ["NORMAL", "ALERT", "DEGRADED", "LOCKDOWN", "RECOVERY"]
    requires_approval: false
    requires_sandbox: false
    risk_level: "low"
    rate_limit_per_hour: null
    description: "FRE-343 — personal time-window retrieval scoped to ctx.user_id"
```

If `search_memory` does not have an entry in `tools.yaml` (some tools are only registered in Python), still add the `recall_personal_history` entry — it's needed for action-boundary governance per ADR-0063.

- [ ] **Step 3: Run the registration smoke test**

```bash
uv run python -c "
from personal_agent.tools import build_registry_default
registry = build_registry_default()
defs = registry.get_tool_definitions_for_llm(allowed_modes=['NORMAL'])
names = [d.name for d in defs]
assert 'recall_personal_history' in names, names
print('OK — recall_personal_history is registered')
"
```

Expected: prints `OK — recall_personal_history is registered`.

If `build_registry_default` isn't the right name, use the existing pattern from `tools/__init__.py` (the same call that boots the gateway at startup).

- [ ] **Step 4: Run the contract test that validates governance**

```bash
uv run pytest tests/personal_agent/tools/ -v -k "governance or registry or contract" 2>/dev/null | tail -20
```

Expected: PASS (or no matching tests — that's fine; the smoke test in Step 3 is the authoritative check).

- [ ] **Step 5: Commit**

```bash
git add src/personal_agent/tools/__init__.py config/governance/tools.yaml
git commit -m "feat(fre-343): register recall_personal_history + governance

Adds the tool to ToolRegistry and an action-boundary governance entry
under config/governance/tools.yaml. Low-risk read-only; all modes
including LOCKDOWN (data scope is narrower than search_memory)."
```

---

## Task 7: Author the SKILL.md (with XML-pilot body)

**Files:**
- Create: `docs/skills/personal-history-recall.md`

- [ ] **Step 1: Write the skill doc**

Create `docs/skills/personal-history-recall.md`:

```markdown
---
name: personal-history-recall
description: Retrieve the connected user's own past turns within a time window via the recall_personal_history tool. Use only when the user refers to *their* history; for general questions, use search_memory.
when_to_use: When the user's phrasing scopes to themselves — 'we talked about', 'what did I ask', 'remind me what I said', 'last week', 'yesterday', 'days ago'. Not for general knowledge questions ('what do we know about X') — those stay on search_memory.
tools: [recall_personal_history]
nudge: "Match the user's scoping. 'We/I/my/us' → recall_personal_history. 'What do we know about X' → search_memory (shared graph)."
keywords:
  - what did we
  - what did I
  - we talked about
  - we discussed
  - did we
  - remind me what
  - last time we
  - my conversation
  - my history
  - I told you
  - I mentioned
  - I asked
  - last week
  - yesterday
  - earlier this week
  - days ago
---

# SKILL: personal-history-recall

> **Tier:** 1 — native tool
> **Tool:** `recall_personal_history`
> **ADR:** [ADR-0052 §Update 2026-05-14](../architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md)

---

## What this skill does

Retrieve the **connected user's own past turns** within a time window. This is the explicit, opt-in narrowing of memory recall — the agent's default is the shared knowledge graph (`search_memory`), which surfaces what *anyone* has contributed. Use this skill only when the user's phrasing scopes to themselves.

---

## When to use vs `search_memory`

<when_to_use>
  Use recall_personal_history when the user scopes to themselves:
    - "we talked about …", "what did we discuss …"
    - "I asked", "I told you", "I mentioned"
    - "my conversation last week", "remind me what I said"

  Use search_memory (the default) when the user asks a general question:
    - "what do we know about X"
    - "tell me about the Acropolis"
    - "find conversations about travel planning"

  The shared graph is the default. Personal-history is an explicit narrowing.
</when_to_use>

---

## Worked examples

<example>
  User: What did we talk about last Tuesday?
  Today is Wednesday; "last Tuesday" = 8 days ago.
  Call: recall_personal_history(days_ago=8)
</example>

<example>
  User: Remind me what I told you about the Athens trip.
  "Remind me" — personal scope. Topic substring: "Athens". 30 days is a safe default.
  Call: recall_personal_history(days_ago=30, topic="Athens")
</example>

<anti_example>
  User: What do we know about the Acropolis?
  This is a general knowledge question — the agent should surface anyone's
  contributions, not just the connected user's. Use the shared graph.
  Call: search_memory(query_text="Acropolis")
  Do NOT call recall_personal_history — that would hide shared knowledge.
</anti_example>

---

## Time-phrase cheat sheet

| Phrase | `days_ago` |
|---|---|
| yesterday | 1 |
| earlier this week | 3 |
| last week | 7 |
| earlier this month | 14 |
| last month | 30 |
| last quarter | 90 |

For specific weekdays ("last Tuesday"), compute the offset from today. The LLM does the math; the tool only takes integer `days_ago`.

---

## Returned shape

```json
{
  "turns": [
    {
      "turn_id": "trace-abc123",
      "timestamp": "2026-05-12T18:30:00+00:00",
      "session_id": "sess-xyz",
      "user_message": "Let's plan a trip to Athens...",
      "summary": "discussed Athens itinerary",
      "entities": ["Athens", "Acropolis"]
    }
  ],
  "total": 1,
  "window_days": 7,
  "user_id": "..."
}
```

---

## Notes

- The tool fails loudly if `ctx.user_id` is missing — that is a bug after FRE-343, not a fallback condition.
- For purely topical recall ("what's a good Greek restaurant?"), prefer `search_memory` — it surfaces other users' contributions.
- The `topic` filter is a case-insensitive substring on `user_message`. It does not yet do semantic search; for fuzzy matches use `search_memory(query_text=..., recency_days=N)`.

See also: [search_memory tool](../skills/seshat-knowledge.md)
```

- [ ] **Step 2: Validate the skill loads cleanly**

Run:

```bash
uv run python -c "
from pathlib import Path
import yaml
content = Path('docs/skills/personal-history-recall.md').read_text()
fm = content.split('---', 2)[1]
data = yaml.safe_load(fm)
assert data['name'] == 'personal-history-recall', data
assert 'nudge' in data and len(data['nudge']) > 0
assert len(data['keywords']) >= 10
print('OK')
"
```

Expected: `OK`.

- [ ] **Step 3: Commit**

```bash
git add docs/skills/personal-history-recall.md
git commit -m "docs(skills): personal-history-recall SKILL.md (FRE-343)

Frontmatter follows the FRE-337/ADR-0067 pattern (keywords, nudge,
when_to_use, tools). Body pilots semantic XML inside the markdown
(<when_to_use>, <example>, <anti_example>) — same Anthropic
prompt-engineering guidance commit 4b67b5c cited when introducing
runtime <skill_library> wrapping. First skill to apply the principle
author-side; not retroactive."
```

---

## Task 8: Backfill script + tests

**Files:**
- Create: `scripts/backfill_participated_in.py`
- Create: `tests/scripts/test_backfill_participated_in.py`

- [ ] **Step 1: Inspect existing one-shot script patterns**

Run: `ls scripts/ | head -20` and `head -50 scripts/init-services.sh 2>/dev/null || ls scripts/*.py 2>/dev/null | head -5`. Goal: match an existing Python-script naming + entry-point style. If no Python scripts exist in `scripts/`, mirror this plan's structure.

- [ ] **Step 2: Write the backfill script**

Create `scripts/backfill_participated_in.py`:

```python
"""FRE-343 one-shot backfill — populate (:Person)-[:PARTICIPATED_IN]->(:Turn) edges.

Idempotent. Algorithm:
  1. Resolve OWNER_UUID from settings.agent_owner_email.
  2. Stream all Sessions from Postgres.
  3. For each Session: target_uid = session.user_id OR OWNER_UUID (NULL fallback).
  4. MERGE the edge in Neo4j for every Turn in that Session.

Run once after the FRE-343 PR merges:
    uv run python -m scripts.backfill_participated_in

Re-runs are safe (MERGE + ON CREATE SET).
"""

from __future__ import annotations

import asyncio
import sys
from uuid import UUID

from neo4j import AsyncGraphDatabase
from sqlalchemy.ext.asyncio import create_async_engine

from personal_agent.config import settings
from personal_agent.service.auth import get_or_create_user_by_email
from personal_agent.telemetry import get_logger

log = get_logger(__name__)


async def _resolve_owner_uuid() -> UUID:
    """Look up the owner's UUID from Postgres."""
    engine = create_async_engine(settings.database_url)
    try:
        from sqlalchemy.ext.asyncio import AsyncSession

        async with AsyncSession(engine) as db:
            uid = await get_or_create_user_by_email(db, settings.agent_owner_email)
            await db.commit()
            return uid
    finally:
        await engine.dispose()


async def _stream_sessions() -> list[tuple[str, UUID | None]]:
    """Stream (session_id, user_id) tuples from Postgres."""
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import AsyncSession

    engine = create_async_engine(settings.database_url)
    try:
        async with AsyncSession(engine) as db:
            result = await db.execute(
                text("SELECT session_id, user_id FROM sessions ORDER BY created_at")
            )
            rows = result.fetchall()
    finally:
        await engine.dispose()

    return [(str(r[0]), UUID(str(r[1])) if r[1] else None) for r in rows]


async def _backfill_session(
    session,
    session_id: str,
    target_uid: UUID,
    source: str,
) -> dict[str, int]:
    """MERGE the PARTICIPATED_IN edges for one Session's Turns.

    Returns:
        {'created': N, 'existed': M, 'turns': K}
    """
    result = await session.run(
        """
        MATCH (t:Turn {session_id: $session_id})
        WITH t
        MATCH (p:Person {user_id: $target_uid})
        MERGE (p)-[r:PARTICIPATED_IN]->(t)
          ON CREATE SET r.created_at = t.timestamp,
                        r.backfilled = true
        RETURN
          count(t) AS turns,
          sum(CASE WHEN r.backfilled = true THEN 1 ELSE 0 END) AS backfilled_count
        """,
        session_id=session_id,
        target_uid=str(target_uid),
    )
    record = await result.single()
    if record is None:
        return {"turns": 0, "created": 0, "existed": 0}

    turns = record["turns"] or 0
    backfilled_count = record["backfilled_count"] or 0
    # An edge is "existed" if it was already there (no r.backfilled=true flag).
    existed = max(turns - backfilled_count, 0)
    created = backfilled_count

    log.info(
        "backfill_participated_in_edges",
        session_id=session_id,
        user_id=str(target_uid),
        user_id_source=source,
        edges_created=created,
        edges_existed=existed,
    )
    return {"turns": turns, "created": created, "existed": existed}


async def _verify_owner_person(neo4j_driver, owner_uuid: UUID) -> None:
    """Fail loud if the owner's :Person node doesn't exist."""
    async with neo4j_driver.session() as session:
        result = await session.run(
            "MATCH (p:Person {user_id: $uid}) RETURN p LIMIT 1",
            uid=str(owner_uuid),
        )
        record = await result.single()
    if record is None:
        raise RuntimeError(
            f"Owner :Person {{user_id: {owner_uuid}}} not found in Neo4j. "
            "Has the FRE-213 bootstrap (get_or_provision_user_person) run? "
            "This script cannot continue without an owner anchor."
        )


async def run_backfill() -> dict[str, int]:
    """Main entrypoint — returns aggregate counts."""
    owner_uuid = await _resolve_owner_uuid()
    log.info("backfill_owner_resolved", owner_uuid=str(owner_uuid))

    sessions = await _stream_sessions()
    log.info("backfill_sessions_loaded", session_count=len(sessions))

    neo4j_driver = AsyncGraphDatabase.driver(
        settings.neo4j_uri,
        auth=(settings.neo4j_user, settings.neo4j_password),
    )
    try:
        await _verify_owner_person(neo4j_driver, owner_uuid)

        totals = {"sessions_processed": 0, "edges_created": 0, "edges_existed": 0}
        async with neo4j_driver.session() as session:
            for session_id, sess_user_id in sessions:
                target_uid = sess_user_id if sess_user_id else owner_uuid
                source = "session" if sess_user_id else "owner_fallback"
                counts = await _backfill_session(session, session_id, target_uid, source)
                totals["sessions_processed"] += 1
                totals["edges_created"] += counts["created"]
                totals["edges_existed"] += counts["existed"]

        log.info("backfill_summary", **totals)
        return totals
    finally:
        await neo4j_driver.close()


def main() -> int:
    """Synchronous CLI entrypoint."""
    try:
        totals = asyncio.run(run_backfill())
    except Exception as e:  # noqa: BLE001
        log.error("backfill_failed", error=str(e), exc_info=True)
        print(f"BACKFILL FAILED: {e}", file=sys.stderr)
        return 1

    print(
        f"Backfill complete: {totals['sessions_processed']} sessions; "
        f"{totals['edges_created']} edges created, "
        f"{totals['edges_existed']} edges already existed."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 3: Write the backfill test**

Create `tests/scripts/test_backfill_participated_in.py`:

```python
"""Tests for the FRE-343 backfill script.

Uses a real Neo4j + Postgres (via `make up`). Marked integration.
"""

from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from personal_agent.config import settings
from personal_agent.memory.models import TurnNode
from scripts.backfill_participated_in import run_backfill

pytestmark = pytest.mark.integration


async def _insert_session(engine, session_id: str, user_id: UUID | None) -> None:
    """Insert a row into the sessions table for backfill input."""
    async with AsyncSession(engine) as db:
        await db.execute(
            text(
                "INSERT INTO sessions (session_id, user_id, created_at) "
                "VALUES (:sid, :uid, :ts) ON CONFLICT DO NOTHING"
            ),
            {"sid": session_id, "uid": user_id, "ts": datetime.now(timezone.utc)},
        )
        await db.commit()


@pytest.fixture
async def pg_engine():
    engine = create_async_engine(settings.database_url)
    yield engine
    await engine.dispose()


@pytest.mark.asyncio
async def test_backfill_uses_session_user_id_when_set(
    memory_service, clean_test_data, pg_engine
) -> None:
    """A Session with user_id set → edge is MERGEd to that :Person."""
    uid = uuid4()
    await memory_service.get_or_provision_user_person(user_id=uid)

    session_id = f"sess-{uuid4()}"
    await _insert_session(pg_engine, session_id, uid)

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="hello",
    )
    turn.session_id = session_id
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    # Drop the live edge first to simulate pre-backfill state.
    async with memory_service.driver.session() as s:
        await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) DELETE r",
            uid=str(uid),
            tid=turn.turn_id,
        )

    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN r.backfilled AS bf",
            uid=str(uid),
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec is not None
    assert rec["bf"] is True


@pytest.mark.asyncio
async def test_backfill_uses_owner_fallback_for_null_session_user(
    memory_service, clean_test_data, pg_engine
) -> None:
    """A Session with user_id=NULL → edge is MERGEd to owner's :Person."""
    # Trust the owner :Person was bootstrapped by Step 2's _verify_owner_person.
    session_id = f"sess-{uuid4()}"
    await _insert_session(pg_engine, session_id, None)

    # Owner UUID resolved the same way the script does.
    from personal_agent.service.auth import get_or_create_user_by_email

    async with AsyncSession(pg_engine) as db:
        owner_uid = await get_or_create_user_by_email(db, settings.agent_owner_email)
        await db.commit()

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="orphan",
    )
    turn.session_id = session_id
    # No prior edge — backfill creates it.
    await memory_service.create_conversation(turn, user_id=owner_uid, visibility="group")
    async with memory_service.driver.session() as s:
        await s.run(
            "MATCH ()-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) DELETE r",
            tid=turn.turn_id,
        )

    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (p:Person)-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN p.user_id AS uid",
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec is not None
    assert rec["uid"] == str(owner_uid)


@pytest.mark.asyncio
async def test_backfill_is_idempotent(memory_service, clean_test_data, pg_engine) -> None:
    """Running twice produces exactly one edge per (user, turn)."""
    uid = uuid4()
    await memory_service.get_or_provision_user_person(user_id=uid)

    session_id = f"sess-{uuid4()}"
    await _insert_session(pg_engine, session_id, uid)

    turn = TurnNode(
        turn_id=f"turn-{uuid4()}",
        timestamp=datetime.now(timezone.utc),
        user_message="idempotent",
    )
    turn.session_id = session_id
    await memory_service.create_conversation(turn, user_id=uid, visibility="group")

    await run_backfill()
    await run_backfill()

    async with memory_service.driver.session() as s:
        result = await s.run(
            "MATCH (:Person {user_id: $uid})-[r:PARTICIPATED_IN]->(:Turn {turn_id: $tid}) "
            "RETURN count(r) AS cnt",
            uid=str(uid),
            tid=turn.turn_id,
        )
        rec = await result.single()
    assert rec["cnt"] == 1
```

- [ ] **Step 4: Run the backfill tests**

Run: `uv run pytest tests/scripts/test_backfill_participated_in.py -v -m integration`
Expected: 3 PASS (requires `make up`).

- [ ] **Step 5: Commit**

```bash
git add scripts/backfill_participated_in.py tests/scripts/test_backfill_participated_in.py
git commit -m "feat(fre-343): one-shot PARTICIPATED_IN backfill script

Streams Sessions from Postgres; for each, picks target user
(session.user_id if set, else owner via settings.agent_owner_email) and
MERGEs the PARTICIPATED_IN edge for every Turn in that Session.
Idempotent (MERGE + ON CREATE SET r.backfilled=true). Fails loud if
the owner :Person is missing; warns + skips for other missing users."
```

---

## Task 9: Makefile target + Owner-Person verification on first run

**Files:**
- Modify: `Makefile`

- [ ] **Step 1: Add the backfill target**

In `Makefile`, find a small target like `dev:` or `ps:` to anchor the insertion. Add this target near other one-shot maintenance commands (look for `init-services` or similar):

```makefile
backfill-participated-in: ## one-shot backfill of (:Person)-[:PARTICIPATED_IN]->(:Turn) edges (FRE-343)
	uv run python -m scripts.backfill_participated_in
```

- [ ] **Step 2: Smoke check that `make` picks it up**

```bash
make help 2>/dev/null | grep backfill-participated-in || make -n backfill-participated-in
```

Expected: either listed in `make help`, or `make -n` prints the command without executing.

- [ ] **Step 3: Commit**

```bash
git add Makefile
git commit -m "chore(fre-343): make backfill-participated-in target"
```

---

## Task 10: ADR-0052 amendment

**Files:**
- Modify: `docs/architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md`

- [ ] **Step 1: Append the amendment**

Open `docs/architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md` and append at the end:

```markdown

---

## Update 2026-05-14 — Personal-history retrieval (FRE-343)

### Decision

Adopt the W3C-PROV provenance edge pattern:

```cypher
(:Person {user_id})-[:PARTICIPATED_IN {created_at, backfilled?}]->(:Turn {turn_id})
```

Written at Turn-save time inside `MemoryService.create_conversation` (atomic with the Turn MERGE, same Neo4j session). Read by a new native tool `recall_personal_history`. Default `search_memory` behavior — and the shared-knowledge-graph design — is **unchanged**.

### `user_id` invariant tightening

`TaskCapture.user_id` is now `UUID` (non-optional). This is the correct invariant because `service.auth.get_request_user` always resolves a `user_id` from one of three sources:
1. `Cf-Access-Authenticated-User-Email` header (CF Access, production path).
2. `settings.agent_owner_email` fallback (dev/CLI path).
3. HTTP 401 — request rejected.

The previous `user_id: UUID | None = None` was a defensive holdover, not a real production state.

### Rationale

`(:Person)-[:PARTICIPATED_IN]->(:Turn)` is the Neo4j-community recommended pattern for "multi-tenant shared-entity graphs with per-tenant interaction history" and aligns with W3C PROV's `(:Activity)-[:wasAssociatedWith]->(:Agent)`. Shared entities stay shared; provenance is modelled as edges, not as per-user copies of nodes. Forward-compatible with FRE-230 (Location) — `(:Turn)-[:OCCURRED_AT]->(:Location)` is a parallel additive edge.

See:
- Spec: `docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md`
- Research: `docs/research/2026-05-09-graph-identity-multi-user-patterns.md` §6
- Linear: [FRE-343](https://linear.app/frenchforest/issue/FRE-343)
```

- [ ] **Step 2: Commit**

```bash
git add docs/architecture_decisions/ADR-0052-seshat-owner-identity-primitive.md
git commit -m "docs(adr-0052): amendment 2026-05-14 — personal-history retrieval

Documents the W3C-PROV PARTICIPATED_IN edge decision (Option B from
FRE-343) and the TaskCapture.user_id non-optional tightening."
```

---

## Task 11: Integration test (full-stack scope-leak check)

**Files:**
- Create: `tests/integration/test_personal_history_e2e.py`

- [ ] **Step 1: Inspect existing integration test patterns**

Run: `ls tests/integration/ 2>/dev/null && grep -n "PERSONAL_AGENT_INTEGRATION\|requires_llm_server" tests/integration/*.py 2>/dev/null | head -10`. Goal: find an end-to-end test that hits `/chat` so the new one can mirror the auth-header pattern.

- [ ] **Step 2: Write the integration test**

Create `tests/integration/test_personal_history_e2e.py`:

```python
"""End-to-end test: scope-leak check for recall_personal_history (FRE-343).

User A sends a /chat about 'Athens'. After consolidation, User A calls
recall_personal_history and gets the Athens turn back. User B (parallel
session, different email) does NOT see User A's Athens turn.

Requires `make up` and a running gateway. Skipped unless
PERSONAL_AGENT_INTEGRATION=1 is set.
"""

from __future__ import annotations

import os
import time
from uuid import uuid4

import httpx
import pytest

pytestmark = pytest.mark.integration

if not os.environ.get("PERSONAL_AGENT_INTEGRATION"):
    pytest.skip(
        "PERSONAL_AGENT_INTEGRATION not set — skipping live-gateway test",
        allow_module_level=True,
    )


GATEWAY_URL = os.environ.get("AGENT_SERVICE_URL", "http://localhost:9000")


def _chat(email: str, message: str, session_id: str | None = None) -> dict:
    """Send a /chat request with the given CF Access email."""
    headers = {"Cf-Access-Authenticated-User-Email": email}
    params = {"message": message}
    if session_id:
        params["session_id"] = session_id
    r = httpx.post(f"{GATEWAY_URL}/chat", params=params, headers=headers, timeout=120.0)
    r.raise_for_status()
    return r.json()


def _wait_for_consolidation(seconds: int = 30) -> None:
    """Crude wait for the background consolidation pass to write a Turn."""
    time.sleep(seconds)


def test_athens_scope_does_not_leak_between_users() -> None:
    """User A's Athens turn is returned to A; not visible to B."""
    a_email = f"user-a-{uuid4()}@test.local"
    b_email = f"user-b-{uuid4()}@test.local"

    a_chat = _chat(a_email, "Plan a trip to Athens — what should I see?")
    assert a_chat.get("trace_id")

    _wait_for_consolidation()

    # Call recall_personal_history via /chat as User A — the LLM should pick the tool.
    a_recall = _chat(
        a_email,
        "remind me what we talked about earlier today",
        session_id=str(a_chat["session_id"]),
    )
    a_text = (a_recall.get("response") or "").lower()
    assert "athens" in a_text, f"Athens should appear in A's recall: {a_text[:300]}"

    # User B should not see A's Athens conversation.
    b_recall = _chat(
        b_email,
        "remind me what we talked about earlier today",
    )
    b_text = (b_recall.get("response") or "").lower()
    assert "athens" not in b_text, f"Athens leaked to B: {b_text[:300]}"
```

- [ ] **Step 3: Run the integration test (manual gate)**

Run only when the user explicitly wants the live e2e validation — this test bills LLM tokens and waits 30s. Not in the default `make test` run.

```bash
PERSONAL_AGENT_INTEGRATION=1 uv run pytest tests/integration/test_personal_history_e2e.py -v
```

Expected: PASS.

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_personal_history_e2e.py
git commit -m "test(fre-343): scope-leak e2e — User A's Athens turn not visible to B

Live-gateway test. Skipped by default; set PERSONAL_AGENT_INTEGRATION=1
to run. Verifies the cross-user isolation contract."
```

---

## Task 12: Final quality gates + MASTER_PLAN update

**Files:**
- Modify: `docs/plans/MASTER_PLAN.md`

- [ ] **Step 1: Run full quality gates**

```bash
make ruff-format
make ruff-check
make mypy
make test
```

Expected: clean run on all four. Fix any new ruff/mypy violations introduced by the diff before continuing.

- [ ] **Step 2: Update MASTER_PLAN.md**

In `docs/plans/MASTER_PLAN.md`:

a. Bump the `Last updated` line (currently `2026-05-14 — FRE-363 …`):

```
> **Last updated**: 2026-05-14 — FRE-343 personal time-window retrieval shipped (PR #TBD) — (:Person)-[:PARTICIPATED_IN]->(:Turn) provenance edge + recall_personal_history tool + skill nudge + backfill.
```

(Replace `#TBD` with the actual PR number after open.)

b. In the **Immediately Actionable** table, remove the `FRE-343` row.

c. In the **Recently Completed** table, add at the top:

```
| **FRE-343: Personal time-window retrieval (PR #TBD)** | 2026-05-14 | Adds (:Person)-[:PARTICIPATED_IN]->(:Turn) provenance edge (W3C-PROV pattern per ADR-0052 §Update 2026-05-14). New native tool `recall_personal_history` scoped to ctx.user_id with optional substring filter on user_message; complementary skill `personal-history-recall.md` with semantic-XML body pilot (<when_to_use>, <example>, <anti_example>). `TaskCapture.user_id` tightened to non-optional UUID — verified `get_request_user` always resolves one (CF Access or owner-email fallback or 401). One-shot idempotent backfill script claims orphan pre-FRE-213 sessions for the owner; multi-user sessions retain correct attribution. Default `search_memory` shared-graph behavior unchanged — personal-history is explicit opt-in via self-scoping phrasing. |
```

- [ ] **Step 3: Commit MASTER_PLAN update**

```bash
git add docs/plans/MASTER_PLAN.md
git commit -m "docs(plan): MASTER_PLAN — FRE-343 personal time-window retrieval shipped"
```

- [ ] **Step 4: Push branch and open PR**

```bash
git push -u origin <branch-name>
gh pr create --title "FRE-343: Personal time-window retrieval (PARTICIPATED_IN edge + tool + skill)" --body "$(cat <<'EOF'
## Summary

- Adds (:Person)-[:PARTICIPATED_IN]->(:Turn) provenance edge per W3C-PROV (Option B from FRE-343 / docs/research/2026-05-09-graph-identity-multi-user-patterns.md §6)
- New native tool `recall_personal_history(days_ago, topic?, limit?)` scoped to ctx.user_id — default `search_memory` behavior unchanged
- SKILL.md with semantic-XML body pilot (<when_to_use>, <example>, <anti_example>) extending the FRE-337 principle author-side
- `TaskCapture.user_id` tightened to non-optional UUID — verified `get_request_user` always resolves one
- One-shot idempotent backfill script + `make backfill-participated-in` target
- ADR-0052 amendment documenting the schema decision

## Test plan

- [ ] `make test` clean
- [ ] `make mypy` clean
- [ ] `make ruff-check` clean
- [ ] Live integration: deploy and run `make backfill-participated-in`
- [ ] PWA smoke: ask "what did we talk about yesterday" and verify a personal-scoped result
- [ ] Scope-leak check: PERSONAL_AGENT_INTEGRATION=1 pytest tests/integration/test_personal_history_e2e.py

See spec: docs/superpowers/specs/2026-05-14-fre-343-personal-time-window-retrieval-design.md
EOF
)"
```

- [ ] **Step 5: After PR merges, run the backfill against the deployed env**

```bash
make backfill-participated-in
```

Expected: prints the summary line; new edges land on existing Turns.

---

## Self-review

### Spec coverage

| Spec section | Implementing task(s) |
|---|---|
| §1 Schema (PARTICIPATED_IN edge) | Task 2 (write Cypher), Task 4 (live test) |
| §2 Write path (tighten + MERGE + call-site) | Task 1, Task 2, Task 3 |
| §3 Read path (`recall_personal_history`) | Task 5 (tool), Task 6 (registration + governance) |
| §4 Backfill | Task 8 (script + tests), Task 9 (Makefile) |
| §5 Skill doc with XML pilot | Task 7 |
| §6 Tests | Tasks 1, 2, 4, 5, 8, 11 |
| §7 Telemetry / governance / rollout | Task 5 (log events), Task 6 (governance), Task 10 (ADR), Task 12 (MASTER_PLAN + PR) |
| Acceptance criteria | All tasks combined |

No gaps.

### Placeholder scan

No "TBD"/"TODO" beyond `#TBD` for the PR number (resolved during Task 12 Step 4). No "implement later" — every step has full code or commands. No "similar to Task N" — every code block is self-contained.

### Type consistency

- `user_id: UUID` consistently (non-optional in TaskCapture, required param on `create_conversation`, required ctx attribute in the tool).
- `days_ago: int` after `int(days_ago)` coercion at the executor entry; same coercion in tests.
- `topic: str | None` consistently — `None` drives the Cypher branch.
- Tool name `recall_personal_history` matches between definition, registration, governance, skill `tools:` field, and test imports.
- Cypher edge name `PARTICIPATED_IN` matches across Task 2 (write), Task 4 (live test), Task 5 (read), Task 8 (backfill).

No drift.
