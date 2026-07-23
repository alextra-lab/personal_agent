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

import json
import pathlib
from datetime import datetime, timezone
from uuid import uuid4

from personal_agent.captains_log.capture import TaskCapture
from personal_agent.memory.session_digest import (
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
# Corrections — a claim span + an evidence span, both located (ADR-0124 D3,
# Amendment A: status_contradiction cites tool status/error; self_correction cites
# supporting evidence in the conversation).
# --------------------------------------------------------------------------


def test_status_contradiction_requires_both_spans_to_resolve() -> None:
    captures = _captures()
    correction = Correction(
        text="Narration said the read succeeded; the tool errored.",
        basis="tool_evidence",
        tier="status_contradiction",
        # The contradicted claim, in the assistant text...
        span="The cluster is green",
        locator=Locator(capture_id="cap-1", field="assistant_text"),
        # ...and the denying tool error.
        evidence_span="ENOENT: /etc/missing.conf",
        evidence_locator=Locator(capture_id="cap-1", field="tool_result[1].error"),
    )
    assert validate_digest_provenance(SessionDigest(corrections=[correction]), captures) == []


def test_status_contradiction_fails_when_the_evidence_span_does_not_resolve() -> None:
    captures = _captures()
    correction = Correction(
        text="Narration said success; the tool error says otherwise.",
        basis="tool_evidence",
        tier="status_contradiction",
        span="The cluster is green",
        locator=Locator(capture_id="cap-1", field="assistant_text"),
        evidence_span="EACCES: permission denied",  # never appears
        evidence_locator=Locator(capture_id="cap-1", field="tool_result[1].error"),
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
        tier="self_correction",
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


# --------------------------------------------------------------------------
# AC-12 fixture pre-validation (FRE-953 / Amendment A)
# --------------------------------------------------------------------------


def test_ac12_positive_fixtures_have_a_resolving_reference_citation() -> None:
    """Every AC-12 self-correction positive must carry a citable, resolvable evidence
    span in a field the producer can SEE (a tool error or the conversation), before the
    paid arm runs.

    Codex plan-review, FRE-953: if a positive's supporting evidence lived only in a tool
    payload (which Amendment A no longer feeds), the producer could not cite it — and a
    provenance failure rejects the whole digest, so the case would read as ``errored``
    rather than a true positive and AC-12 would fail on a fixture flaw, not a producer
    one. This asserts a valid citation exists — a necessary condition for the arm.
    """
    fixture = json.loads(
        (
            pathlib.Path(__file__).parents[3]
            / "tests"
            / "fixtures"
            / "session_digest"
            / "ac12_corrections.json"
        ).read_text(encoding="utf-8")
    )
    positives = [c for c in fixture["cases"] if c["expected"] == "correction"]
    assert len(positives) >= 8, "AC-12 (amended) requires at least 8 self-correction positives"

    for case in positives:
        ref = case["reference_correction"]
        captures = [TaskCapture(**c) for c in case["captures"]]
        correction = Correction(
            text="reference citation",
            basis="assistant_reasoning",
            tier=case["tier"],
            span=ref["span"],
            locator=Locator(**ref["locator"]),
            evidence_span=ref["evidence_span"],
            evidence_locator=Locator(**ref["evidence_locator"]),
        )
        violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
        assert violations == [], (
            f"{case['case_id']}: reference citation does not resolve: {violations}"
        )
