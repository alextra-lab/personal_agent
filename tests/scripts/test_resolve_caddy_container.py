# ruff: noqa: D103
"""FRE-1146 / ADR-0132 D3 — tests for config/filebeat/resolve-caddy-container.sh.

The resolver is startup-critical logic (it's what lets the container avoid mounting
/var/run/docker.sock — see the plan's decision 1), so it's exercised directly rather than
only asserted indirectly through the filebeat.yml/compose config it's invoked from. A fake
``filebeat`` executable on PATH captures what the script would have exec'd into, so no real
Filebeat binary is needed.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT = REPO_ROOT / "config" / "filebeat" / "resolve-caddy-container.sh"


def _write_fake_filebeat(bin_dir: Path) -> Path:
    """A stand-in ``filebeat`` that just dumps its env/args so the test can assert on them."""
    fake = bin_dir / "filebeat"
    fake.write_text(
        '#!/bin/sh\necho "CADDY_CONTAINER_ID=${CADDY_CONTAINER_ID:-}"\necho "ARGS=$*"\n'
    )
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return fake


def _write_container_config(containers_dir: Path, container_id: str, name: str) -> None:
    cfg_dir = containers_dir / container_id
    cfg_dir.mkdir(parents=True)
    (cfg_dir / "config.v2.json").write_text(f'{{"Name":"/{name}"}}')


def test_resolves_matching_container_id_and_execs_filebeat(tmp_path: Path) -> None:
    containers_dir = tmp_path / "containers"
    _write_container_config(containers_dir, "abc123", "cloud-sim-caddy")
    _write_container_config(containers_dir, "other456", "some-unrelated-container")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_filebeat(bin_dir)

    env = {
        **os.environ,
        "DOCKER_CONTAINERS_DIR": str(containers_dir),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert "CADDY_CONTAINER_ID=abc123" in result.stdout


def test_exits_nonzero_when_no_matching_container(tmp_path: Path) -> None:
    containers_dir = tmp_path / "containers"
    _write_container_config(containers_dir, "other456", "some-unrelated-container")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_filebeat(bin_dir)

    env = {
        **os.environ,
        "DOCKER_CONTAINERS_DIR": str(containers_dir),
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )

    assert result.returncode != 0
    assert "no container named cloud-sim-caddy found" in result.stderr


def test_respects_custom_container_name(tmp_path: Path) -> None:
    containers_dir = tmp_path / "containers"
    _write_container_config(containers_dir, "xyz789", "a-different-caddy-name")

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_fake_filebeat(bin_dir)

    env = {
        **os.environ,
        "DOCKER_CONTAINERS_DIR": str(containers_dir),
        "CADDY_CONTAINER_NAME": "a-different-caddy-name",
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
    }
    result = subprocess.run(
        ["sh", str(SCRIPT)], env=env, capture_output=True, text=True, timeout=10
    )

    assert result.returncode == 0, result.stderr
    assert "CADDY_CONTAINER_ID=xyz789" in result.stdout
