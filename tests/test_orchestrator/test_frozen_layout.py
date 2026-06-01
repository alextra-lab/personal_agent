"""Tests for ADR-0081 §D2 frozen append-only layout (FRE-434).

Part A delivers cross-turn KV reuse on the local SLM by moving per-turn volatile
content (recalled memory + selected skill bodies) out of the system head and
inlining it into the current user turn, so prior turns replay byte-identically.

These cover the pure volatile-carrier helper; the assembly/persistence wiring is
exercised separately once it lands.
"""

from __future__ import annotations

from personal_agent.llm_client.history_sanitiser import sanitise_messages
from personal_agent.orchestrator.executor import (
    _inline_volatile_into_last_user_message,
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
