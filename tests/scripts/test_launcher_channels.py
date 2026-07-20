# ruff: noqa: D103
"""Launcher channel-mode wiring tests (FRE-875, ADR-0116 Phase 1 seam).

Channel-mode is derived from a **single source of truth** — the seat's
``StreamTopology.mode`` (``"channel"`` | ``"send_keys"``). The launcher wires the
approved ``--channels plugin:seshat-dispatch@seshat-dispatch`` allowlist reference
and a per-seat ``SESHAT_CHANNEL_PORT`` **iff** that seat's ``mode == "channel"``;
there is no independent per-invocation channel flag (FRE-875 removed it), so the
launch shape can never drift from the mode the watcher reads to pick a transport.

A ``send_keys``-mode seat is its pre-channel shape (ADR-0116 §5, "one seat at a
time") **plus** the FRE-922 background-tasks-disabled ``env`` prefix that every
worker seat now carries. All three live worker seats are cut over to channel
(FRE-875 Phase B complete), so ``send_keys`` is no longer any live seat's default
— it remains the ``StreamTopology`` dataclass default and the channel-down
fallback target, and these tests construct it explicitly via ``_flip_to_send_keys``.
The shared secret is never placed on the command line (it would leak via
``ps``/plan JSON); it is provisioned out-of-band into the seat's environment.
"""

from __future__ import annotations

import dataclasses
import json

import pytest
from scripts.dispatch import launcher
from scripts.dispatch.launcher import (
    main,
    plan_launch,
    topology_for,
)

_CHANNEL_REF = "plugin:seshat-dispatch@seshat-dispatch"


def _inner(plan) -> str:  # type: ignore[no-untyped-def]
    """Return the claude invocation tmux runs (the last argv element)."""
    assert plan.command is not None
    return plan.command[-1]


def _flip_to_channel(monkeypatch: pytest.MonkeyPatch, stream: str) -> None:
    """Flip one seat's topology to channel-mode — exactly what a cutover edits.

    Mirrors the Phase-B cutover mutation (a one-field change to ``_TOPOLOGY``) so
    the test proves the *binding* between mode and launch shape, not a synthetic
    flag.
    """
    channel_topology = dataclasses.replace(topology_for(stream), mode="channel")
    monkeypatch.setitem(launcher._TOPOLOGY, stream, channel_topology)


def _flip_to_send_keys(monkeypatch: pytest.MonkeyPatch, stream: str) -> None:
    """Flip one seat's topology to send_keys-mode — the pre-cutover shape.

    All live worker seats are channel-mode now (Phase B complete), so a test that
    proves the send_keys argv shape must construct that mode explicitly.
    """
    send_keys_topology = dataclasses.replace(topology_for(stream), mode="send_keys")
    monkeypatch.setitem(launcher._TOPOLOGY, stream, send_keys_topology)


def test_send_keys_seat_argv_unchanged_by_default(monkeypatch: pytest.MonkeyPatch) -> None:
    # A send_keys-mode seat → the pre-channel argv, untouched (the dataclass
    # default and the channel-down fallback shape; constructed explicitly now
    # that every live worker seat is channel-mode).
    _flip_to_send_keys(monkeypatch, "build2")
    plan = plan_launch("build2", "FRE-871", "haiku", context_keep=False)
    inner = _inner(plan)
    assert "--channels" not in inner
    assert "SESHAT_CHANNEL_PORT" not in inner
    # FRE-922: every worker seat is launched with background tasks disabled, so
    # even a send_keys seat carries the env prefix — no channel port, though.
    assert inner.startswith(
        "env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 "
        "claude --remote-control -n cc-2build --model haiku --session-id "
    )
    assert inner.endswith("'/build FRE-871'")  # seed is shlex-quoted (contains a space)


def test_channel_mode_seat_adds_allowlist_ref_and_per_seat_port(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _flip_to_channel(monkeypatch, "adr")
    plan = plan_launch("adr", "FRE-875", "opus", context_keep=False)
    inner = _inner(plan)
    assert f"--channels {_CHANNEL_REF}" in inner
    port = topology_for("adr").channel_port
    # FRE-922: the background-tasks kill flag and the per-seat port share one
    # ``env`` prefix (order: kill flag first, then port), before ``claude``.
    assert f"env CLAUDE_CODE_DISABLE_BACKGROUND_TASKS=1 SESHAT_CHANNEL_PORT={port} claude" in inner
    # The seat is still an RC seat at its model — the channel composes, not replaces.
    assert "claude --remote-control -n cc-adrs --model opus" in inner
    # The seed must survive channel-mode AND precede the variadic --channels flag,
    # or --channels would swallow it as a second channel ref (never run the turn).
    assert "'/adr FRE-875'" in inner
    assert inner.index("'/adr FRE-875'") < inner.index("--channels")


def test_channel_wiring_is_derived_from_topology_mode_and_cannot_drift(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The single-source invariant: for any seat, launch channel-wiring is
    # exactly (topology.mode == "channel"). There is no other input that could
    # make the two disagree — the drift class FRE-872 flagged is gone.
    _flip_to_send_keys(monkeypatch, "adr")
    assert "--channels" not in _inner(plan_launch("adr", "FRE-875", "opus", context_keep=False))
    _flip_to_channel(monkeypatch, "adr")
    assert "--channels" in _inner(plan_launch("adr", "FRE-875", "opus", context_keep=False))


def test_channel_ports_are_per_seat_distinct() -> None:
    ports = {s: topology_for(s).channel_port for s in ("build1", "build2", "adr")}
    assert len(set(ports.values())) == 3


def test_secret_is_never_on_the_command_line(monkeypatch: pytest.MonkeyPatch) -> None:
    _flip_to_channel(monkeypatch, "build1")
    plan = plan_launch("build1", "FRE-875", "opus", context_keep=False)
    assert "SESHAT_CHANNEL_SECRET" not in _inner(plan)


def test_no_independent_channels_cli_flag() -> None:
    # FRE-875 removed the per-invocation --channels flag; it is now a parse error,
    # so an operator can never launch a seat in a shape that contradicts its mode.
    with pytest.raises(SystemExit):
        main(["--stream", "build2", "--model", "haiku", "--ticket", "FRE-875", "--channels"])


def test_main_channel_seat_wires_channel(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _flip_to_channel(monkeypatch, "build2")
    rc = main(["--stream", "build2", "--model", "haiku", "--ticket", "FRE-875", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert f"--channels {_CHANNEL_REF}" in payload["command"][-1]


def test_main_send_keys_seat_has_no_channel(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    _flip_to_send_keys(monkeypatch, "build2")
    rc = main(["--stream", "build2", "--model", "haiku", "--ticket", "FRE-875", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "--channels" not in payload["command"][-1]
