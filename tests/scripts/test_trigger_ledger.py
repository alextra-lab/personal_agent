# ruff: noqa: D103
"""Unit tests for the durable trigger ledger (FRE-829, ADR-0113 §2).

Covers the ticket's acceptance-criteria slice:
  AC-4 duplicate event -> one actuation; malformed/idle event -> zero; two
       crash-injection runs (ledger-write<->send, send<->mark-consumed) ->
       exactly one net actuation, never a blind replay
  AC-1 (half) an unconsumed trigger survives a simulated context clear
       (fresh load from disk), sourced from the ledger not conversation

Also covers the FRE-832 CLI read surface `prime-master` shells out to
(`main`) -- the durable-read mechanism a `/clear`-safe rebuild depends on.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Literal

import pytest
from scripts.dispatch.trigger_ledger import (
    LedgerEntry,
    load_ledger,
    main,
    mark_consumed,
    mark_send_started,
    mark_sent,
    mark_surfaced,
    mark_transport,
    prune_ledger,
    reconcile,
    record_pending,
    save_ledger,
    snapshot_unconsumed,
)


class _NullLogger:
    def info(self, *args: object, **kwargs: object) -> None: ...
    def warning(self, *args: object, **kwargs: object) -> None: ...


class _RecordingLogger:
    def __init__(self) -> None:
        self.warnings: list[str] = []
        self.infos: list[str] = []

    def info(self, event: str, **kwargs: object) -> None:
        self.infos.append(event)

    def warning(self, event: str, **kwargs: object) -> None:
        self.warnings.append(event)


class _Spy:
    """A callable that records calls and returns a canned outcome (or raises)."""

    def __init__(
        self, outcome: Literal["sent", "busy", "absent"] | None = "sent", raises: bool = False
    ) -> None:
        self.calls: list[LedgerEntry] = []
        self._outcome = outcome
        self._raises = raises

    def __call__(self, entry: LedgerEntry) -> Literal["sent", "busy", "absent"]:
        self.calls.append(entry)
        if self._raises:
            raise RuntimeError("simulated tmux failure")
        assert self._outcome is not None
        return self._outcome


def _record(ledger: dict[str, LedgerEntry], *, now: float = 100.0, ttl_s: float = 600.0):
    return record_pending(
        ledger,
        event_id="master:412:abc123",
        source="master-ready",
        target_pane="cc-master",
        ticket="412",
        command="/master 412",
        preconditions={"head_sha": "abc123"},
        now=now,
        ttl_s=ttl_s,
    )


# --- record_pending: dedup semantics -----------------------------------------


def test_record_pending_new_event_creates_pending_entry() -> None:
    ledger, outcome = _record({})
    assert outcome == "new"
    entry = ledger["master:412:abc123"]
    assert entry.send_started_at is None
    assert entry.sent_at is None
    assert entry.consumed_at is None


def test_record_pending_duplicate_while_unconsumed_blocks() -> None:
    ledger, _ = _record({})
    ledger2, outcome = _record(ledger, now=105.0)
    assert outcome == "duplicate"
    assert ledger2 == ledger  # unchanged


def test_record_pending_duplicate_within_ttl_after_sent_consumed_blocks() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    # 50s later, well within the 600s ttl -> still suppressed
    ledger2, outcome = _record(ledger, now=150.0, ttl_s=600.0)
    assert outcome == "duplicate"
    assert ledger2 == ledger


def test_record_pending_allows_new_after_ttl_elapses_past_sent_consumed() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    ledger2, outcome = _record(ledger, now=800.0, ttl_s=600.0)  # past the 600s window
    assert outcome == "new"
    assert ledger2["master:412:abc123"].sent_at is None  # fresh entry, reset


def test_record_pending_allows_new_immediately_after_abandoned_consume() -> None:
    # Abandoned (busy/absent) consume: sent_at stays None -> never suppressed by ttl,
    # matching the existing watcher's behaviour of retrying a busy/absent skip
    # on the very next tick (no TTL was ever recorded for it).
    ledger, _ = _record({}, now=100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)  # abandoned, sent_at=None
    ledger2, outcome = _record(ledger, now=101.0, ttl_s=600.0)
    assert outcome == "new"


# --- transport (FRE-872, ADR-0116): default send_keys, flipped only on confirmed
# channel delivery -- never written optimistically, never "corrected" on fallback.
# See docs/superpowers/plans/2026-07-13-fre-872-channel-delivery-end-to-end.md.


def test_record_pending_new_entry_defaults_transport_send_keys() -> None:
    ledger, _ = _record({})
    assert ledger["master:412:abc123"].transport == "send_keys"


def test_mark_transport_flips_to_channel_and_only_that_field() -> None:
    ledger, _ = _record({}, now=100.0)
    before = ledger["master:412:abc123"]
    after_ledger = mark_transport(ledger, "master:412:abc123", "channel")
    after = after_ledger["master:412:abc123"]
    assert after.transport == "channel"
    # every other field is untouched
    assert dataclasses.replace(after, transport=before.transport) == before


def test_load_ledger_missing_transport_key_defaults_send_keys(tmp_path: Path) -> None:
    path = tmp_path / "trigger_ledger.json"
    path.write_text(
        json.dumps(
            {
                "master:412:abc123": {
                    "event_id": "master:412:abc123",
                    "source": "master-ready",
                    "target_pane": "cc-master",
                    "ticket": "412",
                    "command": "/master 412",
                    "preconditions": {},
                    "created_at": 100.0,
                }
            }
        )
    )
    reloaded = load_ledger(path, _NullLogger())
    assert reloaded["master:412:abc123"].transport == "send_keys"


def test_load_ledger_garbage_transport_value_defaults_send_keys(tmp_path: Path) -> None:
    path = tmp_path / "trigger_ledger.json"
    path.write_text(
        json.dumps(
            {
                "master:412:abc123": {
                    "event_id": "master:412:abc123",
                    "source": "master-ready",
                    "target_pane": "cc-master",
                    "ticket": "412",
                    "command": "/master 412",
                    "preconditions": {},
                    "created_at": 100.0,
                    "transport": "carrier-pigeon",
                }
            }
        )
    )
    reloaded = load_ledger(path, _NullLogger())
    assert reloaded["master:412:abc123"].transport == "send_keys"


def test_save_load_round_trip_preserves_channel_transport(tmp_path: Path) -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_transport(ledger, "master:412:abc123", "channel")
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)
    reloaded = load_ledger(path, _NullLogger())
    assert reloaded["master:412:abc123"].transport == "channel"


# --- reconcile: AC-4 crash scenarios ------------------------------------------


def test_reconcile_never_attempted_retries_and_consumes() -> None:
    # "kill between ledger-write and send": entry written, crash before any send
    # attempt (send_started_at is None).
    ledger, _ = _record({})
    spy = _Spy(outcome="sent")
    persisted: list[dict[str, LedgerEntry]] = []
    result = reconcile(
        ledger, now=200.0, execute_pending=spy, persist=persisted.append, logger=_NullLogger()
    )
    assert len(spy.calls) == 1  # exactly one net actuation
    entry = result["master:412:abc123"]
    assert entry.sent_at is not None
    assert entry.consumed_at is not None
    assert entry.surfaced_at is None
    assert persisted  # the crash-safety contract: reconcile persists its own transitions


def test_reconcile_known_sent_closes_out_without_replay() -> None:
    # "kill between send and mark-consumed": send_to_session already returned
    # "sent" (sent_at recorded) before the crash; must NOT resend.
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)  # crash happens right here
    spy = _Spy(outcome="sent")
    result = reconcile(
        ledger, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=_NullLogger()
    )
    assert (
        spy.calls == []
    )  # never replayed -- the original send already counts as the one actuation
    entry = result["master:412:abc123"]
    assert entry.consumed_at is not None


def test_reconcile_ambiguous_mid_send_surfaces_without_replay() -> None:
    # A genuine crash *during* the send call: send_started_at is set, but we never
    # learned whether send_to_session actually returned "sent". Never a blind replay.
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)  # crash happens right here
    spy = _Spy(outcome="sent")
    logger = _RecordingLogger()
    result = reconcile(
        ledger, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=logger
    )
    assert spy.calls == []  # never replayed
    entry = result["master:412:abc123"]
    assert entry.surfaced_at is not None
    assert entry.consumed_at is None  # left pending -- owner intervention required
    assert logger.warnings  # surfaced, never a silent drop


def test_reconcile_retry_failure_surfaces_and_never_retries_again() -> None:
    ledger, _ = _record({})
    spy = _Spy(raises=True)
    logger = _RecordingLogger()
    result = reconcile(
        ledger, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=logger
    )
    assert len(spy.calls) == 1
    entry = result["master:412:abc123"]
    assert entry.surfaced_at is not None
    assert entry.consumed_at is None
    assert logger.warnings

    # A second reconcile pass must not retry an already-surfaced entry.
    result2 = reconcile(
        result, now=300.0, execute_pending=spy, persist=lambda _l: None, logger=logger
    )
    assert len(spy.calls) == 1  # unchanged -- no second attempt
    assert result2 == result


def test_reconcile_busy_absent_leaves_entry_abandoned_not_surfaced() -> None:
    ledger, _ = _record({})
    spy = _Spy(outcome="busy")
    result = reconcile(
        ledger, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=_NullLogger()
    )
    entry = result["master:412:abc123"]
    assert entry.sent_at is None
    assert entry.surfaced_at is None
    assert entry.consumed_at is not None  # abandoned -- eligible for a fresh attempt


def test_reconcile_duplicate_pending_actuates_once() -> None:
    # AC-4 "inject a duplicate event -> assert one actuation": a replayed
    # record_pending() call before consumption is blocked at the ledger-write
    # boundary (see test_record_pending_duplicate_while_unconsumed_blocks), so
    # only one entry ever reaches reconcile.
    ledger, first = _record({}, now=100.0)
    ledger, second = _record(ledger, now=101.0)
    assert first == "new"
    assert second == "duplicate"
    spy = _Spy(outcome="sent")
    reconcile(ledger, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=_NullLogger())
    assert len(spy.calls) == 1


def test_reconcile_empty_ledger_actuates_nothing() -> None:
    # AC-4 "malformed/idle event -> assert zero": an event that never became a
    # trigger never reaches record_pending, so the ledger is empty.
    spy = _Spy(outcome="sent")
    result = reconcile(
        {}, now=200.0, execute_pending=spy, persist=lambda _l: None, logger=_NullLogger()
    )
    assert spy.calls == []
    assert result == {}


# --- AC-1: durable reconstruction after a simulated context clear ------------


def test_snapshot_unconsumed_survives_reload_from_disk(tmp_path: Path) -> None:
    ledger, _ = _record({}, now=100.0)
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)

    # Simulate a context clear: no in-memory state carried over, fresh load only.
    reloaded = load_ledger(path, _NullLogger())
    snapshot = snapshot_unconsumed(reloaded)

    assert len(snapshot) == 1
    assert snapshot[0].event_id == "master:412:abc123"
    assert snapshot[0].consumed_at is None


def test_snapshot_unconsumed_excludes_consumed_entries() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    assert snapshot_unconsumed(ledger) == ()


def test_load_ledger_absent_file_is_empty(tmp_path: Path) -> None:
    assert load_ledger(tmp_path / "nope.json", _NullLogger()) == {}


def test_load_ledger_corrupt_file_warns_loudly_not_silent(tmp_path: Path) -> None:
    path = tmp_path / "trigger_ledger.json"
    path.write_text("{not valid json")
    logger = _RecordingLogger()
    result = load_ledger(path, logger)
    assert result == {}
    assert "trigger_ledger_corrupt" in logger.warnings


def test_save_load_round_trip(tmp_path: Path) -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)
    reloaded = load_ledger(path, _NullLogger())
    assert reloaded == ledger


# --- prune_ledger -------------------------------------------------------------


def test_prune_drops_old_consumed_entry() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    pruned = prune_ledger(ledger, now=100.0 + 999.0, retention_s=500.0, open_prs=[412])
    assert pruned == {}


def test_prune_drops_consumed_entry_for_closed_pr() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    pruned = prune_ledger(ledger, now=101.0, retention_s=999999.0, open_prs=[])
    assert pruned == {}


def test_prune_never_drops_unconsumed_entry_regardless_of_age() -> None:
    ledger, _ = _record({}, now=100.0)  # never consumed
    pruned = prune_ledger(ledger, now=100.0 + 10_000_000.0, retention_s=1.0, open_prs=[])
    assert pruned == ledger


def test_prune_never_drops_surfaced_entry() -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_surfaced(ledger, "master:412:abc123", 100.0)
    pruned = prune_ledger(ledger, now=100.0 + 10_000_000.0, retention_s=1.0, open_prs=[])
    assert pruned == ledger


# --- FRE-848: non-PR-ticketed entries (e.g. context-pressure) ----------------


def _record_non_pr(ledger: dict[str, LedgerEntry], *, now: float = 100.0, ttl_s: float = 600.0):
    return record_pending(
        ledger,
        event_id="ctxpressure:cc-master",
        source="context-pressure",
        target_pane="cc-master",
        ticket="cc-master",  # not a PR number -- session-keyed
        command="Context at 75% ...",
        preconditions={},
        now=now,
        ttl_s=ttl_s,
    )


def test_prune_keeps_non_pr_ticketed_consumed_entry_within_retention() -> None:
    # A session-keyed ticket ("cc-master") is never in open_prs (PR numbers) --
    # must not be evicted by that check alone, only by retention_s.
    ledger, _ = _record_non_pr({}, now=100.0)
    ledger = mark_send_started(ledger, "ctxpressure:cc-master", 100.0)
    ledger = mark_sent(ledger, "ctxpressure:cc-master", 100.0)
    ledger = mark_consumed(ledger, "ctxpressure:cc-master", 100.0)
    pruned = prune_ledger(ledger, now=150.0, retention_s=500.0, open_prs=[412])
    assert "ctxpressure:cc-master" in pruned


def test_prune_drops_non_pr_ticketed_entry_past_retention() -> None:
    ledger, _ = _record_non_pr({}, now=100.0)
    ledger = mark_consumed(ledger, "ctxpressure:cc-master", 100.0)
    pruned = prune_ledger(ledger, now=100.0 + 999.0, retention_s=500.0, open_prs=[412])
    assert pruned == {}


# --- FRE-832: CLI read surface `prime-master` shells out to on rebuild --------


def test_main_json_includes_seeded_unconsumed_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _record({}, now=100.0)
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)

    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["event_id"] == "master:412:abc123"
    assert payload[0]["ticket"] == "412"


def test_main_json_empty_ledger_emits_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "trigger_ledger.json"  # never written -- absent file
    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])
    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_json_consumed_only_emits_empty_list(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_sent(ledger, "master:412:abc123", 100.0)
    ledger = mark_consumed(ledger, "master:412:abc123", 100.0)
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)

    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])

    assert exit_code == 0
    assert json.loads(capsys.readouterr().out) == []


def test_main_json_includes_surfaced_entry(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _record({}, now=100.0)
    ledger = mark_send_started(ledger, "master:412:abc123", 100.0)
    ledger = mark_surfaced(ledger, "master:412:abc123", 100.0)
    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)

    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload) == 1
    assert payload[0]["surfaced_at"] == 100.0


def test_main_json_mixed_ledger_excludes_only_consumed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    ledger, _ = _record({}, now=100.0)  # pending: master:412:abc123
    ledger, _ = record_pending(
        ledger,
        event_id="master:413:def456",
        source="master-ready",
        target_pane="cc-master",
        ticket="413",
        command="/master 413",
        preconditions={"head_sha": "def456"},
        now=100.0,
        ttl_s=600.0,
    )
    ledger = mark_send_started(ledger, "master:413:def456", 100.0)
    ledger = mark_surfaced(ledger, "master:413:def456", 100.0)  # surfaced
    ledger, _ = record_pending(
        ledger,
        event_id="worker:414:ghi789",
        source="worker-bounce",
        target_pane="cc-build1",
        ticket="414",
        command="/prime-worker",
        preconditions={},
        now=100.0,
        ttl_s=600.0,
    )
    ledger = mark_send_started(ledger, "worker:414:ghi789", 100.0)
    ledger = mark_sent(ledger, "worker:414:ghi789", 100.0)
    ledger = mark_consumed(ledger, "worker:414:ghi789", 100.0)  # consumed -- must be excluded

    path = tmp_path / "trigger_ledger.json"
    save_ledger(path, ledger)

    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])

    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out)
    event_ids = {entry["event_id"] for entry in payload}
    assert event_ids == {"master:412:abc123", "master:413:def456"}


def test_main_corrupt_ledger_exits_nonzero_and_warns(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "trigger_ledger.json"
    path.write_text("{not valid json")

    exit_code = main(["--ledger-file", str(path), "--unconsumed", "--json"])

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert "corrupt" in captured.err.lower()


def test_main_requires_unconsumed_flag(tmp_path: Path) -> None:
    path = tmp_path / "trigger_ledger.json"
    with pytest.raises(SystemExit):
        main(["--ledger-file", str(path), "--json"])
