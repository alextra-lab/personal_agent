"""AC-11 — located-span provenance validation (ADR-0124 D3, Amendment B).

The criterion: every ``corrections`` entry carries a span **and a locator**, and the
span occurs **at that location**. The locator grammar is conversation-only and,
after Amendment B, **assistant text only** — the assistant is the one doing the
self-correcting, and the survivor kind (``self_correction``) is grounded entirely in
the assistant's own corrective text, never the user's message and never a tool
result field.

It fails if any locator is absent or unresolvable, or the span is not found at the
cited location — **bare containment anywhere in the session does not pass**.

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
            assistant=(
                "The cluster is green and all shards are assigned. Correcting myself — I "
                're-read the output, which said "relation sessions already exists", so the '
                "migration did not apply."
            ),
            # Still captured/stored (Amendment A), just never read by the producer or
            # citable by the validator (Amendment B removed the tool locator targets).
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
# Locator resolution — conversation-only, assistant text only (Amendment B)
# --------------------------------------------------------------------------


def test_resolves_assistant_text() -> None:
    captures = _captures()
    assert "shards are assigned" in (
        resolve_locator(Locator(capture_id="cap-1", field="assistant_text"), captures) or ""
    )


def test_unresolvable_locators_return_none() -> None:
    """The grammar is assistant-text only.

    Unknown capture, ``user_text`` (dropped from the grammar), and a tool field
    (also dropped) all fail to resolve.
    """
    captures = _captures()
    assert resolve_locator(Locator(capture_id="nope", field="assistant_text"), captures) is None
    assert resolve_locator(Locator(capture_id="cap-1", field="user_text"), captures) is None
    assert (
        resolve_locator(Locator(capture_id="cap-1", field="tool_result[0].output"), captures)
        is None
    )
    assert resolve_locator(Locator(capture_id="cap-1", field="thinking"), captures) is None


# --------------------------------------------------------------------------
# Slots other than corrections never require a span (no basis obliges citation
# post-Amendment-B: tool_evidence, the only basis that ever did, is retired)
# --------------------------------------------------------------------------


def test_no_slot_besides_corrections_requires_a_span() -> None:
    captures = _captures()
    digest = SessionDigest(
        established=[DigestItem(text="Established without citation.", basis="mixed")],
        decisions=[DigestItem(text="Chose to defer the reindex.", basis="user_statement")],
        unresolved=[
            UnresolvedItem(text="Whether to shard by date.", basis="assistant_reasoning", as_of=_TS)
        ],
    )
    assert validate_digest_provenance(digest, captures) == []


# --------------------------------------------------------------------------
# Corrections — the only slot the span+locator obligation still applies to
# --------------------------------------------------------------------------


def _correction(**overrides: object) -> Correction:
    base: dict[str, object] = {
        "text": "The assistant corrected the migration outcome within the session.",
        "basis": "assistant_reasoning",
        "tier": "self_correction",
        "span": "the migration did not apply",
        "locator": Locator(capture_id="cap-1", field="assistant_text"),
        "evidence_span": "relation sessions already exists",
        "evidence_locator": Locator(capture_id="cap-1", field="assistant_text"),
    }
    base.update(overrides)
    return Correction(**base)  # type: ignore[arg-type]


def test_self_correction_with_resolving_spans_passes() -> None:
    captures = _captures()
    assert validate_digest_provenance(SessionDigest(corrections=[_correction()]), captures) == []


def test_whitespace_differences_do_not_defeat_a_real_citation() -> None:
    captures = _captures()
    correction = _correction(
        evidence_span='"relation   sessions\n  already exists"'.strip('"'),
    )
    assert validate_digest_provenance(SessionDigest(corrections=[correction]), captures) == []


def test_fails_when_locator_is_absent() -> None:
    captures = _captures()
    correction = _correction(locator=None)
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "requires both a span and a locator" in violations[0]


def test_fails_when_locator_does_not_resolve() -> None:
    captures = _captures()
    correction = _correction(locator=Locator(capture_id="cap-404", field="assistant_text"))
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "does not resolve" in violations[0]


def test_fails_when_span_sits_in_a_different_capture() -> None:
    """The span is real and in the session — but not where the item says it is."""
    captures = _captures()
    correction = _correction(
        span="Four shards are unassigned.",  # really lives in cap-2
        locator=Locator(capture_id="cap-1", field="assistant_text"),
    )
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "span not found at" in violations[0]


def test_fails_when_span_is_elsewhere_in_the_session() -> None:
    """Bare containment is not sufficient — this is the criterion's stated evasion."""
    captures = _captures()
    correction = _correction(
        text="Shards were unassigned.",
        span="Four shards are unassigned.",
        # Verbatim from cap-2, cited against cap-1's assistant text.
        locator=Locator(capture_id="cap-1", field="assistant_text"),
    )
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "span not found at" in violations[0]


def test_evidence_fails_when_it_does_not_resolve() -> None:
    captures = _captures()
    correction = _correction(evidence_span="EACCES: permission denied")  # never appears
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "evidence" in violations[0]


def test_correction_evidence_from_user_text_is_rejected() -> None:
    """The direct regression test for Amendment B's narrowed grammar.

    Citing the user's own message as evidence — legal under Amendment A — must now
    fail, since ``user_text`` is no longer a valid locator target for a
    correction's evidence.
    """
    captures = _captures()
    correction = _correction(
        evidence_span="and the shards?",
        evidence_locator=Locator(capture_id="cap-2", field="user_text"),
    )
    violations = validate_digest_provenance(SessionDigest(corrections=[correction]), captures)
    assert len(violations) == 1
    assert "does not resolve" in violations[0]


def test_correction_is_checked_regardless_of_basis_tag() -> None:
    """Every corrections entry carries spans — the obligation is not basis-gated.

    A model that tags a correction `user_statement` must not thereby escape the
    citation requirement.
    """
    captures = _captures()
    correction = _correction(
        text="Unsupported claim of an error.",
        basis="user_statement",
        span="I was wrong about the shard count",  # nowhere in the session
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
# AC-12 fixture pre-validation (Amendment B)
# --------------------------------------------------------------------------


def test_ac12_positive_fixtures_have_a_resolving_reference_citation() -> None:
    """Every AC-12 positive must carry a citable, resolvable evidence span.

    That span must be in the assistant's own text, before the paid arm runs.

    Codex plan-review, FRE-956: Amendment B restricts a self-correction's evidence to
    the assistant's own corrective text (never the user's message, never a tool
    result). If a positive's supporting evidence lived anywhere else the producer
    could not cite it — and a provenance failure rejects the whole digest, so the
    case would read as ``errored`` rather than a true positive and AC-12 would fail
    on a fixture flaw, not a producer one. This asserts a valid citation exists — a
    necessary condition for the arm.
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
    assert len(positives) >= 8, "AC-12 requires at least 8 self-correction positives"

    for case in positives:
        assert case["tier"] == "self_correction"
        ref = case["reference_correction"]
        assert ref["locator"]["field"] == "assistant_text"
        assert ref["evidence_locator"]["field"] == "assistant_text"
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
