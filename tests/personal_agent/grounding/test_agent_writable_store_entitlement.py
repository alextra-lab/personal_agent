"""FRE-1303 AC-3: a typed retrieval reading back an agent-writable store is not EXTERNAL.

FRE-1302 shipped the premise that ``search_memory`` was the one ``TYPED_RETRIEVAL_TOOLS`` member
needing more than a blanket ``EXTERNAL``, "every other member keeping the blanket EXTERNAL a
typed, model-independent retrieval earns by default". That premise is false for every tool here:
each reads back a store one of the agent's own write tools fills
(``notes_write``, ``artifact_write``/``artifact_draft``, ``create_linear_issue``,
``create_linear_project``, and — for ``expand_tool_result`` — *any* earlier tool call, including
the ``bash``/``run_python`` output ``ARBITRARY_CODE_TOOLS`` already excludes).

Typed parameters make a call independent of the model's *arguments*; they say nothing about who
authored what the store hands back. This file pins the second axis.
"""

from __future__ import annotations

import json

import pytest

from personal_agent.grounding.citations import parse_citations
from personal_agent.grounding.source_registry import (
    AGENT_WRITABLE_STORE_TOOLS,
    DOCUMENTATION_TOOLS,
    TYPED_RETRIEVAL_TOOLS,
    Entitlement,
    SourceRegistry,
)
from personal_agent.grounding.spans import NonExemptReason, Span, SpanExtraction, SpanLabel
from personal_agent.grounding.verification import CheckOutcome, verify_turn

TURN = "trace-fre1303-stores"

LAUNDERED = "Ortiz bonito costs 12 euros"
"""A sentence the model authored, parked in a store, and read back as if it were evidence.

Names an entity and states a figure so D3(c) containment resolves directly and D3(d) never
engages — the outcome under test is the entitlement gate, not the entailment judge.
"""


def _register(tool_name: str, content: str) -> tuple[Entitlement, CheckOutcome]:
    """Run one tool result through the real registry and the real verifier.

    Arguments are numeric so ``_strip_argument_echo`` has no eligible value and the registered
    content is the payload byte for byte.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name=tool_name, arguments={"limit": 5}, content=content
    )
    assert registration.source is not None, f"{tool_name} registered no source"
    source = registration.source

    output = f"{LAUNDERED} [{source.identifier}]."
    start = output.index(LAUNDERED)
    extraction = SpanExtraction(
        output=output,
        spans=(
            Span(
                start=start,
                end=start + len(LAUNDERED),
                text=LAUNDERED,
                label=SpanLabel.CLAIM_NON_EXEMPT,
                reason=NonExemptReason.CLASSIFIED,
            ),
        ),
    )
    verification = verify_turn(extraction, parse_citations(output), registry)
    return source.entitlement, verification.spans[0].outcome


# One seeded negative per member, each shaped like the store it reads back.
_SEEDED_NEGATIVES: dict[str, str] = {
    # notes_write parks the agent's own markdown; notes_search hands it straight back.
    "notes_search": json.dumps({"notes": [{"note_id": "n1", "content": LAUNDERED}]}),
    # Verbatim R2 replay of an earlier tool result — including a bash result the policy
    # table already refuses to register. The exclusion must survive the extra hop.
    "expand_tool_result": json.dumps({"key": "tool-results/abc", "content": LAUNDERED}),
    # artifact_write / artifact_draft are the only writers of this store.
    "artifact_read": json.dumps({"artifact_id": "a1", "title": "Prices", "content": LAUNDERED}),
    "artifact_list": json.dumps({"artifacts": [{"artifact_id": "a1", "title": LAUNDERED}]}),
    # create_linear_issue sets `title` verbatim from a model-authored argument, and
    # find_linear_issues reads it back with no author field to split on.
    "find_linear_issues": json.dumps({"issues": [{"identifier": "FRE-1", "title": LAUNDERED}]}),
    "list_linear_projects": json.dumps({"projects": [{"id": "p1", "description": LAUNDERED}]}),
}

_LINEAR_MCP_READS: tuple[str, ...] = (
    "mcp_get_attachment",
    "mcp_get_document",
    "mcp_get_issue",
    "mcp_get_issue_status",
    "mcp_get_milestone",
    "mcp_get_project",
    "mcp_get_team",
    "mcp_get_user",
    "mcp_list_comments",
    "mcp_list_cycles",
    "mcp_list_documents",
    "mcp_list_issue_labels",
    "mcp_list_issue_statuses",
    "mcp_list_issues",
    "mcp_list_milestones",
    "mcp_list_project_labels",
    "mcp_list_projects",
    "mcp_list_teams",
    "mcp_list_users",
)
"""The MCP half of the Linear surface — same store, same agent writer, same verdict."""

for _name in _LINEAR_MCP_READS:
    _SEEDED_NEGATIVES[_name] = json.dumps({"nodes": [{"title": LAUNDERED}]})


@pytest.mark.parametrize("tool_name", sorted(_SEEDED_NEGATIVES))
def test_agent_writable_store_read_is_refused(tool_name: str) -> None:
    """Each member returns the agent's own sentence and must not be citable for it."""
    entitlement, outcome = _register(tool_name, _SEEDED_NEGATIVES[tool_name])

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_every_agent_writable_member_carries_a_seeded_negative() -> None:
    """A member added to the set later fails here until it has its own proof.

    AC-3's requirement is a verdict per member; this is the mechanical half of it, so the
    set and the evidence cannot drift apart silently.
    """
    assert set(_SEEDED_NEGATIVES) == set(AGENT_WRITABLE_STORE_TOOLS)


def test_agent_writable_members_stay_admissible_retrievals() -> None:
    """These tools remain admissible retrievals — the fix is their entitlement, not their
    admissibility. Dropping them from the policy table instead would register no source at
    all, and D4's terminal no-source statement could no longer name what was read.

    ``mcp_get_document`` is the reason this checks the union rather than
    ``TYPED_RETRIEVAL_TOOLS`` alone: it is a Linear workspace read that sits in
    ``DOCUMENTATION_TOOLS``, and asserting the narrower subset would have made adding it fail
    a test for the wrong reason — or, worse, discouraged adding it at all.
    """
    assert AGENT_WRITABLE_STORE_TOOLS <= (TYPED_RETRIEVAL_TOOLS | DOCUMENTATION_TOOLS)


def test_the_documentation_tool_that_reads_the_linear_workspace_is_denied() -> None:
    """The gap the first pass of this audit missed, pinned by name.

    ``mcp_get_document`` never appeared in ``TYPED_RETRIEVAL_TOOLS``, so an audit that walked
    that set alone could not see it — while the entitlement dispatch, which is independent of
    ``SourceKind``, sent it to the ``EXTERNAL`` default just the same. Its sibling
    ``mcp_list_documents`` denied correctly, which is what made the asymmetry visible.
    """
    assert "mcp_get_document" in DOCUMENTATION_TOOLS
    assert "mcp_get_document" not in TYPED_RETRIEVAL_TOOLS

    entitlement, outcome = _register(
        "mcp_get_document", json.dumps({"id": "doc-1", "content": LAUNDERED})
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_linear_product_documentation_search_keeps_external() -> None:
    """The control that stops the fix over-reaching into ``DOCUMENTATION_TOOLS``.

    ``mcp_search_documentation`` searches Linear's public product docs, not the user's
    workspace, so nothing the agent writes can come back through it.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="mcp_search_documentation", arguments={"limit": 5}, content=LAUNDERED
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.EXTERNAL


@pytest.mark.parametrize("tool_name", ["web_search", "fetch_url", "read", "read_skill"])
def test_externally_authored_retrievals_keep_external(tool_name: str) -> None:
    """The negative control. A blanket denial would pass every test above and gut D2 item 2,
    so the members with no agent-authorship channel must still earn ``EXTERNAL``.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name=tool_name, arguments={"limit": 5}, content=LAUNDERED
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.EXTERNAL


# ── get_location — content-aware, because one of its two resolvers is genuinely external ──


def _location_output(source: str, city: str) -> str:
    """The shape ``_executor_output`` builds from ``asdict(LocationResolution)``."""
    return json.dumps(
        {
            "resolved": True,
            "location": {
                "city": city,
                "country": "FR",
                "latitude": 48.85,
                "longitude": 2.35,
                "timezone": "Europe/Paris",
                "source": source,
                "precise": False,
            },
            "latency_ms": 3.2,
        }
    )


def test_location_resolved_from_model_written_session_notes_is_refused() -> None:
    """D2's ``printf 'Paris'`` shape wearing a typed parameter.

    ``session_notes`` is free text the *model* writes; ``ExplicitLocationProvider`` extracts a
    city from it and hands it back as ``location.city``. ``_strip_argument_echo`` cannot see
    this — it compares whole top-level values, and the returned city is a substring of the
    argument nested one level down — so the registered content really does carry the model's
    own word back at ``EXTERNAL`` unless this rule fires.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="get_location",
        arguments={"session_notes": "I'm writing from Paris this week, by the way."},
        content=_location_output("explicit", "Paris"),
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED


def test_device_provided_location_keeps_external() -> None:
    """The positive control that stops this becoming a flat denial: the stored client-provided
    device location owes nothing to the model's arguments.
    """
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="get_location",
        arguments={"session_notes": None},
        content=_location_output("client", "Lyon"),
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.EXTERNAL


def test_unreadable_location_shape_denies() -> None:
    """Tool content is attacker-influenced, so an unreadable shape must not readmit itself."""
    registry = SourceRegistry(turn_id=TURN)
    registration = registry.register_tool_result(
        tool_name="get_location",
        arguments={"session_notes": None},
        content=json.dumps({"resolved": False, "reason": "consent_not_given"}),
    )

    assert registration.source is not None
    assert registration.source.entitlement is Entitlement.AGENT_DERIVED


# ── The accepted regressions, pinned so they are decisions rather than discoveries ────


def test_user_uploaded_artifact_is_denied_and_this_is_the_accepted_cost() -> None:
    """``artifact_read`` serves ``type IN ('artifact', 'upload')`` and uploads carry
    ``created_by = 'user'`` — but ``created_by`` is in neither its ``SELECT`` nor its output,
    so nothing in the result distinguishes an owner's upload from generated HTML.

    Most-restrictive therefore denies both. That is the same tradeoff ``_entitlement_of``
    already documents for a Claim lacking ``asserted_by``: an owner-stated fact that is merely
    *unlabelled* loses its citation, because the alternative is the system certifying its own
    errors. The remedy is to thread ``created_by`` into the tool's output (ticketed), not to
    readmit the agent-authored half meanwhile.
    """
    entitlement, outcome = _register(
        "artifact_read",
        json.dumps(
            {
                "artifact_id": "a1",
                "title": "prices.md",
                "content_type": "text/markdown",
                "content": LAUNDERED,
            }
        ),
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED


def test_expanded_external_result_loses_citability_and_this_is_deliberate() -> None:
    """``expand_tool_result`` replays a digested ``web_search`` result as readily as a digested
    ``bash`` one — the digest pipeline handles ``tool_name == "bash"`` explicitly — and the
    replay carries no originating-tool provenance.

    So the denial costs a genuinely external result its citation. It is still the right
    direction: the alternative readmits ``ARBITRARY_CODE_TOOLS``' own excluded stdout through a
    typed tool one hop later. The originating ``web_search`` call registered its own source in
    the turn that made it, which is where that grounding belongs.
    """
    entitlement, outcome = _register(
        "expand_tool_result",
        json.dumps({"key": "tool-results/websearch-1", "content": LAUNDERED}),
    )

    assert entitlement is Entitlement.AGENT_DERIVED
    assert outcome is CheckOutcome.SOURCE_NOT_ENTITLED
