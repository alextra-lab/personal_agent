"""FRE-1730: prepare one scratch run directory and run one real ``claude`` session in the jail."""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from scripts.eval.fre1730.egress import ANTHROPIC_API, EgressPolicy, serve_gate
from scripts.eval.fre1730.jail import (
    EGRESS_PORT,
    EGRESS_SOCKET_NAME,
    HOME_IN_JAIL,
    PLUGIN_IN_JAIL,
    Jail,
    scope_env,
    session_path,
    uv_python_dir,
    with_egress_relay,
)

_GIT_DEPTH = 20
_SESSION_ID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
#: Both arms lose the web. A page on the public repo holds the merged fix.
_DISALLOWED_TOOLS = ("WebFetch", "WebSearch")
#: The scrub below keeps credentials from Bash and from hooks, and it forces the default permission mode. So the tools
#: are named. With no one to answer a prompt, a tool that is not named would add noise to the score.
_ALLOWED_TOOLS = ("Bash", "Edit", "Write", "Read", "Glob", "Grep", "Agent", "Skill", "TodoWrite")
_SESSION_ENV = {
    "DISABLE_AUTOUPDATER": "1",
    "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB": "1",
    "UV_NO_SYNC": "1",
    "UV_OFFLINE": "1",
    "UV_CACHE_DIR": "/tmp/uv-cache",
    "UV_PYTHON_DOWNLOADS": "never",
    "PYTHONDONTWRITEBYTECODE": "1",
}


@dataclass(frozen=True)
class Route:
    """How a session reaches a model.

    Attributes:
        kind: ``stub``, ``max-plan`` or ``api-key``.
        env: The variables that the session receives.
    """

    kind: str
    env: Mapping[str, str]


def stub_route() -> Route:
    """Build the route to the stub, through the relay.

    Returns:
        The route.
    """
    return Route(
        "stub",
        {
            "ANTHROPIC_API_KEY": "fre1730-stub-unused",
            "ANTHROPIC_BASE_URL": f"http://127.0.0.1:{EGRESS_PORT}",
        },
    )


def paid_route(host_env: Mapping[str, str]) -> Route:
    """Build the paid route from variables that exist only for this run.

    The production key is not read. ``~/.claude/.credentials.json`` is not copied, because a session that refreshed a
    copy might end the login of every seat.

    Args:
        host_env: The caller's environment.

    Returns:
        The route.

    Raises:
        SystemExit: If not exactly one credential is set.
    """
    token = host_env.get("FRE1730_CLAUDE_OAUTH_TOKEN")
    key = host_env.get("FRE1730_ANTHROPIC_API_KEY")
    if bool(token) == bool(key):
        raise SystemExit("set exactly one: FRE1730_CLAUDE_OAUTH_TOKEN or FRE1730_ANTHROPIC_API_KEY")
    credential = {"CLAUDE_CODE_OAUTH_TOKEN": token} if token else {"ANTHROPIC_API_KEY": str(key)}
    return Route(
        "max-plan" if token else "api-key",
        {**credential, "HTTPS_PROXY": f"http://127.0.0.1:{EGRESS_PORT}"},
    )


def prepare_repo(source: Path, commit: str, target: Path) -> None:
    """Make a repo that holds the commit and its past only, with no remote.

    A worktree shares its refs with the seats and shows the merged fix. This copy has neither.

    Args:
        source: The source repo.
        commit: The commit.
        target: The new directory.
    """
    target.mkdir(parents=True)
    for args in (
        ("init", "-q"),
        ("fetch", "-q", f"--depth={_GIT_DEPTH}", f"file://{source}", commit),
        ("-c", "advice.detachedHead=false", "checkout", "-q", "--detach", "FETCH_HEAD"),
        ("config", "user.name", "replay"),
        ("config", "user.email", "replay@example.invalid"),
    ):
        subprocess.run(["git", "-C", str(target), *args], check=True, capture_output=True)


def prepare_home(home: Path, global_claude_md: Path | None) -> None:
    """Make a new ``~`` for one session, with the owner's global rules.

    Args:
        home: The new directory.
        global_claude_md: The global CLAUDE.md to copy.
    """
    (home / ".claude").mkdir(parents=True)
    if global_claude_md is not None:
        shutil.copyfile(global_claude_md, home / ".claude" / "CLAUDE.md")


@dataclass(frozen=True)
class SessionResult:
    """The end of one session.

    Attributes:
        session_id: The session's id, or an empty string.
        cost_usd: The cost that the session reports. ``None`` if it reported none.
        is_error: The session ended on an error.
        subtype: The result's subtype.
        api_error_status: The HTTP status of an API error.
        turns: The count of turns.
        duration_s: The session's time.
        refused: The targets that the egress gate refused.
        exit_code: The process's exit code.
        stderr_tail: The end of stderr.
    """

    session_id: str
    cost_usd: float | None
    is_error: bool
    subtype: str
    api_error_status: int | None
    turns: int
    duration_s: float
    refused: tuple[str, ...] = field(default=())
    exit_code: int = 0
    stderr_tail: str = ""


def _number(value: object) -> float | None:
    """Read a number from session output. A plugin can write to the session's stdout, so nothing is assumed.

    Args:
        value: The value.

    Returns:
        A finite number of zero or more, else ``None``.
    """
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value) if math.isfinite(value) and value >= 0 else None


def _parse_result(
    stdout: str, refused: tuple[str, ...], exit_code: int, stderr: str
) -> SessionResult:
    result: dict[str, object] = {}
    session_id = ""
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict):
            continue
        if isinstance(event.get("session_id"), str) and _SESSION_ID.fullmatch(event["session_id"]):
            session_id = event["session_id"]
        if event.get("type") == "result":
            result = event
    status = result.get("api_error_status")
    return SessionResult(
        session_id=session_id,
        cost_usd=_number(result.get("total_cost_usd")),
        is_error=bool(result.get("is_error", True)),
        subtype=str(result.get("subtype", "no-result")),
        api_error_status=status if isinstance(status, int) else None,
        turns=int(_number(result.get("num_turns")) or 0),
        duration_s=(_number(result.get("duration_ms")) or 0.0) / 1000,
        refused=refused,
        exit_code=exit_code,
        stderr_tail=stderr[-500:],
    )


def run_session(
    *,
    run_root: Path,
    prompt: str,
    model: str,
    route: Route,
    budget_usd: float,
    timeout_s: float,
    plugin: Path | None = None,
    venv: Path | None = None,
    stub_address: tuple[str, int] | None = None,
    extra_env: Mapping[str, str] | None = None,
    allowed_hosts: frozenset[tuple[str, int]] | None = None,
) -> SessionResult:
    """Run one session, with its only way out through the egress gate.

    Args:
        run_root: The run directory, which holds ``repo`` and ``home``.
        prompt: The prompt.
        model: The exact model id.
        route: The route to a model.
        budget_usd: What remains under the stop.
        timeout_s: The time limit.
        plugin: The plugin directory. ``None`` for the control arm.
        venv: The task's environment.
        stub_address: The stub's address on a stub run.
        extra_env: More environment variables.
        allowed_hosts: What ``CONNECT`` may reach. By default the Anthropic API, or nothing on a stub run.

    Returns:
        The result.
    """
    claude = shutil.which("claude")
    if claude is None:
        raise SystemExit("claude is not on PATH")
    command = [
        str(Path(claude).resolve()),
        "-p",
        prompt,
        "--model",
        model,
        "--output-format",
        "stream-json",
        "--verbose",
        "--strict-mcp-config",
        "--allowed-tools",
        ",".join(_ALLOWED_TOOLS),
        "--max-budget-usd",
        f"{budget_usd:.4f}",
        "--disallowed-tools",
        *_DISALLOWED_TOOLS,
    ]
    if plugin is not None:
        command += ["--plugin-dir", PLUGIN_IN_JAIL]
    env = {
        "PATH": session_path(),
        "HOME": HOME_IN_JAIL,
        "LANG": "C.UTF-8",
        "UV_PYTHON_INSTALL_DIR": uv_python_dir(),
        **scope_env(),
        **_SESSION_ENV,
        **route.env,
        **(extra_env or {}),
    }
    if allowed_hosts is None:
        allowed_hosts = frozenset() if stub_address is not None else frozenset({ANTHROPIC_API})
    policy = EgressPolicy(allowed=allowed_hosts, stub=stub_address)
    jail = Jail(run_root=run_root, venv=venv, plugin=plugin)
    with serve_gate(run_root / EGRESS_SOCKET_NAME, policy):
        done = subprocess.run(
            jail.argv(with_egress_relay(command)),
            env=env,
            capture_output=True,
            text=True,
            errors="replace",
            timeout=timeout_s,
            stdin=subprocess.DEVNULL,
            check=False,
        )
    return _parse_result(done.stdout, tuple(policy.refused), done.returncode, done.stderr)


def find_transcript(home: Path, session_id: str) -> Path | None:
    """Find a session's transcript under its ``~``. A link that leaves that ``~`` does not count.

    The session wrote this directory, and the host reads what this returns.

    Args:
        home: The session's home on the host.
        session_id: The session's id.

    Returns:
        The path, or ``None``.
    """
    if home.is_symlink() or not _SESSION_ID.fullmatch(session_id):
        return None
    found = sorted((home / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
    if not found or not found[0].is_file():
        return None
    return found[0] if found[0].resolve().is_relative_to(home.resolve()) else None
