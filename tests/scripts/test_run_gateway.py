"""Tests for docker/mcp/run-gateway.sh MCP server list configuration.

FRE-338: Verify run-gateway.sh reads AGENT_MCP_GATEWAY_ENABLED_SERVERS env var.

Tests invoke the real script with a stubbed docker on PATH to assert
that --servers is passed with the correct value under both env conditions.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from pathlib import Path


def test_run_gateway_script_exists() -> None:
    """Verify run-gateway.sh exists and is executable."""
    script = Path(__file__).parent.parent.parent / "docker/mcp/run-gateway.sh"
    assert script.exists(), f"Script not found: {script}"
    assert script.stat().st_mode & 0o111, f"Script not executable: {script}"


def test_run_gateway_default_servers() -> None:
    """When env var unset, script should pass default servers to docker.

    The default should be --servers "sequentialthinking,context7".
    We stub docker on PATH to capture the --servers argument.
    """
    script_path = Path(__file__).parent.parent.parent / "docker/mcp/run-gateway.sh"

    # Create a stub docker that echoes its arguments and exits 0
    stub_docker = """#!/bin/bash
    # Stub docker: echo args and exit 0
    echo "docker args: $@"
    exit 0
    """

    with tempfile.TemporaryDirectory() as tmpdir:
        stub_path = Path(tmpdir) / "docker"
        stub_path.write_text(stub_docker)
        stub_path.chmod(0o755)

        # Run script with env var unset, stub docker on PATH
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "AGENT_MCP_GATEWAY_ENABLED_SERVERS": "",  # explicitly unset
            },
        )

        # Should succeed (stub docker exits 0)
        assert result.returncode == 0, f"script failed: {result.stderr}"

        # Verify --servers arg contains default servers
        output = result.stdout + result.stderr
        assert "--servers" in output, f"--servers not found in output: {output}"
        assert "sequentialthinking,context7" in output, (
            f"Default servers not found in output: {output}"
        )


def test_run_gateway_custom_servers() -> None:
    """When env var set, script should pass custom servers to docker.

    We test with custom array ["sequentialthinking", "context7", "linear"].
    """
    script_path = Path(__file__).parent.parent.parent / "docker/mcp/run-gateway.sh"

    # Create a stub docker that echoes its arguments
    stub_docker = """#!/bin/bash
    echo "docker args: $@"
    exit 0
    """

    custom_servers = json.dumps(["sequentialthinking", "context7", "linear"])

    with tempfile.TemporaryDirectory() as tmpdir:
        stub_path = Path(tmpdir) / "docker"
        stub_path.write_text(stub_docker)
        stub_path.chmod(0o755)

        # Run script with custom env var, stub docker on PATH
        result = subprocess.run(
            [str(script_path)],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "PATH": f"{tmpdir}:{os.environ.get('PATH', '')}",
                "AGENT_MCP_GATEWAY_ENABLED_SERVERS": custom_servers,
            },
        )

        # Should succeed
        assert result.returncode == 0, f"script failed: {result.stderr}"

        # Verify --servers arg contains custom servers
        output = result.stdout + result.stderr
        assert "--servers" in output, f"--servers not found in output: {output}"
        assert "sequentialthinking,context7,linear" in output, (
            f"Custom servers not found in output: {output}"
        )
