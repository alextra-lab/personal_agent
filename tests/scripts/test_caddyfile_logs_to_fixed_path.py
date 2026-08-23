# ruff: noqa: D103
"""FRE-1243 — every Caddy access-log writer targets the fixed shared-volume path.

Adapts `config/cloud-sim/Caddyfile` with the real `caddy` binary (the same `caddy:2-alpine` image
`.github/workflows/ci.yml`'s `caddy-validate` job already uses for syntax-only validation) and
asserts semantically on the resulting JSON, rather than regexing the Caddyfile text — a regex over
8 loggers (one with a nested `format filter {...} wrap json`) is exactly the kind of structural
mismatch a real parser doesn't have.

Recreating Caddy alone must never again require a companion Filebeat action (AC-1): Filebeat only
tails a fixed path, so every logger here writing anywhere other than that fixed path — or with a
writer config that silently diverges from its siblings, since Caddy pools file writers by filename
and the first-opened config wins for every other logger targeting the same path — would reopen the
coordination gap this ticket closes.
"""

from __future__ import annotations

import json
import shutil
import subprocess

import pytest

from personal_agent.config.config_guard import repo_root

pytestmark = pytest.mark.skipif(shutil.which("docker") is None, reason="requires the docker CLI")

CADDYFILE = repo_root() / "config" / "cloud-sim" / "Caddyfile"
FIXED_LOG_PATH = "/var/log/caddy/access.log"


def _adapt() -> dict:
    result = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{CADDYFILE}:/etc/caddy/Caddyfile:ro",
            "caddy:2-alpine",
            "caddy",
            "adapt",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return json.loads(result.stdout)


def _non_default_writers() -> list[dict]:
    logs = _adapt()["logging"]["logs"]
    return [cfg["writer"] for name, cfg in logs.items() if name != "default"]


def test_eight_access_loggers_are_declared() -> None:
    """One writer per site block using the `log` directive.

    The `routing` snippet (unnamed `log {}`) is imported by both `localhost` and `{$AGENT_HOST}`,
    each compiling to its own logger — so 8, not 7: routing x2 + graph + es + otlp + api +
    egress_slm + egress_artifacts.
    """
    assert len(_non_default_writers()) == 8


def test_every_logger_writes_to_the_fixed_file_path() -> None:
    for writer in _non_default_writers():
        assert writer["output"] == "file"
        assert writer["filename"] == FIXED_LOG_PATH


def test_all_eight_writers_are_configured_identically() -> None:
    """Catches a fat-fingered sub-option silently losing to a sibling logger's writer config.

    Caddy pools file writers by filename — a mismatched sub-option on one block would be silently
    discarded in favor of whichever logger's writer config opens first, with no error.
    """
    writers = _non_default_writers()
    assert len(writers) == 8
    distinct = {json.dumps(w, sort_keys=True) for w in writers}
    assert len(distinct) == 1, f"writer configs diverge across loggers: {writers}"


def test_no_logger_writes_to_stdout() -> None:
    for writer in _non_default_writers():
        assert writer["output"] != "stdout"
