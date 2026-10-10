"""Workers get the side-effecting tools under per-call approval (FRE-1565).

ADR-0150 D2 amendment of 2026-10-10 (FRE-1565). The owner's decisions: a new `operator`
worker type holds the side-effecting tools; each such call asks the owner, with its exact
arguments on the card; a worker's `bash` runs as `nobody` in its own workspace.

AC-1: a worker `bash` call reaches the owner-facing card, runs on approve, is refused on deny.
AC-2: the approval scope is per call, at fan-out scale (six workers).
AC-3: no silent exfiltration path (`test_worker_tool_split.py`).
AC-4: workspaces are separate — two workers that write the same file name keep two files.
AC-5: no duplicate side effects — two identical Linear issue requests make one issue.
AC-6: a turn with no PWA client still denies.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.config import settings
from personal_agent.orchestrator.sub_agent import run_sub_agent
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.telemetry.trace import TraceContext
from personal_agent.tools.primitives import bash as bash_mod
from personal_agent.tools.primitives.worker_workspace import (
    confine_to_workspace,
    get_worker_workspace,
    reset_worker_workspace,
    set_worker_workspace,
    worker_workspace_path,
)
from personal_agent.tools.primitives.write import write_executor

_SUB_AGENT = "personal_agent.orchestrator.sub_agent"


def _llm_response(content: str, tool_calls: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "role": "assistant",
        "content": content,
        "tool_calls": tool_calls or [],
        "usage": {},
        "response_id": None,
        "raw": {},
    }


def _calls_then_answers(*calls: tuple[str, str]) -> AsyncMock:
    """A client that makes each ``(tool, json_arguments)`` call in its own round, then answers."""
    client = AsyncMock()
    client.respond = AsyncMock(
        side_effect=[
            *(
                _llm_response("", tool_calls=[{"id": f"c{i}", "name": name, "arguments": args}])
                for i, (name, args) in enumerate(calls)
            ),
            _llm_response("final answer"),
        ]
    )
    return client


def _spec(tools: list[str], task: str = "test task") -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[],
        max_tokens=1024,
        timeout_seconds=300.0,
        tools=tools,
    )


def _stub_tool_layer(*tool_names: str) -> MagicMock:
    layer = MagicMock()
    layer.registry.get_tool_definitions_for_llm.return_value = [
        {"type": "function", "function": {"name": n, "description": "d", "parameters": {}}}
        for n in tool_names
    ]
    return layer


def _dispatch_result(tool_call_id: str, tool_name: str, content: str) -> dict[str, Any]:
    return {
        "tool_call_id": tool_call_id,
        "tool_name": tool_name,
        "content": content,
        "success": True,
        "latency_ms": 1.0,
        "output_hash": "h",
        "gate_result": None,
        "args_hash": "",
        "loop_policy": None,
        "tool_layer_output": None,
        "tool_layer_error": None,
    }


@pytest.fixture
def workspace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "workers"
    monkeypatch.setattr(settings, "worker_workspace_root", str(root))
    return root


@pytest.fixture
def a_workspace(workspace_root: Path) -> Iterator[Path]:
    path = worker_workspace_path("trace-1", "task-1")
    token = set_worker_workspace(path)
    try:
        yield path
    finally:
        reset_worker_workspace(token)


_CTX = TraceContext.new_trace()


# --------------------------------------------------------------------------------------
# AC-4 — workspaces are separate
# --------------------------------------------------------------------------------------


class TestWorkspacesAreSeparate:
    @pytest.mark.asyncio
    async def test_two_workers_that_write_the_same_name_keep_two_files(
        self, workspace_root: Path
    ) -> None:
        """AC-4. Two real workers write `out.txt` through the real `write` executor."""
        seen: list[Path | None] = []

        async def _real_write(**kwargs: Any) -> dict[str, Any]:
            seen.append(get_worker_workspace())
            args = kwargs["arguments"]
            out = await write_executor(path=args["path"], content=args["content"], ctx=_CTX)
            return _dispatch_result(kwargs["tool_call_id"], "write", str(out))

        with (
            patch(
                f"{_SUB_AGENT}.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer("write"),
            ),
            patch(
                f"{_SUB_AGENT}.resolve_sub_agent_approval_requirements",
                return_value=frozenset(),
            ),
            patch(f"{_SUB_AGENT}.dispatch_tool_call", side_effect=_real_write),
        ):
            for body in ("from worker A", "from worker B"):
                await run_sub_agent(
                    spec=_spec(["write"]),
                    llm_client=_calls_then_answers(
                        ("write", f'{{"path": "out.txt", "content": "{body}"}}')
                    ),
                    trace_id="trace-1",
                )

        assert len(seen) == 2
        assert seen[0] is not None and seen[1] is not None
        assert seen[0] != seen[1]
        assert (seen[0] / "out.txt").read_text() == "from worker A"
        assert (seen[1] / "out.txt").read_text() == "from worker B"
        assert sorted(p.read_text() for p in workspace_root.rglob("out.txt")) == [
            "from worker A",
            "from worker B",
        ]

    @pytest.mark.asyncio
    async def test_the_primary_never_sees_a_workspace(self, workspace_root: Path) -> None:
        with (
            patch(
                f"{_SUB_AGENT}.get_shared_tool_execution_layer",
                return_value=_stub_tool_layer(),
            ),
        ):
            await run_sub_agent(
                spec=_spec([]),
                llm_client=_calls_then_answers(),
                trace_id="trace-1",
            )
        assert get_worker_workspace() is None

    def test_a_workspace_is_keyed_by_turn_and_worker(self, workspace_root: Path) -> None:
        assert worker_workspace_path("t", "a") == workspace_root / "t" / "a"
        assert worker_workspace_path("t", "a") != worker_workspace_path("t", "b")
        assert worker_workspace_path("t", "a") != worker_workspace_path("u", "a")

    def test_an_identifier_cannot_climb_out_of_the_root(self, workspace_root: Path) -> None:
        path = worker_workspace_path("..", "../../etc")
        assert workspace_root in path.parents


class TestWriteIsConfined:
    @pytest.mark.asyncio
    async def test_a_relative_path_lands_inside_the_workspace(self, a_workspace: Path) -> None:
        out = await write_executor(path="sub/notes.md", content="x", ctx=_CTX)
        assert out["success"] is True
        assert (a_workspace / "sub" / "notes.md").read_text() == "x"

    @pytest.mark.asyncio
    @pytest.mark.parametrize("path", ["../escape.txt", "ABSOLUTE", "~/fre1565-escape.txt"])
    async def test_a_path_outside_the_workspace_is_refused(
        self, a_workspace: Path, workspace_root: Path, path: str
    ) -> None:
        absolute = workspace_root.parent / "escape.txt"
        out = await write_executor(
            path=str(absolute) if path == "ABSOLUTE" else path, content="x", ctx=_CTX
        )
        assert out["success"] is False
        assert out["error"] == "outside_worker_workspace"
        assert not absolute.exists()

    @pytest.mark.asyncio
    async def test_a_symlink_out_of_the_workspace_is_refused(
        self, a_workspace: Path, tmp_path: Path
    ) -> None:
        outside = tmp_path / "outside"
        outside.mkdir()
        a_workspace.mkdir(parents=True)
        (a_workspace / "link").symlink_to(outside)
        out = await write_executor(path="link/x.txt", content="x", ctx=_CTX)
        assert out["error"] == "outside_worker_workspace"
        assert not (outside / "x.txt").exists()

    def test_confine_is_none_for_the_workspace_itself(self, tmp_path: Path) -> None:
        assert confine_to_workspace(".", tmp_path) is None
        assert confine_to_workspace("a/b", tmp_path) == (tmp_path / "a" / "b").resolve()

    @pytest.mark.asyncio
    async def test_the_dispatch_boundary_moves_a_relative_path_before_the_layer_check(
        self, a_workspace: Path
    ) -> None:
        """Codex review: the layer's allowed_paths check refuses a relative path.

        So the sub-agent dispatch must hand the layer the absolute workspace path.
        """
        from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call

        layer = MagicMock()
        layer.governance_config = MagicMock(sub_agent_tools={})
        layer.registry.get_tool.return_value = None
        layer.execute_tool = AsyncMock(
            return_value=MagicMock(success=True, output={"ok": True}, error=None, metadata={})
        )
        await dispatch_tool_call(
            tool_call_id="c0",
            tool_name="write",
            arguments={"path": "out.txt", "content": "x"},
            tool_layer=layer,
            trace_ctx=_CTX,
            trace_id="t",
            session_id=None,
            loaded_skills=set(),
            principal="sub_agent",
        )
        sent = layer.execute_tool.await_args.args[1]
        assert sent["path"] == str((a_workspace / "out.txt").resolve())

    @pytest.mark.asyncio
    async def test_the_primary_write_path_is_unchanged(self, workspace_root: Path) -> None:
        """Seeded negative: with no workspace set, a relative path keeps the old rule."""
        out = await write_executor(path="out.txt", content="x", ctx=_CTX)
        assert out.get("error") != "outside_worker_workspace"


# --------------------------------------------------------------------------------------
# A worker's bash: the FRE-1505 drop, its own workspace, its own process group
# --------------------------------------------------------------------------------------


class _FakeProc:
    pid = 4242
    returncode = 0

    async def communicate(self) -> tuple[bytes, bytes]:
        return b"ok\n", b""


class TestWorkerBash:
    @pytest.mark.asyncio
    async def test_a_worker_shell_drops_to_nobody_in_its_workspace(
        self, a_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Spawn arguments only: CI is unprivileged (see test_eval_credential_isolation.py)."""
        monkeypatch.setattr(os, "geteuid", lambda: 0)
        monkeypatch.setattr(os, "chown", lambda *_a, **_k: None)
        monkeypatch.setenv("AGENT_FAKE_SECRET_FRE1565", "seeded-secret")
        spawn = AsyncMock(return_value=_FakeProc())
        killed: list[int] = []
        monkeypatch.setattr(bash_mod.asyncio, "create_subprocess_exec", spawn)
        monkeypatch.setattr(bash_mod.os, "killpg", lambda pgid, _sig: killed.append(pgid))

        out = await bash_mod.bash_executor(command="echo ok", ctx=_CTX)

        assert out["success"] is True
        argv = spawn.await_args.args
        kwargs = spawn.await_args.kwargs
        assert tuple(argv[: len(bash_mod.EVAL_CHILD_SETPRIV_ARGV)]) == (
            bash_mod.EVAL_CHILD_SETPRIV_ARGV
        )
        assert kwargs["cwd"] == a_workspace
        assert kwargs["env"]["HOME"] == str(a_workspace)
        assert "AGENT_FAKE_SECRET_FRE1565" not in kwargs["env"]
        assert set(kwargs["env"]) <= bash_mod.EVAL_CHILD_ENV_NAMES | {"HOME", "TMPDIR"}
        assert kwargs["start_new_session"] is True
        assert killed == [4242], "the worker shell's process group must be killed at the end"
        assert a_workspace.is_dir()

    @pytest.mark.asyncio
    async def test_a_non_root_gateway_refuses_worker_bash(
        self, a_workspace: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(os, "geteuid", lambda: 1000)
        spawn = AsyncMock()
        monkeypatch.setattr(bash_mod.asyncio, "create_subprocess_exec", spawn)
        out = await bash_mod.bash_executor(command="echo ok", ctx=_CTX)
        assert out["error"] == "credential_isolation_unavailable"
        spawn.assert_not_called()

    @pytest.mark.asyncio
    async def test_the_primary_shell_is_unchanged(
        self, workspace_root: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Seeded negative: no workspace → no drop, no cwd, no new session."""
        monkeypatch.setattr(settings, "deployment_profile", "cloud")
        spawn = AsyncMock(return_value=_FakeProc())
        monkeypatch.setattr(bash_mod.asyncio, "create_subprocess_exec", spawn)
        await bash_mod.bash_executor(command="echo ok", ctx=_CTX)
        kwargs = spawn.await_args.kwargs
        assert spawn.await_args.args[0] == "/bin/bash"
        assert kwargs["env"] is None
        assert kwargs["cwd"] is None
        assert kwargs["start_new_session"] is False
