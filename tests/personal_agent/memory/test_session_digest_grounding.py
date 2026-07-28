"""AC-11 — located-span provenance, now held *by construction* (ADR-0124 Amendment E).

The criterion did not change; the mechanism did. A ``corrections`` entry still carries a
span **and a locator**, and the span still occurs **at that location** — but the model no
longer transcribes the span. It supplies the locator alone and :func:`ground_correction`
quotes the text there, so "the span occurs at its locator" is an identity rather than a
comparison. The failure class the old validator managed — a paraphrased transcription
discarding a whole digest — stops existing (FRE-1024).

What can still go wrong is narrower and is handled without losing the artefact: a locator
that does not resolve (unknown capture, a field outside the assistant-text grammar, or a
turn with no assistant response) grounds nothing, so **that item is dropped and the digest
is kept**, with the drop recorded on the record and declared in the rendering.

The locator grammar is conversation-only and, after Amendment B, **assistant text only** —
the assistant is the one doing the self-correcting, and the survivor kind
(``self_correction``) is grounded entirely in the assistant's own corrective text, never
the user's message and never a tool result field.
"""

# ruff: noqa: D103

from __future__ import annotations

import inspect
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
    ground_correction,
    render_digest,
    resolve_locator,
)

_USER_ID = uuid4()
_TS = datetime(2026, 7, 23, 10, 0, 0, tzinfo=timezone.utc)

_CAP1_TEXT = (
    "The cluster is green and all shards are assigned. Correcting myself — I re-read the "
    'output, which said "relation sessions already exists", so the migration did not apply.'
)
_CAP2_TEXT = "Four shards are unassigned."


def _capture(
    trace_id: str,
    *,
    user: str = "check the cluster",
    assistant: str | None = "The cluster is green and all shards are assigned.",
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
            assistant=_CAP1_TEXT,
            # Still captured/stored (Amendment A), just never read by the producer or
            # citable by a locator (Amendment B removed the tool locator targets).
            tool_results=[
                {
                    "tool_name": "query_elasticsearch",
                    "success": True,
                    "output": '{"status": "red", "unassigned_shards": 4}',
                    "error": None,
                    "latency_ms": 12.0,
                },
            ],
        ),
        _capture("cap-2", user="and the shards?", assistant=_CAP2_TEXT),
    ]


def _ground(**overrides: object) -> Correction | None:
    """Ground one correction, defaulting to a citation that resolves."""
    base: dict[str, object] = {
        "text": "The assistant corrected the migration outcome within the session.",
        "basis": "assistant_reasoning",
        "tier": "self_correction",
        "locator": Locator(capture_id="cap-1", field="assistant_text"),
        "evidence_locator": Locator(capture_id="cap-1", field="assistant_text"),
        "captures": _captures(),
    }
    base.update(overrides)
    return ground_correction(**base)  # type: ignore[arg-type]


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
# Grounding — the code quotes what the model pointed at (FRE-1024)
# --------------------------------------------------------------------------


def test_span_is_the_text_at_its_locator() -> None:
    """The AC's identity: the persisted span *is* the resolved text, not a copy of it."""
    correction = _ground()

    assert correction is not None
    assert correction.span == _CAP1_TEXT
    assert correction.evidence_span == _CAP1_TEXT
    assert correction.span == resolve_locator(correction.locator, _captures())


def test_claim_and_evidence_are_quoted_from_their_own_locators() -> None:
    """Two locators, two independent reads — neither borrows the other's text."""
    correction = _ground(evidence_locator=Locator(capture_id="cap-2", field="assistant_text"))

    assert correction is not None
    assert correction.span == _CAP1_TEXT
    assert correction.evidence_span == _CAP2_TEXT


def test_grounding_cannot_be_handed_a_span() -> None:
    """``ground_correction`` has no span parameter at all.

    The strongest form of the AC — a span cannot be passed in even by mistake, so no
    caller can reintroduce model-supplied text on this path.
    """
    params = inspect.signature(ground_correction).parameters

    assert "span" not in params
    assert "evidence_span" not in params


def test_unresolvable_claim_locator_grounds_nothing() -> None:
    assert _ground(locator=Locator(capture_id="cap-404", field="assistant_text")) is None


def test_unresolvable_evidence_locator_grounds_nothing() -> None:
    assert _ground(evidence_locator=Locator(capture_id="cap-404", field="assistant_text")) is None


def test_evidence_cited_from_user_text_grounds_nothing() -> None:
    """The direct regression for Amendment B's narrowed grammar.

    Citing the user's own message as evidence — legal under Amendment A — is outside the
    locator grammar, so it resolves to nothing and the item is dropped rather than
    silently grounded against the wrong field.
    """
    assert _ground(evidence_locator=Locator(capture_id="cap-2", field="user_text")) is None


def test_absent_locator_grounds_nothing() -> None:
    assert _ground(locator=None) is None
    assert _ground(evidence_locator=None) is None


def test_turn_with_no_assistant_response_grounds_nothing() -> None:
    """An empty quote is not evidence.

    ``resolve_locator`` returns ``""`` rather than ``None`` for a turn whose assistant
    response was never recorded, so a containment check would have passed vacuously. The
    grounding rule rejects blank text explicitly.
    """
    captures = [_capture("cap-1", assistant=None), _capture("cap-2", assistant=_CAP2_TEXT)]

    assert _ground(captures=captures) is None


def test_grounding_is_not_basis_gated() -> None:
    """A model that mislabels a correction's basis does not thereby escape grounding."""
    unresolvable = Locator(capture_id="cap-404", field="assistant_text")

    assert _ground(basis="user_statement", locator=unresolvable) is None


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


def test_dropped_corrections_are_declared_in_the_rendering() -> None:
    """ADR-0125 D5 — content cut on an evidence path is marked, never silently cut."""
    digest = SessionDigest(
        decisions=[DigestItem(text="Deferred the reindex.", basis="user_statement")],
        corrections_dropped=1,
    )
    rendered = render_digest(digest)

    assert "1 correction" in rendered
    assert "citation did not resolve" in rendered


def test_the_drop_declaration_is_distinct_from_the_trim_declaration() -> None:
    """Two causes, two statements — a reader must not read one as the other."""
    digest = SessionDigest(
        decisions=[DigestItem(text="Deferred the reindex.", basis="user_statement")],
        items_dropped=2,
        corrections_dropped=1,
    )
    rendered = render_digest(digest)

    assert "Trimmed to fit the digest budget: 2" in rendered
    assert "1 correction" in rendered


def test_a_record_that_lost_everything_still_declares_it() -> None:
    """The disclosure is not gated on surviving sections.

    The trim note's ``and sections`` guard is a no-op for trimming, which only fires on a
    content-bearing digest. Copying it here would silence the one record whose reader most
    needs telling: one that kept nothing.
    """
    rendered = render_digest(SessionDigest(corrections_dropped=1))

    assert "1 correction" in rendered


# --------------------------------------------------------------------------
# AC-12 fixture pre-validation (Amendment B, mechanism per Amendment E)
# --------------------------------------------------------------------------


def test_ac12_positive_fixtures_have_a_resolving_reference_citation() -> None:
    """Every AC-12 positive must carry citable, resolvable locators.

    Both must name the assistant's own text, before the paid arm runs.

    Codex plan-review, FRE-956: Amendment B restricts a self-correction's evidence to
    the assistant's own corrective text (never the user's message, never a tool
    result). If a positive's supporting evidence lived anywhere else the producer could
    not cite it, and the case would read as a dropped correction rather than a true
    positive — failing AC-12 on a fixture flaw, not a producer one.

    Asserted as *grounding* rather than span containment (FRE-1024): the reference span
    recorded in the fixture is no longer what the producer emits, so requiring it to be
    found there would test a retired mechanism.
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
        grounded = ground_correction(
            text="reference citation",
            basis="assistant_reasoning",
            tier=case["tier"],
            locator=Locator(**ref["locator"]),
            evidence_locator=Locator(**ref["evidence_locator"]),
            captures=captures,
        )
        assert grounded is not None, f"{case['case_id']}: reference locators do not ground"
