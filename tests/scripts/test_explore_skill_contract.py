# ruff: noqa: D103
"""Contract guards for the `/explore` working skill (FRE-1196, ADR-0135 D3–D6).

The skill document's *wording* is the deliverable: ADR-0135's seam ticket
(FRE-1195) later runs a discrimination test on this exact text — apply the
admissibility rule verbatim to a historical negative finding and it must come
out inadmissible; apply the same text with arm 1 **textually deleted** and it
must come out admissible.  That test is only runnable if the deletion is a
defined operation and the remainder is still a rule.

These guards therefore pin structure, not prose:

  - the three arms sit in separately-delimited, correctly-nested blocks;
  - deleting arm 1 leaves arms 2 and 3 intact and the operative sentence
    standing — asserted by *performing* the deletion, not by inspection;
  - no arm references another by number, so removing one strands nothing;
  - the operative sentence carries no hardcoded arm count, which is what would
    make the arm-1-deleted variant self-contradictory rather than merely
    shorter.

Follows `test_dispatch_skill_contracts.py`: content guards on stable markers,
scoped to the section under test so a stray match elsewhere cannot hide a
regression at the actual call site.
"""

from __future__ import annotations

from pathlib import Path

_SKILL = Path(".claude/skills/explore/SKILL.md")

_RULE_START = "<!-- RULE:START -->"
_RULE_END = "<!-- RULE:END -->"
_ARM_ANCHORS = [(f"<!-- ARM-{n}:START -->", f"<!-- ARM-{n}:END -->") for n in (1, 2, 3)]


def _read() -> str:
    return _SKILL.read_text()


def _norm(text: str) -> str:
    return " ".join(text.lower().split())


def _section(text: str, start_marker: str, end_marker: str) -> str:
    """Slice `text` between `start_marker` and the next `end_marker` after it."""
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    return text[start:end]


def _arm_block(text: str, n: int) -> str:
    """Return arm `n`'s block including its anchors."""
    start_anchor, end_anchor = _ARM_ANCHORS[n - 1]
    start = text.index(start_anchor)
    end = text.index(end_anchor, start) + len(end_anchor)
    return text[start:end]


def _delete_arm_one(text: str) -> str:
    """Excise arm 1's block — the exact operation seam AC-2 performs."""
    start_anchor, end_anchor = _ARM_ANCHORS[0]
    start = text.index(start_anchor)
    end = text.index(end_anchor, start) + len(end_anchor)
    return text[:start] + text[end:]


# --- the skill exists and declares itself ----------------------------------


def test_skill_file_exists_with_frontmatter() -> None:
    assert _SKILL.exists(), "the /explore working skill has no file"
    text = _read()
    assert text.startswith("---\n"), "skill must open with a frontmatter block"
    frontmatter = _section(text, "---\n", "\n---")
    assert "name: explore" in frontmatter
    assert "description:" in frontmatter


# --- all six contract areas the ticket enumerates --------------------------
# One marker per area.  An absent area is the ticket's stated failure mode.


def test_covers_all_six_contract_areas() -> None:
    text = _norm(_read())
    # 1 — read-only scope on everything operational
    assert "read-only on everything operational" in text
    # 2 — measure live, never reason from code
    assert "never reason from code" in text
    # 3 — the admissibility rule for negative findings
    assert "admissibility rule" in text and "inadmissible" in text
    # 4 — the fixed deliverable shape
    assert "deliverable shape" in text
    # 5 — the durable substrate map
    assert "substrate map" in text
    # 6 — branch and path write scope
    assert "docs/research/" in text and "explore-fre-" in text


# --- the arms are separable by textual deletion (ticket AC-2) --------------


def test_arms_are_separately_delimited_and_nested_in_the_rule() -> None:
    text = _read()
    for start_anchor, end_anchor in _ARM_ANCHORS:
        assert start_anchor in text, f"missing {start_anchor}"
        assert end_anchor in text, f"missing {end_anchor}"

    positions = [
        text.index(_RULE_START),
        *[i for pair in _ARM_ANCHORS for i in (text.index(pair[0]), text.index(pair[1]))],
        text.index(_RULE_END),
    ]
    # Strictly increasing ⟹ arms are ordered, non-overlapping, and both the
    # first and last sit inside the rule block.
    assert positions == sorted(positions), "arm blocks overlap or escape the rule block"
    assert len(set(positions)) == len(positions)


def test_deleting_arm_one_leaves_a_complete_rule() -> None:
    variant = _delete_arm_one(_read())

    # Arm 1 is gone...
    assert "<!-- ARM-1:START -->" not in variant
    assert "<!-- ARM-1:END -->" not in variant
    # ...and the rule that remains is still a rule: operative sentence intact,
    # arms 2 and 3 intact, block still closed.
    assert "every arm stated below" in _norm(variant)
    for start_anchor, end_anchor in _ARM_ANCHORS[1:]:
        assert start_anchor in variant and end_anchor in variant
    assert _RULE_START in variant and _RULE_END in variant

    # The remaining arms are still applicable on their own terms — each still
    # states its own test rather than pointing at the deleted one.
    remaining = _norm(_section(variant, _RULE_START, _RULE_END))
    assert "arm 1" not in remaining, "the arm-1-deleted variant still refers to arm 1"


def test_operative_sentence_carries_no_hardcoded_arm_count() -> None:
    # A count inside the rule block ("all three of") makes the arm-1-deleted
    # variant self-contradictory instead of merely shorter, which is what would
    # break the seam's discrimination test.
    rule = _norm(_section(_read(), _RULE_START, _RULE_END))
    for forbidden in ("all three", "all 3", "three arms", "each of the three"):
        assert forbidden not in rule, f"rule block hardcodes an arm count: {forbidden!r}"


def test_arms_do_not_cross_reference() -> None:
    text = _read()
    for n in (1, 2, 3):
        block = _norm(_arm_block(text, n))
        for other in {1, 2, 3} - {n}:
            assert f"arm {other}" not in block, (
                f"arm {n} references arm {other} — deleting one would strand the other"
            )


# --- the worked example (ticket AC-3) --------------------------------------


def test_worked_example_rejects_a_liveness_only_negative() -> None:
    example = _norm(_section(_read(), "## Worked example", "## The deliverable shape"))
    # The historical finding, and the same-store liveness control that is the
    # only evidence it carried.
    assert "within_session_compressed" in example
    assert "cache_reset_decision" in example
    # The verdict the rule as written must return, and the arm that produced it.
    assert "inadmissible" in example
    assert "for want of arm 1" in example


def test_worked_example_names_the_real_producers_it_was_read_against() -> None:
    # The example is only instructive if it shows how arm 1 is actually
    # satisfied — the identifiers that do exist, in the store that has them.
    example = _norm(_section(_read(), "## Worked example", "## The deliverable shape"))
    assert "within_session_compression_hard_trigger" in example
    assert "within_session_compression_recorded" in example
    assert "stream:context.within_session_compressed" in example


# --- UNVERIFIABLE is separated from a negative by a stated test (AC-4) -----


def test_unverifiable_is_separated_from_negative_by_a_stated_test() -> None:
    section = _norm(_section(_read(), "### UNVERIFIABLE", "## Worked example"))
    assert "unverifiable" in section
    # The separating test is anchored on arm 1, and distinguishes "cannot be
    # produced" from "has not been attempted" — otherwise an unfinished
    # measurement silently becomes a first-class verdict.
    assert "arm 1" in section
    assert "cannot be produced" in section
    assert "not attempted" in section or "have not attempted" in section


def test_a_positive_verdict_needs_no_arms() -> None:
    # ADR-0135 D3: a wrong identifier cannot produce a non-zero, so a positive
    # is self-validating.  Without this the rule reads as a tax on every finding.
    text = _norm(_read())
    assert "positive" in text and "self-validating" in text


# --- the fixed deliverable shape (ticket AC-5) -----------------------------


def test_deliverable_shape_names_proposals_cap_and_filed_tickets() -> None:
    shape = _norm(_section(_read(), "## The deliverable shape", "## The durable substrate map"))
    # Per-finding record.
    assert "verdict" in shape and "query" in shape and "actual output" in shape
    # Proposals: the single place recommendations appear, capped at ten.
    assert "proposals" in shape
    assert "single place" in shape
    assert "at most ten" in shape
    assert "overflow is inadmissible" in shape
    # The filed-tickets list, and the method appendix.  Asserted on the
    # obligation, not on one spelling of the section's name: the list must be
    # required, and its completeness must be the thing that is required.
    assert "filed tickets" in shape
    assert "every ticket this study filed" in shape
    assert "is itself a violation" in shape
    assert "method appendix" in shape


def test_deliverable_shape_is_stated_as_requirement_not_suggestion() -> None:
    shape = _norm(_section(_read(), "## The deliverable shape", "## The durable substrate map"))
    # Requirement voice, not "consider" / "you may".
    assert "must" in shape
    assert "consider adding" not in shape
    assert "optional" not in shape


# --- filing authority: Backlog only (D5) -----------------------------------


def test_filing_is_backlog_only_and_never_self_promoted() -> None:
    section = _norm(_section(_read(), "## Filing", "## Branch"))
    assert "backlog" in section
    assert "needs approval" in section
    assert "never" in section
    assert "promote" in section


# --- write scope: one file, one branch, never merge (D6) -------------------


def test_write_scope_is_one_research_document_and_never_merges() -> None:
    # End marker must be the *next* heading: "## " would match the start
    # marker itself and slice an empty section, which passes nothing.
    section = _norm(_section(_read(), "## Branch", "## Running a study"))
    assert "docs/research/<date>-fre-xxxx-<slug>.md" in section
    assert "explore-fre-xxxx-<slug>" in section
    assert "never merge" in section


# --- the substrate map's load-bearing facts (D4) ---------------------------


def test_substrate_map_carries_its_five_facts() -> None:
    section = _norm(_section(_read(), "## The durable substrate map", "## Filing"))
    assert "_count" in section and "_cat" in section
    assert "fre-1051" in section  # ES counts provisional
    assert "api_costs" in section  # per-call authority is Postgres, not ES
    assert "running process" in section  # config, not the repo
    assert "file hash" in section  # deployed code, not board state


# --- the maintenance note that keeps the arms deletable --------------------


def test_maintenance_note_pins_the_separability_constraints() -> None:
    # Without this an editor "tidies" the anchors away and the seam's
    # discrimination test silently stops being runnable.
    note = _norm(_section(_read(), "### Maintenance note", "### UNVERIFIABLE"))
    assert "fre-1195" in note or "seam" in note
    assert "delet" in note  # deletion is the operation being protected
    assert "count" in note  # no hardcoded count
    assert "refer" in note  # no cross-arm references
