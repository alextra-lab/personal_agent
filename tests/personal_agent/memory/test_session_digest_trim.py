"""Trim-not-discard for an over-long digest (FRE-993, ADR-0124 Amendment C2).

Amendment C2 makes the rendered ceiling *"a rejection threshold of last resort, not
the sizing mechanism"*: a digest between the target and the ceiling is delivered, not
discarded. This module pins the mechanism that makes that true — dropping items until
the rendering fits, rather than throwing an already-parsed, already-validated digest
away and paying for a second generation that lands at the same length.

Two properties carry the design and are each asserted directly:

**Drop order is ours, not the model's.** The generator is asked for
most-consequential-first *within* a slot, but a digest whose surviving content depends
on the model having obeyed that would put drop order back into model judgment. The slot
order plus tail-first is the deterministic mechanism underneath it.

**No slot is annihilated while another still has slack.** A digest that keeps four
recoverable corrections while deleting every trace that work remains open is the
failure Amendment C's own instrument post-mortem warns about, and a naive
exhaust-each-slot-in-turn trim produces exactly it.
"""

# ruff: noqa: D103

from __future__ import annotations

from datetime import datetime, timezone

from personal_agent.memory.session_digest import (
    Correction,
    DigestItem,
    Locator,
    SessionDigest,
    UnresolvedItem,
    digest_token_count,
    parse_stored_digest,
    render_digest,
    trim_digest_to_budget,
)

_TS = datetime(2026, 7, 27, 10, 0, 0, tzinfo=timezone.utc)

#: Long enough that a handful of items clears any bound used below, so the tests
#: exercise trimming rather than the tokenizer's rounding.
_FILLER = "a decision that runs on at considerable length about the substrate " * 3


def _item(text: str) -> DigestItem:
    return DigestItem(text=text, basis="user_statement")


def _unresolved(text: str) -> UnresolvedItem:
    return UnresolvedItem(text=text, basis="assistant_reasoning", as_of=_TS)


def _correction(text: str) -> Correction:
    locator = Locator(capture_id="c1", field="assistant_text")
    return Correction(
        text=text,
        basis="assistant_reasoning",
        span="span",
        locator=locator,
        tier="self_correction",
        evidence_span="evidence",
        evidence_locator=locator,
    )


def _texts(digest: SessionDigest) -> dict[str, list[str]]:
    return {
        "established": [i.text for i in digest.established],
        "decisions": [i.text for i in digest.decisions],
        "unresolved": [i.text for i in digest.unresolved],
        "corrections": [i.text for i in digest.corrections],
    }


def _total_items(digest: SessionDigest) -> int:
    return sum(len(v) for v in _texts(digest).values())


# --------------------------------------------------------------------------
# It fits already — trimming is a no-op
# --------------------------------------------------------------------------


def test_a_digest_within_its_bound_is_returned_untouched() -> None:
    digest = SessionDigest(established=[_item("short")], decisions=[_item("also short")])

    trimmed, dropped = trim_digest_to_budget(digest, 400)

    assert dropped == 0
    assert trimmed == digest


def test_an_empty_digest_is_returned_untouched() -> None:
    trimmed, dropped = trim_digest_to_budget(SessionDigest(), 400)

    assert dropped == 0
    assert trimmed.is_empty()


# --------------------------------------------------------------------------
# AC-1 — it fits afterwards, and the cheapest slot goes first
# --------------------------------------------------------------------------


def test_an_over_long_digest_is_trimmed_to_fit() -> None:
    digest = SessionDigest(
        established=[_item(f"{i} {_FILLER}") for i in range(6)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(3)],
    )
    assert digest_token_count(digest) > 200

    trimmed, dropped = trim_digest_to_budget(digest, 200)

    assert digest_token_count(trimmed) <= 200
    assert dropped > 0
    assert _total_items(trimmed) == _total_items(digest) - dropped


def test_established_is_the_first_slot_drawn_from() -> None:
    """D3 names ``established`` the slot most at risk of re-deriving stored facts."""
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(4)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(4)],
    )

    # 8 items render at 312 tokens; 300 forces a single drop, so which slot it comes
    # from is the whole assertion.
    trimmed, dropped = trim_digest_to_budget(digest, 300)

    assert dropped == 1
    assert len(trimmed.established) == 3
    assert len(trimmed.decisions) == 4


def test_decisions_are_the_last_prose_slot_dropped() -> None:
    """D3's definition of a wrong digest is about omitted *conclusions*."""
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(3)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(3)],
        unresolved=[_unresolved(f"unresolved {i} {_FILLER}") for i in range(3)],
        corrections=[_correction(f"correction {i} {_FILLER}") for i in range(3)],
    )

    trimmed, _ = trim_digest_to_budget(digest, 120)

    assert len(trimmed.decisions) >= len(trimmed.established)
    assert len(trimmed.decisions) >= len(trimmed.corrections)


# --------------------------------------------------------------------------
# AC-2 — the order is ours, deterministically
# --------------------------------------------------------------------------


def test_items_are_dropped_from_the_tail_of_a_slot() -> None:
    """The model is asked for most-consequential-first; the tail is what goes."""
    digest = SessionDigest(
        established=[_item(f"keep {_FILLER}"), _item(f"drop {_FILLER}")],
    )

    trimmed, dropped = trim_digest_to_budget(digest, 60)

    assert dropped == 1
    assert [i.text for i in trimmed.established] == [f"keep {_FILLER}"]


def test_trimming_the_same_digest_twice_gives_the_same_result() -> None:
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(4)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(4)],
        unresolved=[_unresolved(f"unresolved {i} {_FILLER}") for i in range(2)],
    )

    first, first_dropped = trim_digest_to_budget(digest, 250)
    second, second_dropped = trim_digest_to_budget(digest, 250)

    assert first == second
    assert first_dropped == second_dropped


# --------------------------------------------------------------------------
# AC-3 — no slot is annihilated while another still has slack
# --------------------------------------------------------------------------


def test_a_lone_unresolved_item_survives_a_crowded_established_slot() -> None:
    """The failure this rule exists for: losing every open question to bulk elsewhere."""
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(8)],
        unresolved=[_unresolved(f"the one open question {_FILLER}")],
    )

    trimmed, _ = trim_digest_to_budget(digest, 200)

    assert len(trimmed.unresolved) == 1
    assert len(trimmed.established) < 8


def test_every_non_empty_slot_keeps_an_item_while_any_slot_has_slack() -> None:
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(5)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(5)],
        unresolved=[_unresolved(f"unresolved {i} {_FILLER}") for i in range(5)],
        corrections=[_correction(f"correction {i} {_FILLER}") for i in range(5)],
    )

    # A bound that forces heavy trimming but still admits four items.
    trimmed, _ = trim_digest_to_budget(digest, 260)

    assert trimmed.established and trimmed.decisions
    assert trimmed.unresolved and trimmed.corrections


def test_phase_two_drops_whole_slots_in_order_once_every_slot_is_singular() -> None:
    """Corrections → unresolved → decisions, once nothing has slack left."""
    digest = SessionDigest(
        established=[_item(f"established {_FILLER}")],
        decisions=[_item(f"decision {_FILLER}")],
        unresolved=[_unresolved(f"unresolved {_FILLER}")],
        corrections=[_correction(f"correction {_FILLER}")],
    )

    trimmed, dropped = trim_digest_to_budget(digest, 60)

    assert dropped == 3
    assert not trimmed.established
    assert not trimmed.corrections
    assert not trimmed.unresolved
    assert len(trimmed.decisions) == 1


# --------------------------------------------------------------------------
# Never to empty — Amendment C5 names the empty digest as the delivery failure
# --------------------------------------------------------------------------


def test_trimming_never_empties_a_digest_that_had_content() -> None:
    digest = SessionDigest(established=[_item(_FILLER * 10)])

    trimmed, dropped = trim_digest_to_budget(digest, 10)

    assert not trimmed.is_empty()
    assert dropped == 0


# --------------------------------------------------------------------------
# Declared, not silent (ADR-0125 D5)
# --------------------------------------------------------------------------


def test_a_trimmed_digest_records_and_renders_what_it_lost() -> None:
    """A reader must be able to tell a trimmed digest from a whole one.

    Telemetry cannot carry this: the log line does not survive into the stored
    artifact that the gateway view and any Phase-2 consumer read back.
    """
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(6)],
        decisions=[_item(f"decision {i} {_FILLER}") for i in range(2)],
    )

    trimmed, dropped = trim_digest_to_budget(digest, 200)

    assert dropped > 0
    assert trimmed.items_dropped == dropped
    assert "Trimmed to fit" in render_digest(trimmed)
    assert str(dropped) in render_digest(trimmed)


def test_an_untrimmed_digest_renders_no_marker() -> None:
    digest = SessionDigest(established=[_item("short")])

    trimmed, _ = trim_digest_to_budget(digest, 400)

    assert trimmed.items_dropped == 0
    assert "Trimmed to fit" not in render_digest(trimmed)


def test_the_marker_is_paid_for_out_of_the_budget() -> None:
    """The declaration must not push the rendering back over the ceiling it declares."""
    digest = SessionDigest(
        established=[_item(f"established {i} {_FILLER}") for i in range(9)],
    )

    trimmed, dropped = trim_digest_to_budget(digest, 200)

    assert dropped > 0
    assert "Trimmed to fit" in render_digest(trimmed)
    assert digest_token_count(trimmed) <= 200


def test_items_dropped_survives_a_storage_round_trip() -> None:
    """Stored, not merely computed — old digests without the field still parse."""
    digest = SessionDigest(established=[_item("kept")], items_dropped=3)

    round_tripped = parse_stored_digest(digest.model_dump(mode="json"))
    legacy = parse_stored_digest({"established": [{"text": "x", "basis": "mixed"}]})

    assert round_tripped.items_dropped == 3
    assert legacy.items_dropped == 0


def test_a_single_item_over_the_whole_ceiling_survives_and_stays_over() -> None:
    """The producer turns this into a loud failure; trimming does not hide it."""
    digest = SessionDigest(
        established=[_item(f"established {_FILLER}")],
        decisions=[_item(_FILLER * 20)],
    )

    trimmed, _ = trim_digest_to_budget(digest, 100)

    assert digest_token_count(trimmed) > 100
    assert _total_items(trimmed) == 1
