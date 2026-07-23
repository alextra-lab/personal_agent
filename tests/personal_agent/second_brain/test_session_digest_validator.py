"""AC-11 — located-span provenance validation (ADR-0124 D3, FRE-947).

The criterion: every ``tool_evidence`` item and every ``corrections`` entry carries
a span **and a locator**, and the span occurs **at that location**. It fails if any
locator is absent or unresolvable, or the span is not found at the cited location —
**bare containment anywhere in the session does not pass**.

That last clause is the whole point, and it is what the negative cases below
exercise: a common word appears everywhere and would sail through a
does-it-appear-somewhere check while supporting nothing.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.second_brain.session_digest import (
    Correction,
    DigestItem,
    Locator,
    SessionDigest,
    UnresolvedItem,
    digest_token_count,
    render_digest,
    resolve_locator,
    validate_digest_provenance,
)

_USER_ID = uuid4()
_TS = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)


def _capture(
    trace_id: str,
    *,
    user: str = "check the cluster",
    assistant: str = "The cluster is green and all shards are assigned.",
    tool_results: list[dict] | None = None,
) -> TaskCapture:
    return TaskCapture(
        trace_id=trace_id,
        session_id="sess-1",
        timestamp=_TS,
        user_message=user,
        assistant_response=assistant,
        outcome="completed",
        user_id=_USER_ID,
        tool_results=tool_results if tool_results is not None else [],
    )


def _captures() -> list[TaskCapture]:
    return [
        _capture(
            "cap-1",
            tool_results=[
                {
                    "tool_name": "query_elasticsearch",
                    "success": True,
                    "output": '{"status": "red", "unassigned_shards": 4}',
                    "error": None,
                    "latency_ms": 12.0,
                },
                {
                    "tool_name": "read_file",
                    "success": False,
                    "output": None,
                    "error": "ENOENT: /etc/missing.conf",
                    "latency_ms": 3.0,
                },
            ],
        ),
        _capture("cap-2", user="and the shards?", assistant="Four shards are unassigned."),
    ]


# --------------------------------------------------------------------------
# Locator resolution
# --------------------------------------------------------------------------


def test_resolves_each_field_in_the_grammar() -> None:
    captures = _captures()
    assert resolve_locator(Locator(capture_id="cap-1", field="user_text"), captures) == (
        "check the cluster"
    )
    assert "shards are assigned" in (
        resolve_locator(Locator(capture_id="cap-1", field="assistant_text"), captures) or ""
    )
    assert '"status": "red"' in (
        resolve_locator(Locator(capture_id="cap-1", field="tool_result[0].output"), captures) or ""
    )
    assert (
        resolve_locator(Locator(capture_id="cap-1", field="tool_result[1].error"), captures)
        == "ENOENT: /etc/missing.conf"
    )


def test_unresolvable_locators_return_none() -> None:
    captures = _captures()
    # Unknown capture, out-of-range tool index, and a field outside the grammar.
    assert resolve_locator(Locator(capture_id="nope", field="user_text"), captures) is None
    assert (
        resolve_locator(Locator(capture_id="cap-1", field="tool_result[9].output"), captures)
        is None
    )
    assert resolve_locator(Locator(capture_id="cap-1", field="thinking"), captures) is None


# --------------------------------------------------------------------------
# AC-11 positive
# --------------------------------------------------------------------------


def test_tool_evidence_item_with_a_resolving_span_passes() -> None:
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(
                text="The cluster was red with four unassigned shards.",
                basis="tool_evidence",
                span='"status": "red"',
                locator=Locator(capture_id="cap-1", field="tool_result[0].output"),
            )
        ]
    )
    assert validate_digest_provenance(digest, captures) == []


def test_non_tool_evidence_items_need_no_span() -> None:
    """Only `tool_evidence` items are obliged to cite. A user statement is not evidence."""
    captures = _captures()
    digest = SessionDigest(
        decisions=[DigestItem(text="Chose to defer the reindex.", basis="user_statement")]
    )
    assert validate_digest_provenance(digest, captures) == []


def test_whitespace_differences_do_not_defeat_a_real_citation() -> None:
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(
                text="Four shards unassigned.",
                basis="tool_evidence",
                span='"status":   "red",\n   "unassigned_shards": 4',
                locator=Locator(capture_id="cap-1", field="tool_result[0].output"),
            )
        ]
    )
    assert validate_digest_provenance(digest, captures) == []


# --------------------------------------------------------------------------
# AC-11 negative — the four failure modes the criterion names
# --------------------------------------------------------------------------


def test_fails_when_locator_is_absent() -> None:
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(text="The cluster was red.", basis="tool_evidence", span='"status": "red"')
        ]
    )
    violations = validate_digest_provenance(digest, captures)
    assert len(violations) == 1
    assert "requires both a span and a locator" in violations[0]


def test_fails_when_locator_does_not_resolve() -> None:
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(
                text="The cluster was red.",
                basis="tool_evidence",
                span='"status": "red"',
                locator=Locator(capture_id="cap-404", field="tool_result[0].output"),
            )
        ]
    )
    violations = validate_digest_provenance(digest, captures)
    assert len(violations) == 1
    assert "does not resolve" in violations[0]


def test_fails_when_span_sits_in_a_different_field_of_the_same_capture() -> None:
    """The span is real and in the session — but not where the item says it is."""
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(
                text="The cluster was red.",
                basis="tool_evidence",
                span='"status": "red"',
                # Really lives in tool_result[0].output, cited against the user text.
                locator=Locator(capture_id="cap-1", field="user_text"),
            )
        ]
    )
    violations = validate_digest_provenance(digest, captures)
    assert len(violations) == 1
    assert "span not found at" in violations[0]


def test_fails_when_span_is_elsewhere_in_the_session() -> None:
    """Bare containment is not sufficient — this is the criterion's stated evasion."""
    captures = _captures()
    digest = SessionDigest(
        established=[
            DigestItem(
                text="Shards were unassigned.",
                basis="tool_evidence",
                # Verbatim from cap-2's assistant text, cited against cap-1's tool result.
                span="Four shards are unassigned.",
                locator=Locator(capture_id="cap-1", field="tool_result[0].output"),
            )
        ]
    )
    violations = validate_digest_provenance(digest, captures)
    assert len(violations) == 1
    assert "bare containment" in violations[0]


# --------------------------------------------------------------------------
# Corrections — two located spans (Tier A) / evidence span (Tier B)
# --------------------------------------------------------------------------


def test_tier_a_correction_requires_both_spans_to_resolve() -> None:
    captures = _captures()
    correction = Correction(
        text="Narration said the cluster was green; the query returned red.",
        basis="tool_evidence",
        tier="A",
        # The contradicted claim, in the assistant text...
        span="The cluster is green",
        locator=Locator(capture_id="cap-1", field="assistant_text"),
        # ...and the contradicting evidence.
        evidence_span='"status": "red"',
        evidence_locator=Locator(capture_id="cap-1", field="tool_result[0].output"),
    )
    assert validate_digest_provenance(SessionDigest(corrections=[correction]), captures) == []


def test_tier_a_correction_fails_when_the_evidence_span_does_not_resolve() -> None:
    captures = _captures()
    correction = Correction(
        text="Narration said green; evidence said red.",
        basis="tool_evidence",
        tier="A",
        span="The cluster is green",
        locator=Locator(capture_id="cap-1", field="assistant_text"),
        evidence_span='"status": "yellow"',  # never appears
        evidence_locator=Locator(capture_id="cap-1", field="tool_result[0].output"),
    )
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "evidence" in violations[0]


def test_correction_is_checked_regardless_of_basis_tag() -> None:
    """Every corrections entry carries spans — the obligation is not basis-gated.

    A model that tags a correction `assistant_reasoning` must not thereby escape the
    citation requirement.
    """
    captures = _captures()
    correction = Correction(
        text="Unsupported claim of an error.",
        basis="assistant_reasoning",
        tier="B",
        span="I was wrong about the shard count",  # nowhere in the session
        locator=Locator(capture_id="cap-2", field="assistant_text"),
        evidence_span="Four shards are unassigned.",
        evidence_locator=Locator(capture_id="cap-2", field="assistant_text"),
    )
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "span not found at" in violations[0]


# --------------------------------------------------------------------------
# Rendering / measurement
# --------------------------------------------------------------------------


def test_render_omits_empty_slots() -> None:
    digest = SessionDigest(
        decisions=[DigestItem(text="Deferred the reindex.", basis="user_statement")]
    )
    rendered = render_digest(digest)
    assert "Decisions" in rendered
    for absent in ("Established", "Unresolved", "Corrections"):
        assert absent not in rendered


def test_unresolved_items_render_as_of_their_session() -> None:
    """A consumer must not read a stale open thread as present-tense."""
    digest = SessionDigest(
        unresolved=[
            UnresolvedItem(text="Whether to shard by date.", basis="mixed", as_of=_TS),
        ]
    )
    assert "(as of 2026-07-23)" in render_digest(digest)


def test_empty_digest_renders_empty_and_costs_nothing() -> None:
    digest = SessionDigest()
    assert digest.is_empty()
    assert render_digest(digest) == ""
    assert digest_token_count(digest) == 0
