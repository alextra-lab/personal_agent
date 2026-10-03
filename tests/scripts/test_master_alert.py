# ruff: noqa: D103
"""Unit tests for the dispatch alert message to cc-master (FRE-1540)."""

from __future__ import annotations

from scripts.dispatch.master_alert import (
    ALERT_AFTER_S,
    ALERT_PREFIX,
    format_notify_entry,
    format_undelivered_trigger,
    is_due,
)
from scripts.dispatch.trigger_ledger import LedgerEntry


def _entry(**kw: object) -> LedgerEntry:
    base: dict[str, object] = {
        "event_id": "dispatch-notify:dispatch_seat_wedged:build2",
        "source": "dispatch_seat_wedged",
        "target_pane": "build2",
        "ticket": "FRE-1234",
        "command": "",
        "preconditions": {"reason": "idle-with-prompt", "question": "Proceed with the merge?"},
        "created_at": 0.0,
    }
    base.update(kw)
    return LedgerEntry(**base)  # type: ignore[arg-type]


def test_alert_is_due_at_fifteen_minutes_and_not_before() -> None:
    assert ALERT_AFTER_S == 900.0
    assert not is_due(_entry(), now=899.0)
    assert is_due(_entry(), now=900.0)


def test_alert_is_never_due_twice() -> None:
    assert not is_due(_entry(alerted_at=950.0), now=5000.0)


def test_trigger_alert_names_pr_seat_reason_first_attempt_and_count() -> None:
    text = format_undelivered_trigger(
        pr=1194,
        seat="cc-1build",
        reason="channel_delivery_failed+busy",
        first_attempt_at=1_790_000_000.0,
        attempts=16,
        now=1_790_000_960.0,
    )
    assert text.startswith(ALERT_PREFIX)
    for part in ("#1194", "cc-1build", "channel_delivery_failed+busy", "16", "2026-"):
        assert part in text, part
    assert "master skill" in text  # the message carries its own instruction


def test_notify_alert_names_source_stream_ticket_and_question() -> None:
    text = format_notify_entry(_entry(), now=960.0)
    assert text.startswith(ALERT_PREFIX)
    for part in ("dispatch_seat_wedged", "build2", "FRE-1234", "Proceed with the merge?"):
        assert part in text, part


def test_alert_text_is_one_line_and_capped() -> None:
    long_question = "line one\nline two\r\n" + "x" * 5000
    text = format_notify_entry(_entry(preconditions={"question": long_question}), now=960.0)
    assert "\n" not in text and "\r" not in text
    assert len(text) < 800


def test_alert_never_starts_with_a_slash_command() -> None:
    assert not ALERT_PREFIX.startswith("/")
