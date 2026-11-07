# ruff: noqa: D103
"""The pure parts of the live dev-agent check, and its guard against touching production."""

from __future__ import annotations

import pytest
from scripts.verify_dev_agent_images import (
    _WRAPPER_SCRIPT,
    canary_target_specs,
    main,
    parse_version,
    wrapper_git_flags,
)

from personal_agent.dev_agent.verification import REQUIRED_DENIED_LABELS


@pytest.mark.parametrize(
    ("text", "version"),
    [("opencode v2.2.5\n", "2.2.5"), ("2.10.0", "2.10.0"), ("no version here", None), ("", None)],
)
def test_parse_version(text: str, version: str | None) -> None:
    assert parse_version(text) == version


def test_canary_specs_cover_every_required_service_label() -> None:
    specs = canary_target_specs("172.30.0.9")
    labels = {s.split("|")[0] for s in specs}
    services = {
        "postgres",
        "neo4j",
        "elasticsearch",
        "redis",
        "caddy-8600",
        "caddy-8601",
        "gateway",
    }
    assert services == labels
    assert services <= REQUIRED_DENIED_LABELS
    assert all(s.endswith("|direct|deny") and "|172.30.0.9|" in s for s in specs)


@pytest.mark.parametrize("flag", ["--up", "--down"])
def test_up_and_down_refuse_the_production_project(flag: str) -> None:
    with pytest.raises(SystemExit) as exit_info:
        main([flag])
    assert exit_info.value.code == 2


def test_wrapper_flags_are_read_from_the_real_script() -> None:
    flags = wrapper_git_flags(_WRAPPER_SCRIPT.read_text(encoding="utf-8"))
    assert {
        "core.hooksPath=/dev/null",
        "core.fsmonitor=false",
        "safe.directory=*",
        "protocol.allow=never",
    } <= set(flags)


def test_wrapper_flags_of_a_script_without_overrides_are_empty() -> None:
    assert wrapper_git_flags("git bundle create x y\n") == []
