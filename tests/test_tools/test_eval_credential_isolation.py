"""A bash call on an eval gateway cannot read a credential (FRE-1505).

Two kinds of test live here.

The subprocess tests boot a real process that stands in for the gateway. It holds seeded
fake credentials, registers the MVP tools through the production path
(``register_mvp_tools``), and runs the registered ``bash`` executor. They prove AC-4
(production unchanged) and the fail-closed refusal when the gateway is not root.

The eval isolation itself (drop to ``nobody``) needs a root parent. CI and the dev VPS run
tests unprivileged, so the root outcome (AC-1, AC-3) is proven in the gateway image by
``scripts/eval/probe_bash_credential_isolation.py``. Here the spawn arguments are checked
with a mocked subprocess, and that test says so.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives.bash import (
    EVAL_CHILD_ENV_NAMES,
    EVAL_CHILD_GID,
    EVAL_CHILD_UID,
    bash_executor,
)

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

_CTX = TraceContext.new_trace()


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


def test_cloud_bash_behaviour_is_unchanged() -> None:
    """AC-4 and instrument check: production bash still inherits the environment.

    If this fails, the leak command cannot see credentials at all, and every eval
    assertion about its output would pass vacuously.
    """
    result = _run_gateway_standin("cloud")
    assert result["bash_registered"] is True
    output = _output(result, "leak")
    assert all(value in output for value in _SEEDED.values())
    ok = result["ok"]
    assert isinstance(ok, dict)
    assert ok["stdout"] == "ok\n"


@pytest.mark.skipif(os.geteuid() == 0, reason="the refusal path needs a non-root gateway")
def test_eval_bash_without_root_refuses_and_runs_nothing() -> None:
    """Fail closed: a non-root eval gateway cannot drop privileges, so bash refuses."""
    result = _run_gateway_standin("eval")
    assert result["bash_registered"] is True
    for key in ("leak", "ok"):
        call = result[key]
        assert isinstance(call, dict)
        assert call["success"] is False
        assert call["error"] == "credential_isolation_unavailable"
    serialized = json.dumps(result)
    assert not [name for name, value in _SEEDED.items() if value in serialized]


def _mock_proc() -> MagicMock:
    """Build a mock asyncio.Process that exits 0 with no output."""
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


@pytest.mark.asyncio
async def test_eval_spawn_drops_to_nobody_with_allowlisted_env() -> None:
    """Wiring (mocked spawn): on eval as root the child gets nobody and no credential.

    The kernel outcome for these arguments is proven in the gateway image by
    scripts/eval/probe_bash_credential_isolation.py.
    """
    with (
        patch.dict(os.environ, _SEEDED),
        patch("personal_agent.tools.primitives.bash.settings") as ms,
        patch("personal_agent.tools.primitives.bash.os.geteuid", return_value=0),
        patch(
            "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_mock_proc()),
        ) as mock_exec,
    ):
        ms.deployment_profile = "eval"
        result = await bash_executor("echo ok", ctx=_CTX)

    assert result["success"] is True
    assert EVAL_CHILD_UID == EVAL_CHILD_GID == 65534
    assert mock_exec.call_args.args == (
        "/usr/bin/setpriv",
        "--reuid=65534",
        "--regid=65534",
        "--clear-groups",
        "--inh-caps=-all",
        "--ambient-caps=-all",
        "--bounding-set=-all",
        "--no-new-privs",
        "--",
        "/bin/bash",
        "-o",
        "pipefail",
        "-c",
        "echo ok",
    )
    kwargs = mock_exec.call_args.kwargs
    # uvloop rejects these kwargs (FRE-1518); the identity change is setpriv's job.
    assert not {"user", "group", "extra_groups", "preexec_fn"} & set(kwargs)
    env = kwargs["env"]
    assert set(env) <= EVAL_CHILD_ENV_NAMES | {"HOME", "TMPDIR"}
    assert not [value for value in _SEEDED.values() if value in env.values()]


@pytest.mark.asyncio
@pytest.mark.parametrize("profile", ["cloud", "local"])
async def test_production_spawn_keeps_identity_and_environment(profile: str) -> None:
    """AC-4 (mocked spawn): production bash inherits the gateway's user and environment."""
    with (
        patch("personal_agent.tools.primitives.bash.settings") as ms,
        patch(
            "personal_agent.tools.primitives.bash.asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=_mock_proc()),
        ) as mock_exec,
    ):
        ms.deployment_profile = profile
        await bash_executor("echo ok", ctx=_CTX)

    assert mock_exec.call_args.args == ("/bin/bash", "-o", "pipefail", "-c", "echo ok")
    kwargs = mock_exec.call_args.kwargs
    assert kwargs["env"] is None
    assert not {"user", "group", "extra_groups", "preexec_fn"} & set(kwargs)


@pytest.mark.parametrize("profile", ["cloud", "local"])
def test_production_bash_runs_on_served_event_loop(profile: str) -> None:
    """FRE-1518: production bash runs on uvloop, the loop uvicorn serves the gateway on.

    uvloop rejects ``user``/``group``/``extra_groups`` even when they are None, so the
    FRE-1505 spawn broke bash on every profile, not only on eval.
    """
    uvloop = pytest.importorskip("uvloop")
    with patch("personal_agent.tools.primitives.bash.settings") as ms:
        ms.deployment_profile = profile
        result = uvloop.run(bash_executor("echo ok", ctx=_CTX))

    assert result["success"] is True
    assert result["stdout"] == "ok\n"


def test_eval_spawn_accepted_by_served_event_loop() -> None:
    """FRE-1518: the eval spawn runs on uvloop, the loop uvicorn serves the gateway on.

    uvloop raises ``ValueError`` for the ``user``/``group``/``extra_groups`` kwargs, and
    that error escaped ``bash_executor``. Here the spawn is real. As a non-root test
    process, setpriv cannot change uid, so the command fails, but bash_executor returns.
    """
    uvloop = pytest.importorskip("uvloop")
    with (
        patch("personal_agent.tools.primitives.bash.settings") as ms,
        patch("personal_agent.tools.primitives.bash.os.geteuid", return_value=0),
    ):
        ms.deployment_profile = "eval"
        result = uvloop.run(bash_executor("id -u", ctx=_CTX))

    assert isinstance(result, dict)
    if os.geteuid() == 0:
        assert result["stdout"] == f"{EVAL_CHILD_UID}\n"
    else:
        # setpriv itself ran and refused before exec, so bash never ran as the test user.
        assert result["exit_code"] != 0
        assert result["stdout"] == ""
        assert "setpriv:" in result["stderr"]
