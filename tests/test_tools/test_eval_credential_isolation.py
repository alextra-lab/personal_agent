"""Outcome tests: a bash call on an eval gateway cannot read a credential (FRE-1505).

Each test boots a real subprocess that stands in for the gateway process. The subprocess
holds seeded fake credentials in its environment, registers the MVP tools through the
production path (``register_mvp_tools``), and runs the registered ``bash`` executor.

``/proc/$PPID/environ`` from the bash child is the gateway-equivalent of the container's
``/proc/1/environ``: the bash child's parent is the process that holds the credentials.

The cloud-profile test is the seeded negative for the instrument. The same commands must
reveal the seeded values there, or the eval assertions prove nothing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_SEEDED = {
    # The two keys the eval stack really holds (the profile refuses to boot without them).
    "AGENT_ANTHROPIC_API_KEY": "seeded-anthropic-key-4e2a8c61",
    "AGENT_OPENAI_API_KEY": "seeded-openai-key-a07d3b95",
    "AGENT_SEEDED_API_KEY": "seeded-api-key-7d41c9a2",
    "SEEDED_ACCESS_TOKEN": "seeded-access-token-0b8e55f3",
    "SEEDED_CLIENT_SECRET": "seeded-client-secret-c3a19d07",
    "SEEDED_DB_PASSWORD": "seeded-db-password-91f2e6b4",
}

_LEAK_COMMAND = "printenv; cat /proc/$PPID/environ | tr '\\0' '\\n'"

_GATEWAY_STANDIN = (
    """
import asyncio, json
from personal_agent.telemetry import TraceContext
from personal_agent.tools import register_mvp_tools
from personal_agent.tools.registry import ToolRegistry

registry = ToolRegistry()
register_mvp_tools(registry)
entry = registry.get_tool("bash")
out = {"bash_registered": entry is not None}
if entry is not None:
    executor = entry[1]
    ctx = TraceContext.new_trace()
    out["leak"] = asyncio.run(executor(command=%r, ctx=ctx))
    out["ok"] = asyncio.run(executor(command="echo ok", ctx=ctx))
print("RESULT:" + json.dumps(out))
"""
    % _LEAK_COMMAND
)


def _run_gateway_standin(profile: str) -> dict[str, object]:
    """Run the gateway stand-in subprocess under ``profile`` and return its result.

    Args:
        profile: Value for ``AGENT_DEPLOYMENT_PROFILE``.

    Returns:
        The JSON result the stand-in printed.
    """
    env = {
        **os.environ,
        **_SEEDED,
        "AGENT_DEPLOYMENT_PROFILE": profile,
        "AGENT_PRIMITIVE_TOOLS_ENABLED": "true",
    }
    proc = subprocess.run(
        [sys.executable, "-c", _GATEWAY_STANDIN],
        env=env,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
    assert lines, f"stand-in printed no result (rc={proc.returncode}): {proc.stderr[-2000:]}"
    return json.loads(lines[-1].removeprefix("RESULT:"))


def _output(result: dict[str, object], key: str) -> str:
    """Return the combined stdout and stderr of one bash call in the result."""
    call = result[key]
    assert isinstance(call, dict)
    return f"{call.get('stdout', '')}{call.get('stderr', '')}"


@pytest.fixture(scope="module")
def eval_result() -> dict[str, object]:
    """The stand-in result under the eval deployment profile."""
    return _run_gateway_standin("eval")


@pytest.fixture(scope="module")
def cloud_result() -> dict[str, object]:
    """The stand-in result under the production (cloud) deployment profile."""
    return _run_gateway_standin("cloud")


def test_eval_bash_cannot_read_any_seeded_credential(eval_result: dict[str, object]) -> None:
    """AC-1: printenv and the parent's environ show no credential value on eval."""
    assert eval_result["bash_registered"] is True
    output = _output(eval_result, "leak")
    leaked = [name for name, value in _SEEDED.items() if value in output]
    assert leaked == [], f"credential values readable from bash on eval: {leaked}"


def test_eval_bash_still_runs_plain_commands(eval_result: dict[str, object]) -> None:
    """AC-3: ``echo ok`` still returns ``ok`` under the eval hardening."""
    ok = eval_result["ok"]
    assert isinstance(ok, dict)
    assert ok["success"] is True
    assert ok["stdout"] == "ok\n"


def test_cloud_bash_behaviour_is_unchanged(cloud_result: dict[str, object]) -> None:
    """AC-4 and instrument check: production bash still inherits the environment.

    If this fails, the leak command cannot see credentials at all, and the eval test
    above would pass vacuously.
    """
    assert cloud_result["bash_registered"] is True
    output = _output(cloud_result, "leak")
    assert all(value in output for value in _SEEDED.values())
