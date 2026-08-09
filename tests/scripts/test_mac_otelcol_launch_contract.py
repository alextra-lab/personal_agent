# ruff: noqa: D103
"""FRE-1224 — the launch wrapper must EXPORT the credential vars, and must fail closed without them.

Behavioural, not just a text scan. Codex plan-review raised this as blocking and it is the one
failure in this ticket that fails *open*: a bare `. file` sets shell variables, not child-process
environment variables, so the Collector's `${env:...}` references would resolve to empty and it
would ship spans to the Cloudflare edge with **blank Access headers** — a custody ticket quietly
authenticating with nothing. A static "does the file contain set -a" assertion would pass on a
script that still failed this way (e.g. `set -a` placed after the source), so these tests run the
wrapper and inspect the environment it actually hands its child.

The wrapper honours SESHAT_OTELCOL_BIN so the child can be a stub here instead of a real Collector.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

from personal_agent.config.config_guard import repo_root
from tests.scripts.test_mac_otel_collector_config import REQUIRED_ENV_VARS

_WRAPPER = "scripts/mac/otelcol_launch.sh"

# sysexits.h EX_CONFIG — a configuration error, distinct from a crash, so a respawn loop is
# diagnosable from `launchctl print` output alone.
_EX_CONFIG = 78


def _wrapper_path() -> Path:
    return repo_root() / _WRAPPER


def _write_env_file(tmp_path: Path, values: dict[str, str]) -> Path:
    env_file = tmp_path / "otlp-collector.env"
    env_file.write_text("".join(f"{k}={v}\n" for k, v in values.items()))
    env_file.chmod(0o600)
    return env_file


def _write_stub_collector(tmp_path: Path) -> Path:
    """A stand-in for otelcol that dumps the environment it was handed, then exits 0."""
    stub = tmp_path / "otelcol-stub"
    stub.write_text("#!/bin/sh\nprintenv\n")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC | stat.S_IRUSR)
    return stub


def _complete_env() -> dict[str, str]:
    return {
        "SESHAT_OTLP_INGRESS_URL": "https://otlp.example.com",
        "SESHAT_OTLP_CF_ACCESS_CLIENT_ID": "test-client-id",
        "SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET": "test-client-secret",
    }


def _run(tmp_path: Path, env_file: Path) -> subprocess.CompletedProcess[str]:
    stub = _write_stub_collector(tmp_path)
    return subprocess.run(
        ["sh", str(_wrapper_path())],
        capture_output=True,
        text=True,
        check=False,
        env={
            **os.environ,
            "SESHAT_OTLP_ENV_FILE": str(env_file),
            "SESHAT_OTELCOL_BIN": str(stub),
        },
    )


def test_wrapper_exists_and_is_executable() -> None:
    path = _wrapper_path()
    assert path.exists(), f"{_WRAPPER} not found"
    assert path.stat().st_mode & stat.S_IXUSR, f"{_WRAPPER} is not executable"


def test_required_vars_reach_the_child_process_environment(tmp_path: Path) -> None:
    """The core of the finding: sourcing alone would leave these as shell-local variables."""
    result = _run(tmp_path, _write_env_file(tmp_path, _complete_env()))
    assert result.returncode == 0, f"wrapper failed: {result.stderr}"
    child_env = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
    for var in REQUIRED_ENV_VARS:
        assert var in child_env, (
            f"{var} never reached the child environment — the Collector would resolve "
            f"${{env:{var}}} to empty and authenticate with a blank header"
        )
        assert child_env[var], f"{var} reached the child but is empty"


@pytest.mark.parametrize("missing", REQUIRED_ENV_VARS)
def test_wrapper_fails_closed_when_a_required_var_is_absent(tmp_path: Path, missing: str) -> None:
    """Fail closed, not open: refuse to start rather than ship spans with an empty credential."""
    values = {k: v for k, v in _complete_env().items() if k != missing}
    result = _run(tmp_path, _write_env_file(tmp_path, values))
    assert result.returncode == _EX_CONFIG, (
        f"expected exit {_EX_CONFIG} with {missing} absent, got {result.returncode}"
    )
    assert missing in result.stderr, "the error must name the missing variable"


def test_wrapper_error_does_not_echo_credential_values(tmp_path: Path) -> None:
    """Diagnostics name variables, never values — the log is persistent (launchd stdout)."""
    values = {k: v for k, v in _complete_env().items() if k != "SESHAT_OTLP_INGRESS_URL"}
    result = _run(tmp_path, _write_env_file(tmp_path, values))
    assert "test-client-secret" not in (result.stdout + result.stderr)


def test_wrapper_fails_closed_when_the_env_file_is_missing(tmp_path: Path) -> None:
    result = _run(tmp_path, tmp_path / "does-not-exist.env")
    assert result.returncode == _EX_CONFIG


@pytest.mark.parametrize("mode", [0o660, 0o606, 0o666, 0o622])
def test_wrapper_refuses_a_group_or_world_writable_env_file(tmp_path: Path, mode: int) -> None:
    """The env file is sourced, i.e. executed.

    A file another principal can write is arbitrary code execution inside a launchd-persistent
    context, re-run at every login, with KeepAlive guaranteeing the retry. `chmod 600` is a manual
    step in the documented install, so the wrapper enforces it rather than trusting it.
    """
    env_file = _write_env_file(tmp_path, _complete_env())
    env_file.chmod(mode)
    result = _run(tmp_path, env_file)
    assert result.returncode == _EX_CONFIG, (
        f"wrapper sourced a {oct(mode)} env file instead of refusing"
    )
    assert "writable" in result.stderr


def test_wrapper_accepts_a_correctly_locked_down_env_file(tmp_path: Path) -> None:
    """The mode check must not be so strict it rejects the documented 0600 file."""
    env_file = _write_env_file(tmp_path, _complete_env())
    env_file.chmod(0o600)
    assert _run(tmp_path, env_file).returncode == 0


def test_installed_unit_runs_frozen_copies_not_the_working_tree() -> None:
    """A persistent agent holding a live credential must not execute mutable repo files.

    Otherwise checking out a branch — which reviewers reasonably treat as a read-only act —
    changes both the code the agent runs and the endpoint it sends the Cloudflare Access token to.
    """
    installer = (repo_root() / "scripts/mac/install_otelcol.sh").read_text()
    assert "LIBEXEC_DIR" in installer
    assert 'cp "${REPO_ROOT}/scripts/mac/otelcol_launch.sh" "$WRAPPER_PATH"' in installer
    assert 'cp "${REPO_ROOT}/config/otel/mac-collector-config.yaml" "$CONFIG_PATH"' in installer
    # The rendered plist must not point back into the repo.
    assert 'WRAPPER_PATH="${LIBEXEC_DIR}' in installer
    assert 'CONFIG_PATH="${LIBEXEC_DIR}' in installer

    template = (repo_root() / "config/otel/com.seshat.otelcol.plist.template").read_text()
    assert "@@CONFIG_PATH@@" in template, "the unit must pin the config path explicitly"


def test_installer_is_executable() -> None:
    """docs/guides/MAC_OTEL_COLLECTOR.md documents invoking it directly."""
    installer = repo_root() / "scripts/mac/install_otelcol.sh"
    assert installer.stat().st_mode & stat.S_IXUSR, "install_otelcol.sh is not executable"
