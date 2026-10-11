"""FRE-1590 envdiff (scripts/eval/fre1590/envdiff.py), offline: fake environments, no docker."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import pytest
from scripts.eval.fre1590 import envdiff

SECRET = "lin_SEEDED_SECRET_VALUE_7f3a"
PROD_DB = "postgresql://seshat_app:prod-db-secret@postgres:5432/personal_agent"
EVAL_DB = "postgresql://seshat_app:eval-db-secret@postgres-eval:5432/personal_agent"


def envs(
    prod_extra: Mapping[str, str] | None = None, control_extra: Mapping[str, str] | None = None
) -> dict[str, dict[str, str]]:
    shared = {"PATH": "/usr/local/bin:/usr/bin", "AGENT_TEMPO_URL": "http://tempo:3200"}
    prod = {
        **shared,
        "HOSTNAME": "aaaa",
        "AGENT_DATABASE_URL": PROD_DB,
        "AGENT_DEPLOYMENT_PROFILE": "cloud",
        "AGENT_LINEAR_API_KEY": SECRET,
        "AGENT_FRESHNESS_ENABLED": "true",
        **(prod_extra or {}),
    }

    def eval_env(host: str) -> dict[str, str]:
        return {
            **shared,
            "HOSTNAME": host,
            "AGENT_DATABASE_URL": EVAL_DB,
            "AGENT_DEPLOYMENT_PROFILE": "eval",
            "AGENT_ENABLE_SECOND_BRAIN": "false",
        }

    return {
        envdiff.PRODUCTION: prod,
        envdiff.CONTROL: {**eval_env("bbbb"), **(control_extra or {})},
        envdiff.TREATMENT: eval_env("cccc"),
    }


def fake(table: Mapping[str, Mapping[str, str]]) -> envdiff.FetchEnv:
    return lambda name: table[name]


def all_values(table: Mapping[str, Mapping[str, str]]) -> set[str]:
    # Short values ("true", "eval") also occur in container names and reasons; the long ones
    # (URLs, credentials, the seeded secret) are what must never be printed.
    return {v for env in table.values() for v in env.values() if len(v) >= 12}


def test_expected_differences_pass(capsys: pytest.CaptureFixture[str]) -> None:
    table = envs()
    assert envdiff.main([], fetch=fake(table)) == 0
    out = capsys.readouterr().out
    assert "expected    AGENT_LINEAR_API_KEY" in out
    assert "expected    AGENT_FRESHNESS_ENABLED" in out
    assert "UNEXPECTED" not in out
    assert "RESULT: PASS" in out


def test_an_unexpected_key_fails(capsys: pytest.CaptureFixture[str]) -> None:
    table = envs(prod_extra={"AGENT_EXPANSION_ENABLED": "true"})
    assert envdiff.main([], fetch=fake(table)) == 1
    out = capsys.readouterr().out
    assert "UNEXPECTED  AGENT_EXPANSION_ENABLED" in out
    assert "RESULT: FAIL" in out


def test_any_difference_between_the_arms_fails(capsys: pytest.CaptureFixture[str]) -> None:
    # Expected against production, but the two arms must not differ in it.
    table = envs(control_extra={"AGENT_PRIMITIVE_TOOLS_ENABLED": "false"})
    assert envdiff.main([], fetch=fake(table)) == 1
    out = capsys.readouterr().out
    arms_block = out.split(f"== {envdiff.CONTROL} vs {envdiff.TREATMENT}")[1]
    assert "UNEXPECTED  AGENT_PRIMITIVE_TOOLS_ENABLED" in arms_block


@pytest.mark.parametrize("extra", [{}, {"AGENT_EXPANSION_ENABLED": SECRET}])
def test_no_value_ever_appears_in_the_output(
    capsys: pytest.CaptureFixture[str], extra: Mapping[str, str]
) -> None:
    table = envs(prod_extra=extra)
    envdiff.main([], fetch=fake(table))
    captured = capsys.readouterr()
    for value in all_values(table):
        assert value not in captured.out
        assert value not in captured.err


def test_printenv_is_split_on_nul_so_a_value_line_is_never_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stdout = b"A=1\0PEM=-----BEGIN-----\nLEAKED=line-of-a-key\n-----END-----\0B=2\0"

    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(args, 0, stdout=stdout, stderr=b"")

    monkeypatch.setattr(envdiff.subprocess, "run", run)
    env = envdiff.fetch_env("c")
    assert set(env) == {"A", "PEM", "B"}


def test_a_failed_docker_exec_exits_2_without_output(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    def run(*args: object, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        return subprocess.CompletedProcess(
            args, 1, stdout=SECRET.encode(), stderr=b"no such container"
        )

    monkeypatch.setattr(envdiff.subprocess, "run", run)
    assert envdiff.main([]) == 2
    captured = capsys.readouterr()
    assert SECRET not in captured.out + captured.err
    assert captured.out == ""
