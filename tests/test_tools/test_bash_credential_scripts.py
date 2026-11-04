"""On ``cloud``, a plain call of a reviewed script gets its credential. No other form does (FRE-1732).

The spawn is mocked, because the root outcome needs a root parent. That outcome is in
``scripts/eval/probe_bash_credential_isolation.py``, in the gateway image. The first two tests
also run a real child with the account change removed, so CI shows what the environment holds.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives import bash as bash_module
from personal_agent.tools.primitives._governance import (
    PathRefusalError,
    _check_path_governance,
)
from personal_agent.tools.primitives.bash import CHILD_SETPRIV_ARGV, bash_executor

_CTX = TraceContext.new_trace()
_CYPHER = "skills/graph-inspection/scripts/cypher.py"
_PROBE = "skills/service-health/scripts/probe_services.py"
_SEEDED = {
    "AGENT_NEO4J_PASSWORD": "seeded-neo4j-password-5c1e",
    "AGENT_DATABASE_URL": "postgresql://agent:seeded-db-password-77ab@postgres/db",
    "AGENT_ANTHROPIC_API_KEY": "seeded-anthropic-key-90d2",
}


def _mock_proc() -> MagicMock:
    proc = MagicMock()
    proc.returncode = 0
    proc.communicate = AsyncMock(return_value=(b"", b""))
    return proc


async def _spawn(command: str, profile: str = "cloud") -> tuple[tuple[str, ...], dict[str, object]]:
    """Run ``command`` as a root gateway with a mocked spawn, and return its arguments."""
    with (
        patch.dict(os.environ, _SEEDED),
        patch.object(bash_module, "settings") as settings,
        patch.object(bash_module.os, "geteuid", return_value=0),
        patch.object(
            bash_module.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(return_value=_mock_proc()),
        ) as spawn,
    ):
        settings.deployment_profile = profile
        await bash_executor(command, ctx=_CTX)
    return spawn.call_args.args, spawn.call_args.kwargs


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cat /proc/self/environ", "jq -n env"])
async def test_the_child_environment_holds_no_seeded_value(command: str) -> None:
    """A real child, with only the account change removed: the value is absent on ``cloud``."""
    with (
        patch.dict(os.environ, _SEEDED),
        patch.object(bash_module, "settings") as settings,
        patch.object(bash_module.os, "geteuid", return_value=0),
        patch.object(bash_module, "CHILD_SETPRIV_ARGV", ()),
    ):
        settings.deployment_profile = "cloud"
        result = await bash_executor(command, ctx=_CTX)

    assert result["exit_code"] == 0
    for value in _SEEDED.values():
        assert value not in f"{result['stdout']}{result['stderr']}"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["cat /proc/self/environ", "jq -n env"])
async def test_seeded_the_same_command_shows_the_value_with_no_isolation(command: str) -> None:
    """Seeded negative: on ``local``, the same command returns the value, so the detector works."""
    with (
        patch.dict(os.environ, _SEEDED),
        patch.object(bash_module, "settings") as settings,
    ):
        settings.deployment_profile = "local"
        result = await bash_executor(command, ctx=_CTX)

    assert _SEEDED["AGENT_NEO4J_PASSWORD"] in f"{result['stdout']}{result['stderr']}"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("script", "secret"),
    [(_CYPHER, "AGENT_NEO4J_PASSWORD"), (_PROBE, "AGENT_DATABASE_URL")],
)
@pytest.mark.parametrize("prefix", ["", "/app/"])
async def test_a_plain_script_call_gets_its_credential_and_no_other(
    script: str, secret: str, prefix: str
) -> None:
    """No shell, a fixed interpreter and script, and only the names that this script reads."""
    argv, kwargs = await _spawn(f"python3 {prefix}{script} 'arg one'")

    assert argv == (sys.executable, "-I", f"/app/{script}", "arg one")
    assert kwargs["cwd"] == "/app"
    env = kwargs["env"]
    assert isinstance(env, dict)
    assert env[secret] == _SEEDED[secret]
    assert "AGENT_ANTHROPIC_API_KEY" not in env
    assert "HOME" not in env
    assert "TMPDIR" not in env
    assert "VIRTUAL_ENV" not in env
    assert env["PATH"] == bash_module._SCRIPT_CHILD_PATH


@pytest.mark.asyncio
@pytest.mark.parametrize("suffix", ["2>&1", " 2>&1", "2>/dev/null", ">/dev/null"])
async def test_a_redirection_that_stays_plain_has_no_effect_on_the_script_call(
    suffix: str,
) -> None:
    """These forms stay one plain call. No shell runs, so the words carry no redirection."""
    argv, _ = await _spawn(f"python3 {_CYPHER} q {suffix}")

    assert argv == (sys.executable, "-I", f"/app/{_CYPHER}", "q")


@pytest.mark.parametrize("tool", ["write", "edit"])
def test_the_real_path_rules_keep_root_tools_out_of_the_child_home(tool: str) -> None:
    """curl, jq, git and psql read start-up files from HOME, and ``write`` needs no card."""
    home = Path(bash_module.child_env()["HOME"])

    with pytest.raises(PathRefusalError):
        _check_path_governance(home / ".curlrc", tool_name=tool)


def test_seeded_the_real_path_rules_allow_tmp() -> None:
    """Seeded negative: ``/tmp`` passes the same rules, so HOME must not be there."""
    _check_path_governance(Path("/tmp/.curlrc"), tool_name="write")


def test_the_image_creates_the_child_home_for_the_child_under_a_root_parent() -> None:
    """The child owns HOME, and it cannot replace it with a link, because root owns the parent."""
    env = bash_module.child_env()
    dockerfile = (Path(__file__).parents[2] / "Dockerfile.gateway").read_text()

    assert f"install -d -o nobody -g nogroup -m 700 {env['HOME']}" in dockerfile
    assert env["HOME"].startswith("/var/lib/")
    assert env["TMPDIR"] == "/tmp"
    assert not {"CURL_HOME", "XDG_CONFIG_HOME"} & set(env)


@pytest.mark.asyncio
async def test_each_script_gets_only_its_own_names() -> None:
    """The probe gets no Neo4j password, and cypher gets no database URL."""
    _, cypher_kwargs = await _spawn(f"python3 {_CYPHER} q")
    _, probe_kwargs = await _spawn(f"python3 {_PROBE} all")

    assert "AGENT_DATABASE_URL" not in cypher_kwargs["env"]
    assert "AGENT_NEO4J_PASSWORD" not in probe_kwargs["env"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "command",
    [
        f"python3 {_CYPHER} q | jq -n env",
        f"jq -n env | python3 {_CYPHER} q",
        f"python3 {_CYPHER} q && jq -n env",
        f"python3 {_CYPHER} q; jq -n env",
        f"python3 {_CYPHER} q > /tmp/out",
        f"python3 {_CYPHER} q &",
        f"X=1 python3 {_CYPHER} q",
        f"PATH=/tmp python3 {_CYPHER} q",
        f"python3 {_CYPHER} $AGENT_NEO4J_PASSWORD",
        f"python3 {_CYPHER} $(jq -n env)",
        f"(python3 {_CYPHER} q)",
        f"python3 -c 1 {_CYPHER}",
        f"python3 ./{_CYPHER} q",
        f"python3 skills/../{_CYPHER} q",
        f"python3 /tmp/{_CYPHER} q",
        f"python {_CYPHER} q",
        f"/tmp/python3 {_CYPHER} q",
        f"cd /tmp\npython3 {_CYPHER} q",
        "python3 skills/log-search/scripts/es_index_granularity.py",
        "python3 skills/mermaid-diagrams/scripts/validate_mermaid.py /tmp/a.mmd",
    ],
)
async def test_any_other_form_runs_as_nobody_with_no_credential(command: str) -> None:
    """Only the one plain form skips the account change."""
    argv, kwargs = await _spawn(command)

    assert argv == (*CHILD_SETPRIV_ARGV, "/bin/bash", "-o", "pipefail", "-c", command)
    env = kwargs["env"]
    assert isinstance(env, dict)
    for name in _SEEDED:
        assert name not in env


@pytest.mark.asyncio
@pytest.mark.parametrize("script", [_CYPHER, _PROBE])
async def test_eval_keeps_nobody_for_every_command(script: str) -> None:
    """``eval`` gets no exception (FRE-1505)."""
    command = f"python3 {script} x"

    argv, kwargs = await _spawn(command, profile="eval")

    assert argv == (*CHILD_SETPRIV_ARGV, "/bin/bash", "-o", "pipefail", "-c", command)
    assert not set(_SEEDED) & set(kwargs["env"])  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_an_exec_failure_of_a_script_is_a_tool_error_with_no_value() -> None:
    """The spawn error path holds no credential."""
    with (
        patch.dict(os.environ, _SEEDED),
        patch.object(bash_module, "settings") as settings,
        patch.object(bash_module.os, "geteuid", return_value=0),
        patch.object(
            bash_module.asyncio,
            "create_subprocess_exec",
            new=AsyncMock(side_effect=OSError("exec failed")),
        ),
        pytest.raises(bash_module.BashError) as refused,
    ):
        settings.deployment_profile = "cloud"
        await bash_executor(f"python3 {_CYPHER} q", ctx=_CTX)

    for value in _SEEDED.values():
        assert value not in str(refused.value)
