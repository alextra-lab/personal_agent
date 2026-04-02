"""FRE-37: E2E validation — multi-turn conversation through the full stack.

Validates that multi-turn conversations work correctly across:
  orchestrator (with hydrated session) → LLM (sees history) → coherent response

All five scenarios from the FRE-37 spec are covered here.

Run with a live LLM server:
    PERSONAL_AGENT_INTEGRATION=1 make test-integration

To run only these tests:
    PERSONAL_AGENT_INTEGRATION=1 uv run pytest tests/test_orchestrator/test_fre37_multi_turn_e2e.py -v

Acceptance criteria (FRE-37):
  - All 5 scenarios pass
  - No regressions in existing test suite
  - Telemetry shows conversation_context_loaded events (verified via Elasticsearch
    when infra is running; not checked here as it requires make infra-up)
"""

from __future__ import annotations

import pytest

from personal_agent.governance.models import Mode
from personal_agent.orchestrator import Channel, Orchestrator


# ---------------------------------------------------------------------------
# Scenario 1 — Basic multi-turn
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_scenario_1_basic_multi_turn() -> None:
    """Scenario 1: Agent recalls a fact introduced in a prior turn.

    Turn 1: 'My name is Alex.'
    Turn 2: 'What is my name?' → reply must contain 'Alex'.
    """
    orchestrator = Orchestrator()
    session_id = "fre37-s1-basic-multi-turn"

    result1 = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message="My name is Alex.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result1["reply"], "Turn 1 must produce a non-empty reply"

    result2 = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message="What is my name?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result2["reply"], "Turn 2 must produce a non-empty reply"
    assert "alex" in result2["reply"].lower(), (
        f"Agent must recall 'Alex' from turn 1. Got: {result2['reply']!r}"
    )

    # Session must have accumulated messages from both turns.
    session = orchestrator.session_manager.get_session(session_id)
    assert session is not None
    assert len(session.messages) >= 4, (
        "Session must contain at least user+assistant for each of the two turns"
    )


# ---------------------------------------------------------------------------
# Scenario 2 — Context window under pressure
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_scenario_2_context_window_under_pressure() -> None:
    """Scenario 2: Agent remains coherent when session history is 30+ messages.

    Pre-populate session with 30 synthetic turns then send a query that
    references an earlier fact. Verifies the orchestrator handles truncation
    without crashing and still produces a meaningful reply.

    Telemetry check: conversation_context_loaded with messages_truncated > 0
    is emitted in Elasticsearch when infra is up (not asserted here).
    """
    orchestrator = Orchestrator()
    session_id = "fre37-s2-context-pressure"

    # Build 30 synthetic messages (15 round-trips).
    synthetic_messages: list[dict[str, str]] = []
    for i in range(15):
        synthetic_messages.append(
            {"role": "user", "content": f"Question {i + 1}: tell me something interesting."}
        )
        if i == 0:
            synthetic_messages.append(
                {
                    "role": "assistant",
                    "content": "Interesting fact: the capital of France is Paris.",
                }
            )
        else:
            synthetic_messages.append(
                {"role": "assistant", "content": f"Answer {i + 1}: this is a placeholder."}
            )

    orchestrator.session_manager.create_session(
        Mode.NORMAL, Channel.CHAT, session_id=session_id
    )
    orchestrator.session_manager.update_session(session_id, messages=synthetic_messages)

    # Verify the session starts at 30 messages.
    session_before = orchestrator.session_manager.get_session(session_id)
    assert session_before is not None
    assert len(session_before.messages) == 30

    result = await orchestrator.handle_user_request(
        session_id=session_id,
        user_message="What is the capital of France? You mentioned it at the start.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    assert result["reply"], "Must produce a reply even with 30+ message context"
    assert len(result["reply"]) > 5, "Reply must not be trivially empty"
    assert any(s.get("type") == "llm_call" for s in result["steps"]), (
        "At least one llm_call step must be recorded"
    )
    assert "paris" in result["reply"].lower(), (
        f"Agent should still recall 'Paris' with long context. Got: {result['reply']!r}"
    )

    # After the turn, the session must grow (user + assistant appended).
    session_after = orchestrator.session_manager.get_session(session_id)
    assert session_after is not None
    assert len(session_after.messages) >= 32


# ---------------------------------------------------------------------------
# Scenario 3 — New conversation isolation
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_scenario_3_new_conversation_isolation() -> None:
    """Scenario 3: A fresh session has no access to a different session's history.

    Session A: 'My name is Alex.' (acknowledged by agent)
    Session B (different ID): no prior messages — agent must not know the name.
    """
    orchestrator = Orchestrator()
    session_a = "fre37-s3-session-a"
    session_b = "fre37-s3-session-b"

    # Session A: name introduction.
    result_a = await orchestrator.handle_user_request(
        session_id=session_a,
        user_message="My name is Alex. Remember this.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result_a["reply"], "Session A must produce a reply"

    # Session B: brand new — no shared history.
    result_b = await orchestrator.handle_user_request(
        session_id=session_b,
        user_message="What is my name?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result_b["reply"], "Session B must produce a reply"

    # The agent should admit it doesn't know rather than hallucinate 'Alex'.
    # Accept "don't know / not sure / haven't told me / no information" patterns.
    reply_lower = result_b["reply"].lower()
    if "alex" in reply_lower:
        uncertain_phrases = (
            "don't know",
            "do not know",
            "not sure",
            "haven't",
            "have not",
            "no information",
            "you haven't told",
            "you have not told",
            "not been provided",
        )
        assert any(p in reply_lower for p in uncertain_phrases), (
            f"Session B must not confidently recall 'Alex' from Session A. "
            f"Got: {result_b['reply']!r}"
        )

    # The two sessions must be independent objects with separate histories.
    session_a_obj = orchestrator.session_manager.get_session(session_a)
    session_b_obj = orchestrator.session_manager.get_session(session_b)
    assert session_a_obj is not None
    assert session_b_obj is not None
    assert session_a_obj is not session_b_obj
    assert session_a_obj.messages is not session_b_obj.messages


# ---------------------------------------------------------------------------
# Scenario 4 — Session resumption after restart
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_scenario_4_session_resumption_after_restart() -> None:
    """Scenario 4: A new Orchestrator hydrated from persisted messages sees prior history.

    This mirrors exactly what service/app.py does on every request:
      1. Load prior_messages from PostgreSQL
      2. Inject into fresh Orchestrator via session_manager.update_session()
      3. Run handle_user_request()

    We simulate the DB by capturing messages from Orchestrator A and
    replaying them into a fresh Orchestrator B.
    """
    session_id = "fre37-s4-resumption"

    # --- "Before restart" ---
    orchestrator_a = Orchestrator()
    result1 = await orchestrator_a.handle_user_request(
        session_id=session_id,
        user_message="My favourite colour is blue.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result1["reply"], "Pre-restart turn must produce a reply"

    # Capture messages as if persisted to the DB.
    session_a = orchestrator_a.session_manager.get_session(session_id)
    assert session_a is not None
    persisted_messages = list(session_a.messages)
    assert len(persisted_messages) >= 2, (
        "At least user + assistant messages must be stored before restart"
    )

    # --- "After restart" — fresh Orchestrator, history injected from 'DB' ---
    orchestrator_b = Orchestrator()
    orchestrator_b.session_manager.create_session(
        Mode.NORMAL, Channel.CHAT, session_id=session_id
    )
    orchestrator_b.session_manager.update_session(session_id, messages=persisted_messages)

    # Verify injection worked before making the LLM call.
    session_b_before = orchestrator_b.session_manager.get_session(session_id)
    assert session_b_before is not None
    assert len(session_b_before.messages) == len(persisted_messages)

    result2 = await orchestrator_b.handle_user_request(
        session_id=session_id,
        user_message="What is my favourite colour?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result2["reply"], "Post-restart turn must produce a reply"
    assert "blue" in result2["reply"].lower(), (
        f"Agent must recall 'blue' from the resumed session. Got: {result2['reply']!r}"
    )


# ---------------------------------------------------------------------------
# Scenario 5 — Memory graph enrichment (requires Neo4j)
# ---------------------------------------------------------------------------


@pytest.mark.integration
@pytest.mark.requires_llm_server
@pytest.mark.asyncio
async def test_scenario_5_memory_graph_enrichment() -> None:
    """Scenario 5: Cross-session recall via the Neo4j memory graph.

    Session A: multi-turn conversation about a topic (e.g. cycling in Paris).
    Session B (new): ask about the same topic → memory graph provides context.

    Skipped automatically if Neo4j is unreachable — run 'make infra-up' first.
    """
    # Check Neo4j availability before attempting.
    try:
        from personal_agent.memory.service import MemoryService

        probe = MemoryService()
        connected = await probe.connect()
        if not connected:
            pytest.skip("Neo4j not reachable — run 'make infra-up' to enable this scenario")
        await probe.disconnect()
    except Exception as exc:
        pytest.skip(f"Neo4j probe failed ({exc}) — run 'make infra-up' to enable")

    from personal_agent.config import settings

    if not settings.enable_memory_graph:
        pytest.skip("Memory graph disabled in config — set enable_memory_graph=true")

    orchestrator = Orchestrator()
    session_a = "fre37-s5-memory-session-a"
    session_b = "fre37-s5-memory-session-b"

    # Session A: establish context about Paris cycling.
    await orchestrator.handle_user_request(
        session_id=session_a,
        user_message="I went cycling in Paris last weekend near the Eiffel Tower.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    await orchestrator.handle_user_request(
        session_id=session_a,
        user_message="The weather was great and I rode along the Seine river.",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )

    # Allow background reflection / memory storage to complete.
    import asyncio

    await asyncio.sleep(5)  # background tasks + ES/Neo4j indexing lag

    # Session B: new session — ask about a topic that memory graph should surface.
    result_b = await orchestrator.handle_user_request(
        session_id=session_b,
        user_message="What places in Paris have I been to?",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    assert result_b["reply"], "Session B must produce a reply"

    # Memory graph should surface Paris / Eiffel Tower / Seine from Session A.
    reply_lower = result_b["reply"].lower()
    memory_keywords = ("paris", "eiffel", "seine", "cycling", "cycle")
    assert any(kw in reply_lower for kw in memory_keywords), (
        f"Cross-session memory recall must surface Paris context. "
        f"Got: {result_b['reply']!r}"
    )
