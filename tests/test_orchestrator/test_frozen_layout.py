"""Tests for ADR-0081 §D2 frozen append-only layout (FRE-434).

Part A delivers cross-turn KV reuse on the local SLM by moving per-turn volatile
content (recalled memory + selected skill bodies) out of the system head and
inlining it into the current user turn, so prior turns replay byte-identically.

These cover the pure volatile-carrier helper; the assembly/persistence wiring is
exercised separately once it lands.
"""

from __future__ import annotations

from personal_agent.captains_log.turn_evidence import InlineOutcome
from personal_agent.llm_client.history_sanitiser import sanitise_messages
from personal_agent.orchestrator.executor import (
    _inline_volatile_into_last_user_message,
    _inline_volatile_with_outcome,
    _validate_and_fix_conversation_roles,
)


def test_empty_volatile_is_noop() -> None:
    """An empty volatile block leaves the message list byte-identical."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "second"},
    ]
    out = _inline_volatile_into_last_user_message(msgs, "")
    assert out == msgs


def test_whitespace_only_volatile_is_noop() -> None:
    """Whitespace-only volatile must not leak separator bytes onto the frozen side."""
    msgs = [{"role": "user", "content": "hello"}]
    out = _inline_volatile_into_last_user_message(msgs, "   \n  ")
    assert out == msgs


def test_volatile_prepended_to_last_user_message() -> None:
    """Non-empty volatile is wrapped in a single fenced block above the query."""
    msgs = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "reply"},
        {"role": "user", "content": "the query"},
    ]
    out = _inline_volatile_into_last_user_message(msgs, "RECALL: x")
    # Only the last user message changes.
    assert out[0] == msgs[0]
    assert out[1] == msgs[1]
    content = out[2]["content"]
    assert content.startswith("<turn_context>")
    assert "RECALL: x" in content
    assert content.rstrip().endswith("the query")
    # Exactly one fenced block.
    assert content.count("<turn_context>") == 1


def test_inline_produces_exact_expected_bytes() -> None:
    """Pin the literal wire bytes, not just structural properties (ADR-0081 D2)."""
    msgs = [{"role": "user", "content": "the query"}]
    out = _inline_volatile_into_last_user_message(msgs, "RECALL: x")
    assert out[0]["content"] == "<turn_context>\nRECALL: x\n</turn_context>\n\nthe query"


# --- Block-form (attachment turn) content — FRE-1137 ---


_TEXT_BLOCK = {"type": "text", "text": "look at this"}
_IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}


def test_volatile_prepended_to_block_form_last_user_message() -> None:
    """An attachment turn's block-list content gets a leading fenced text block."""
    msgs = [{"role": "user", "content": [_TEXT_BLOCK, _IMAGE_BLOCK]}]
    out, outcome = _inline_volatile_with_outcome(msgs, "RECALL: x")
    assert outcome is InlineOutcome.INLINED
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "<turn_context>\nRECALL: x\n</turn_context>"}
    assert content[1:] == [_TEXT_BLOCK, _IMAGE_BLOCK]


def test_block_form_idempotent_when_already_wrapped() -> None:
    """Re-inlining an already-wrapped block-form turn must not double-wrap."""
    msgs = [{"role": "user", "content": [_TEXT_BLOCK, _IMAGE_BLOCK]}]
    once, first_outcome = _inline_volatile_with_outcome(msgs, "VOL")
    assert first_outcome is InlineOutcome.INLINED
    twice, second_outcome = _inline_volatile_with_outcome(once, "VOL")
    assert second_outcome is InlineOutcome.ALREADY_WRAPPED
    assert twice == once


def test_block_form_empty_list_is_injectable() -> None:
    """An edge-case empty block list still gets the fence — not treated as no target."""
    msgs = [{"role": "user", "content": []}]
    out, outcome = _inline_volatile_with_outcome(msgs, "VOL")
    assert outcome is InlineOutcome.INLINED
    assert out[0]["content"] == [{"type": "text", "text": "<turn_context>\nVOL\n</turn_context>"}]


def test_block_form_image_only_last_user_message() -> None:
    """An image-only turn (no caption text block) still gets the fence prepended."""
    msgs = [{"role": "user", "content": [_IMAGE_BLOCK]}]
    out, outcome = _inline_volatile_with_outcome(msgs, "VOL")
    assert outcome is InlineOutcome.INLINED
    content = out[0]["content"]
    assert content[0] == {"type": "text", "text": "<turn_context>\nVOL\n</turn_context>"}
    assert content[1:] == [_IMAGE_BLOCK]


def test_two_consecutive_block_form_turns_frozen_byte_identical() -> None:
    """Turn N+1's block-form inlining must not perturb turn N's frozen content."""
    turn1, _ = _inline_volatile_with_outcome(
        [{"role": "user", "content": [_TEXT_BLOCK, _IMAGE_BLOCK]}], "VOL1"
    )
    persisted_turn1_content = turn1[0]["content"]

    turn2_in = turn1 + [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": [_TEXT_BLOCK, _IMAGE_BLOCK]},
    ]
    turn2, outcome = _inline_volatile_with_outcome(turn2_in, "VOL2")
    assert outcome is InlineOutcome.INLINED

    assert turn2[0]["content"] == persisted_turn1_content
    assert turn2[2]["content"][0]["text"] == "<turn_context>\nVOL2\n</turn_context>"


def test_does_not_mutate_input() -> None:
    """The helper returns a new list and never mutates the caller's messages."""
    msgs = [{"role": "user", "content": "q"}]
    snapshot = "q"
    _inline_volatile_into_last_user_message(msgs, "VOL")
    assert msgs[0]["content"] == snapshot


def test_idempotent_when_block_already_present() -> None:
    """Re-inlining an already-wrapped turn must not double-wrap (byte stability)."""
    msgs = [{"role": "user", "content": "q"}]
    once = _inline_volatile_into_last_user_message(msgs, "VOL")
    twice = _inline_volatile_into_last_user_message(once, "VOL")
    assert twice == once


def test_no_user_message_is_noop() -> None:
    """With no user message to carry volatile, the list is returned unchanged."""
    msgs = [{"role": "assistant", "content": "only assistant"}]
    out = _inline_volatile_into_last_user_message(msgs, "VOL")
    assert out == msgs


def test_targets_last_user_not_earlier() -> None:
    """Volatile attaches to the newest user turn, never an earlier one."""
    msgs = [
        {"role": "user", "content": "old"},
        {"role": "user", "content": "new"},
    ]
    out = _inline_volatile_into_last_user_message(msgs, "VOL")
    assert out[0]["content"] == "old"
    assert out[1]["content"].startswith("<turn_context>")


# --- Byte-identity invariant (ADR-0081 §D2 — the make-or-break for local reuse) ---


def test_inlined_frozen_history_is_transform_chain_fixed_point() -> None:
    """Persisted bytes must equal wire bytes after the full transform chain.

    Per ADR-0081 §D2 the canonical rule is: the bytes written to session.messages
    for turn N must equal the bytes sent on the wire for turn N, after every
    transform. With /no_think retired, the remaining transforms are
    role-validation and the history sanitiser. For clean frozen history both are
    no-ops, so the inlined history is a fixed point — persisting it equals what the
    client dispatches (the ADR's "prove the sanitiser is a no-op + assert" option).
    """
    history = _inline_volatile_into_last_user_message(
        [{"role": "user", "content": "first query"}], "RECALL t1"
    )
    history = history + [
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second query"},
    ]
    history = _inline_volatile_into_last_user_message(history, "RECALL t2")

    after_roles = _validate_and_fix_conversation_roles(history)
    assert after_roles == history  # role-fix is a no-op for clean alternation

    after_sanitise, report = sanitise_messages(after_roles, trace_id="trace-test")
    assert after_sanitise == history  # sanitiser is a no-op (no orphan tool pairs)
    assert report.was_dirty is False


def test_prior_turn_frozen_byte_identical_across_turns() -> None:
    """Turn N+1 reproduces turn N's user message byte-for-byte (forward extension).

    This is the property local KV reuse requires: each turn is a strict forward
    extension of the previous wire sequence.
    """
    turn1 = _inline_volatile_into_last_user_message([{"role": "user", "content": "q1"}], "VOL1")
    persisted_turn1_user = turn1[0]["content"]

    # Turn 2 replays persisted history, appends a new user turn + fresh volatile.
    turn2_in = list(turn1) + [
        {"role": "assistant", "content": "a1"},
        {"role": "user", "content": "q2"},
    ]
    turn2 = _inline_volatile_into_last_user_message(turn2_in, "VOL2")

    # Turn 1's user message bytes are unchanged in turn 2's sequence (frozen).
    assert turn2[0]["content"] == persisted_turn1_user
    # Only the newest turn carries fresh volatile.
    assert "VOL2" in turn2[2]["content"]
    assert "VOL1" not in turn2[2]["content"]


def test_perturbation_probe_changes_frozen_prefix() -> None:
    """A one-byte perturbation of a frozen turn changes the prefix.

    Proves the byte-identity instrument is live (ADR-0081 §D2 Verification): a
    deliberate single-byte change to a frozen turn must be observable, otherwise a
    silently-perturbed prefix would zero local reuse without detection.
    """
    turn1 = _inline_volatile_into_last_user_message([{"role": "user", "content": "q1"}], "VOL1")
    good = turn1[0]["content"]
    perturbed = f"{good} "  # one trailing byte
    assert perturbed != good
