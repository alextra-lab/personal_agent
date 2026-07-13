# ruff: noqa: D103
"""Launcher channel-mode wiring tests (FRE-871, ADR-0116 Phase 1 / AC-2).

The launcher gains an opt-in channel-mode: when on, the RC seat argv carries the
approved ``--channels plugin:seshat-dispatch@seshat-dispatch`` allowlist reference
and a per-seat ``SESHAT_CHANNEL_PORT``. Default OFF: a not-yet-cutover seat's argv
is byte-for-byte what it was before (ADR-0116 §5, "one seat at a time"). The shared
secret is never placed on the command line (it would leak via ``ps``/plan JSON); it
is provisioned out-of-band into the seat's environment.
"""

from __future__ import annotations

import json

from scripts.dispatch.launcher import (
    DEFAULT_CAPABILITIES,
    LauncherCapabilities,
    main,
    plan_launch,
    topology_for,
)

_CHANNEL_REF = "plugin:seshat-dispatch@seshat-dispatch"


def _inner(plan) -> str:  # type: ignore[no-untyped-def]
    """Return the claude invocation tmux runs (the last argv element)."""
    assert plan.command is not None
    return plan.command[-1]


def test_channel_mode_off_by_default_argv_unchanged() -> None:
    plan = plan_launch("build2", "FRE-871", "haiku", context_keep=False)
    assert DEFAULT_CAPABILITIES.channels is False
    inner = _inner(plan)
    assert "--channels" not in inner
    assert "SESHAT_CHANNEL_PORT" not in inner
    # The exact pre-FRE-871 shape — a not-yet-cutover seat is untouched.
    assert inner.startswith("claude --remote-control cc-build2 --model haiku --session-id ")
    assert inner.endswith("'/build FRE-871'")  # seed is shlex-quoted (contains a space)


def test_channel_mode_on_adds_allowlist_ref_and_per_seat_port() -> None:
    caps = LauncherCapabilities(channels=True)
    plan = plan_launch("build2", "FRE-871", "haiku", context_keep=False, capabilities=caps)
    inner = _inner(plan)
    assert f"--channels {_CHANNEL_REF}" in inner
    port = topology_for("build2").channel_port
    assert f"env SESHAT_CHANNEL_PORT={port}" in inner
    # The seat is still an RC seat at its model — the channel composes, not replaces.
    assert "claude --remote-control cc-build2 --model haiku" in inner
    # The seed must survive channel-mode AND precede the variadic --channels flag,
    # or --channels would swallow it as a second channel ref (never run the turn).
    assert "'/build FRE-871'" in inner
    assert inner.index("'/build FRE-871'") < inner.index("--channels")


def test_channel_ports_are_per_seat_distinct() -> None:
    ports = {s: topology_for(s).channel_port for s in ("build1", "build2", "adr")}
    assert len(set(ports.values())) == 3


def test_secret_is_never_on_the_command_line() -> None:
    caps = LauncherCapabilities(channels=True)
    plan = plan_launch("build1", "FRE-871", "opus", context_keep=False, capabilities=caps)
    assert "SESHAT_CHANNEL_SECRET" not in _inner(plan)


def test_main_channels_flag_enables_channel_mode(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(
        ["--stream", "build2", "--model", "haiku", "--ticket", "FRE-871", "--channels", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    inner = payload["command"][-1]
    assert f"--channels {_CHANNEL_REF}" in inner


def test_main_without_channels_flag_stays_off(capsys) -> None:  # type: ignore[no-untyped-def]
    rc = main(["--stream", "build2", "--model", "haiku", "--ticket", "FRE-871", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "--channels" not in payload["command"][-1]
