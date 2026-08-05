"""FRE-1150 — the authenticated operator identity must outrank recalled identity claims.

The incident these tests pin: the owner said "Good evening" and was greeted as "Susan", a
different registered user. The operator stanza was *present* in the cached system head
saying "You are assisting Alex" — proven from the turn's own telemetry — but a recalled
entity named Susan, described as "The user's stated name in the conversation", sat inside
the current user message (the volatile block is prepended there) and won.

So these tests assert two things the incident disproves:

1. The stanza claims *authority*, not merely fact — it states that recalled claims do not
   override it. A bare "You are assisting Alex" is what lost.
2. The competing claim still reaches the model. The fix must not work by deleting the
   entity; that is FRE-674's territory and this ticket fails if it is closed here.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from personal_agent.orchestrator.prompts import OperatorIdentity, get_owner_identity

# The incident's exact competing claim, verbatim from the graph.
SUSAN_ENTITY: dict[str, Any] = {
    "name": "Susan",
    "entity_type": "Person",
    "description": "The user's stated name in the conversation.",
}


def _make_memory_service(facts: dict[str, Any] | None = None) -> MagicMock:
    """Return a mock MemoryService whose get_or_provision_user_person returns facts."""
    svc = MagicMock()
    svc.get_or_provision_user_person = AsyncMock(return_value=facts or {})
    svc.connected = True
    return svc


def _make_ctx(*, user_id: Any = None, user_email: str | None = None) -> Any:
    from personal_agent.governance.models import Mode
    from personal_agent.orchestrator.channels import Channel
    from personal_agent.orchestrator.types import ExecutionContext

    return ExecutionContext(
        session_id="test-session",
        trace_id="test-trace",
        user_message="Good evening",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
        messages=[{"role": "user", "content": "Good evening"}],
        user_id=user_id,
        user_email=user_email,
    )


class TestStanzaAssertsAuthority:
    """The head states authority over recall, not just the fact of identity."""

    @pytest.mark.asyncio
    async def test_stanza_asserts_authority_over_recalled_claims(self) -> None:
        svc = _make_memory_service({"name": "Alex"})
        identity = await get_owner_identity(svc, uuid4(), "a@b.com", None)

        assert "You are assisting Alex" in identity.stanza
        # The authority claim itself — what the incident's stanza lacked.
        assert "authentication" in identity.stanza.lower()
        assert "override" in identity.stanza.lower()
        # And it must name the user in the conflict rule, so "someone other than X"
        # is decidable by the model rather than left abstract.
        assert identity.stanza.count("Alex") >= 2

    @pytest.mark.asyncio
    async def test_identity_name_matches_stanza_text(self) -> None:
        """One source for the asserted name — the capture records `name`, the model
        reads `stanza`; they must never be able to disagree.
        """
        svc = _make_memory_service({"name": "Alex"})
        identity = await get_owner_identity(svc, uuid4(), "a@b.com", None)

        assert identity.name == "Alex"
        assert f"You are assisting {identity.name}." in identity.stanza

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("service", "user_id", "email", "facts"),
        [
            ("none", True, "a@b.com", None),
            ("mock", False, "a@b.com", {"name": "Alex"}),
            ("mock", True, None, {"name": "Alex"}),
            ("mock", True, "a@b.com", {}),
            ("mock", True, "a@b.com", {"location": "Paris"}),
        ],
    )
    async def test_stanza_empty_when_identity_unavailable(
        self,
        service: str,
        user_id: bool,
        email: str | None,
        facts: dict[str, Any] | None,
    ) -> None:
        svc = None if service == "none" else _make_memory_service(facts)
        result = await get_owner_identity(svc, uuid4() if user_id else None, email, None)

        assert result == OperatorIdentity()
        assert result.name == ""
        assert result.stanza == ""


class TestOperatorIdentityPopulation:
    """Fold-in A — the guard's false branches no longer skip silently.

    A silent skip is what made the original misdiagnosis survivable: nothing in the
    logs distinguished 'never ran' from 'ran and produced nothing'.
    """

    @pytest.mark.asyncio
    async def test_populates_both_fields(self) -> None:
        from personal_agent.orchestrator.executor import _populate_operator_identity

        ctx = _make_ctx(user_id=uuid4(), user_email="a@b.com")
        await _populate_operator_identity(ctx, _make_memory_service({"name": "Alex"}))

        assert ctx.operator_name == "Alex"
        assert "You are assisting Alex" in ctx.operator_stanza
        # The recorded assertion carries the mechanism but not the profile detail block.
        assert "You are assisting Alex" in ctx.operator_assertion
        assert "none of them override this line" in ctx.operator_assertion

    @pytest.mark.asyncio
    async def test_logs_unidentified_request(self, caplog: pytest.LogCaptureFixture) -> None:
        from personal_agent.orchestrator.executor import _populate_operator_identity

        caplog.set_level("INFO", logger="personal_agent.orchestrator.executor")
        ctx = _make_ctx(user_id=None, user_email=None)
        await _populate_operator_identity(ctx, _make_memory_service({"name": "Alex"}))

        assert "operator_stanza_skipped" in caplog.text
        assert "unidentified_request" in caplog.text
        assert ctx.operator_stanza == ""

    @pytest.mark.asyncio
    async def test_logs_memory_service_unavailable(self, caplog: pytest.LogCaptureFixture) -> None:
        from personal_agent.orchestrator.executor import _populate_operator_identity

        caplog.set_level("WARNING", logger="personal_agent.orchestrator.executor")
        ctx = _make_ctx(user_id=uuid4(), user_email="a@b.com")
        await _populate_operator_identity(ctx, None)

        assert "operator_stanza_skipped" in caplog.text
        assert "memory_service_unavailable" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_identity_unresolved(self, caplog: pytest.LogCaptureFixture) -> None:
        """The case that had no branch at all: the call succeeds and yields nothing."""
        from personal_agent.orchestrator.executor import _populate_operator_identity

        caplog.set_level("WARNING", logger="personal_agent.orchestrator.executor")
        ctx = _make_ctx(user_id=uuid4(), user_email="a@b.com")
        await _populate_operator_identity(ctx, _make_memory_service({}))

        assert "operator_stanza_skipped" in caplog.text
        assert "identity_unresolved" in caplog.text

    @pytest.mark.asyncio
    async def test_logs_failure_without_raising(self, caplog: pytest.LogCaptureFixture) -> None:
        from personal_agent.orchestrator.executor import _populate_operator_identity

        caplog.set_level("WARNING", logger="personal_agent.orchestrator.executor")
        svc = MagicMock()
        svc.connected = True
        svc.get_or_provision_user_person = AsyncMock(side_effect=RuntimeError("neo4j down"))
        ctx = _make_ctx(user_id=uuid4(), user_email="a@b.com")

        await _populate_operator_identity(ctx, svc)

        assert "operator_stanza_failed" in caplog.text
        assert ctx.operator_stanza == ""


class TestCompetingClaimSurvives:
    """The fix must not work by removing the entity — that is FRE-674's job.

    ``It fails if`` on the ticket: "the fix is demonstrated only by removing the offending
    entity rather than by the identity claim losing to the authenticated one while still
    present."
    """

    def test_competing_claim_still_rendered(self) -> None:
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids

        section, ids = _render_memory_section_with_ids([SUSAN_ENTITY])

        assert "Susan" in section
        assert "The user's stated name in the conversation." in section
        assert "Susan" in ids

    def test_entity_section_does_not_license_answering_about_the_user(self) -> None:
        """Step 8 — the sentence that promoted a third party's name into an answer
        about the connected user.
        """
        from personal_agent.orchestrator.executor import _render_memory_section_with_ids

        section, _ = _render_memory_section_with_ids([SUSAN_ENTITY])

        assert "directly answer questions about what the user" not in section
        assert "not who you are speaking with" in section
        # The legitimate half of the old instruction survives.
        assert "Do NOT say you have no memory." in section

    def test_competing_claim_reaches_the_wire_alongside_the_stanza(self) -> None:
        """End-to-end on the artifact the model receives: the volatile block is
        prepended into the current user message, so the competing claim is adjacent to
        the query — that is the geometry, and it is unchanged by this fix.
        """
        from personal_agent.captains_log.turn_evidence import InlineOutcome
        from personal_agent.orchestrator.executor import (
            _inline_volatile_with_outcome,
            _render_memory_section_with_ids,
        )

        section, _ = _render_memory_section_with_ids([SUSAN_ENTITY])
        messages = [{"role": "user", "content": "Good evening"}]
        out, outcome = _inline_volatile_with_outcome(messages, section)

        assert outcome is InlineOutcome.INLINED
        wire = out[-1]["content"]
        assert "Susan" in wire
        assert wire.index("Susan") < wire.index("Good evening")
