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


# --------------------------------------------------------------------------------------
# The per-call broker (AC-1, AC-2, AC-5, AC-6)
# --------------------------------------------------------------------------------------

import asyncio  # noqa: E402

import personal_agent.orchestrator.executor as ex  # noqa: E402
from personal_agent.governance.models import Mode  # noqa: E402
from personal_agent.orchestrator.channels import Channel  # noqa: E402
from personal_agent.orchestrator.constraint_options import ConstraintDecision  # noqa: E402
from personal_agent.orchestrator.sub_agent_approval import (  # noqa: E402
    APPROVE_ACTION_ID,
    DENY_ACTION_ID,
    PER_CALL_CARD_MAX_ARGUMENT_CHARS,
    SUB_AGENT_APPROVAL_CONSTRAINT,
    SubAgentApprovalBroker,
    reset_sub_agent_approval_broker,
    set_sub_agent_approval_broker,
)
from personal_agent.orchestrator.types import ExecutionContext  # noqa: E402
from personal_agent.orchestrator.worker_types import WorkerType  # noqa: E402

_TRANSPORT = "personal_agent.transport.agui.transport"


def _ctx() -> ExecutionContext:
    return ExecutionContext(
        session_id="s1",
        trace_id="t1",
        user_message="hi",
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )


def _operator_spec(tools: list[str], task: str = "operate") -> SubAgentSpec:
    return SubAgentSpec(
        task=task,
        context=[],
        max_tokens=1024,
        timeout_seconds=300.0,
        tools=tools,
        worker_type=WorkerType.OPERATOR,
    )


@pytest.fixture
def broker() -> Iterator[SubAgentApprovalBroker]:
    b = SubAgentApprovalBroker(_ctx())
    token = set_sub_agent_approval_broker(b)
    try:
        yield b
    finally:
        reset_sub_agent_approval_broker(token)


class _Cards:
    """A fake pause that records each card's text and answers with a fixed decision."""

    def __init__(self, action: str = APPROVE_ACTION_ID, resolution: str = "user_choice") -> None:
        self.contexts: list[str] = []
        self._action = action
        self._resolution = resolution

    async def __call__(self, **kwargs: object) -> ConstraintDecision:
        self.contexts.append(str(kwargs["context"]))
        return ConstraintDecision(self._action, self._resolution)


def _patched_loop(dispatch: AsyncMock, *tools: str) -> Any:
    """Patch the layer and dispatch; the approval sets come from the REAL tools.yaml."""
    from contextlib import ExitStack

    stack = ExitStack()
    stack.enter_context(
        patch(f"{_SUB_AGENT}.get_shared_tool_execution_layer", return_value=_stub_tool_layer(*tools))
    )
    stack.enter_context(patch(f"{_SUB_AGENT}.dispatch_tool_call", dispatch))
    stack.enter_context(
        patch(
            "personal_agent.orchestrator.sub_agent_approval.get_current_mode",
            return_value=Mode.NORMAL,
        )
    )
    return stack


def _ok_dispatch(content: str = "done") -> AsyncMock:
    return AsyncMock(
        side_effect=lambda **kw: _dispatch_result(kw["tool_call_id"], kw["tool_name"], content)
    )


class TestASideEffectingCallAsksTheOwner:
    """AC-1."""

    @pytest.mark.asyncio
    async def test_the_card_reaches_the_transport_with_the_exact_command(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        """Drives the REAL `_maybe_pause_for_constraint`; only the socket push is faked."""
        pushed: list[Any] = []

        async def fake_load(user_id: object, constraint: str, **_kw: object) -> None:
            return None

        async def fake_push(**kwargs: object) -> dict[str, str]:
            pushed.append(kwargs["event"])
            return {"decision": APPROVE_ACTION_ID, "resolution": "user_choice"}

        async def fake_emit(**_kw: object) -> None:
            return None

        monkeypatch.setattr(ex, "_load_constraint_preference", fake_load)
        monkeypatch.setattr(f"{_TRANSPORT}.register_and_push_constraint", fake_push)
        monkeypatch.setattr(f"{_TRANSPORT}.emit_constraint_resolved", fake_emit)
        dispatch = _ok_dispatch("Linux")

        with _patched_loop(dispatch, "bash"):
            result = await run_sub_agent(
                spec=_operator_spec(["bash"]),
                llm_client=_calls_then_answers(("bash", '{"command": "uname -a"}')),
                trace_id="t",
            )

        assert len(pushed) == 1
        event = pushed[0]
        assert event.constraint == SUB_AGENT_APPROVAL_CONSTRAINT
        assert event.default_option == DENY_ACTION_ID
        assert 'operator worker wants to run bash with these exact arguments: {"command":"uname -a"}' in event.context
        assert "this one call only" in event.context
        assert dispatch.call_count == 1
        assert dispatch.await_args.kwargs["approved_upstream"] is True
        assert result.tools_used == ["bash"]

    @pytest.mark.asyncio
    async def test_deny_refuses_the_call_and_the_worker_still_finishes(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _Cards(DENY_ACTION_ID))
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, "bash"):
            result = await run_sub_agent(
                spec=_operator_spec(["bash"]),
                llm_client=_calls_then_answers(("bash", '{"command": "uname -a"}')),
                trace_id="t",
            )
        dispatch.assert_not_called()
        assert result.success is True
        assert result.tools_used == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        ("tool", "args"),
        [
            ("bash", '{"command": "ls"}'),
            ("create_linear_issue", '{"title": "t", "description": "d"}'),
            ("create_linear_project", '{"name": "p"}'),
            ("notes_write", '{"slug": "s", "content": "c"}'),
            ("artifact_write", '{"title": "a", "content": "c"}'),
        ],
    )
    @pytest.mark.parametrize("action", [APPROVE_ACTION_ID, DENY_ACTION_ID])
    async def test_every_per_call_grant_asks_and_obeys(
        self,
        monkeypatch: pytest.MonkeyPatch,
        broker: SubAgentApprovalBroker,
        tool: str,
        args: str,
        action: str,
    ) -> None:
        """Every `per_call` grant in the real config asks, and the answer decides."""
        cards = _Cards(action)
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, tool):
            await run_sub_agent(
                spec=_operator_spec([tool]),
                llm_client=_calls_then_answers((tool, args)),
                trace_id="t",
            )
        assert len(cards.contexts) == 1
        assert f"run {tool} with these exact arguments" in cards.contexts[0]
        assert dispatch.call_count == (1 if action == APPROVE_ACTION_ID else 0)


class TestTheApprovalScopeIsPerCall:
    """AC-2 — at fan-out scale (FRE-1461 AC-4 shape: six workers)."""

    @pytest.mark.asyncio
    async def test_six_workers_with_two_distinct_bash_calls_raise_twelve_cards(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, "bash"):
            for i in range(6):
                await run_sub_agent(
                    spec=_operator_spec(["bash"], task=f"task {i}"),
                    llm_client=_calls_then_answers(
                        ("bash", f'{{"command": "echo {i}-a"}}'),
                        ("bash", f'{{"command": "echo {i}-b"}}'),
                    ),
                    trace_id="t",
                )
        assert len(cards.contexts) == 12, "one approval must cover exactly one call"
        assert dispatch.call_count == 12

    @pytest.mark.asyncio
    async def test_contrast_a_policy_tool_still_asks_once_per_turn(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        """The FRE-1461 scope is unchanged for a tool that is not `per_call`."""
        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch()
        with (
            _patched_loop(dispatch, "run_python"),
            patch(
                f"{_SUB_AGENT}.resolve_sub_agent_approval_requirements",
                return_value=frozenset({"run_python"}),
            ),
        ):
            for i in range(6):
                await run_sub_agent(
                    spec=_operator_spec(["run_python"], task=f"task {i}"),
                    llm_client=_calls_then_answers(
                        ("run_python", f'{{"code": "{i}"}}'),
                        ("run_python", f'{{"code": "{i}+1"}}'),
                    ),
                    trace_id="t",
                )
        assert len(cards.contexts) == 1
        assert dispatch.call_count == 12


class TestNoDuplicateSideEffects:
    """AC-5."""

    _ISSUE = '{"title": "Fix the cache", "description": "It is stale."}'

    @pytest.mark.asyncio
    async def test_two_workers_asking_for_the_same_issue_make_one(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch('{"identifier": "FRE-9999"}')
        clients = [_calls_then_answers(("create_linear_issue", self._ISSUE)) for _ in range(2)]
        with _patched_loop(dispatch, "create_linear_issue"):
            results = [
                await run_sub_agent(
                    spec=_operator_spec(["create_linear_issue"], task=f"file it {i}"),
                    llm_client=clients[i],
                    trace_id="t",
                )
                for i in range(2)
            ]
        assert dispatch.call_count == 1, "two identical issues must not be created"
        assert len(cards.contexts) == 1
        assert results[0].tools_used == ["create_linear_issue"]
        assert results[1].tools_used == []
        second_messages = clients[1].respond.await_args_list[-1].kwargs["messages"]
        tool_message = next(m for m in second_messages if m.get("role") == "tool")
        assert "A sibling worker already made this identical create_linear_issue call" in (
            tool_message["content"]
        )
        assert "FRE-9999" in tool_message["content"]

    @pytest.mark.asyncio
    async def test_two_concurrent_workers_still_make_one(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _Cards())
        release = asyncio.Event()
        calls = {"n": 0}

        async def _slow_dispatch(**kw: Any) -> dict[str, Any]:
            calls["n"] += 1
            await release.wait()
            return _dispatch_result(kw["tool_call_id"], kw["tool_name"], "FRE-9999")

        async def _both() -> None:
            await asyncio.gather(
                *(
                    run_sub_agent(
                        spec=_operator_spec(["create_linear_issue"]),
                        llm_client=_calls_then_answers(("create_linear_issue", self._ISSUE)),
                        trace_id="t",
                    )
                    for _ in range(2)
                )
            )

        with _patched_loop(AsyncMock(side_effect=_slow_dispatch), "create_linear_issue"):
            task = asyncio.create_task(_both())
            await asyncio.sleep(0.05)
            release.set()
            await task
        assert calls["n"] == 1

    @pytest.mark.asyncio
    async def test_seeded_negative_different_titles_are_two_calls(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, "create_linear_issue"):
            for title in ("One", "Two"):
                await run_sub_agent(
                    spec=_operator_spec(["create_linear_issue"]),
                    llm_client=_calls_then_answers(
                        ("create_linear_issue", f'{{"title": "{title}"}}')
                    ),
                    trace_id="t",
                )
        assert len(cards.contexts) == 2
        assert dispatch.call_count == 2


class TestHarnessTurnsStillDeny:
    """AC-6 — ADR-0063 A2: a turn with no PWA client denies."""

    @pytest.mark.asyncio
    async def test_no_broker_in_scope_refuses(self) -> None:
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, "bash"):
            result = await run_sub_agent(
                spec=_operator_spec(["bash"]),
                llm_client=_calls_then_answers(("bash", '{"command": "ls"}')),
                trace_id="t",
            )
        dispatch.assert_not_called()
        assert result.success is True

    @pytest.mark.asyncio
    async def test_connection_lost_refuses_and_stops_asking_for_the_turn(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        cards = _Cards(DENY_ACTION_ID, "connection_lost")
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        dispatch = _ok_dispatch()
        with _patched_loop(dispatch, "bash"):
            await run_sub_agent(
                spec=_operator_spec(["bash"]),
                llm_client=_calls_then_answers(
                    ("bash", '{"command": "ls"}'), ("bash", '{"command": "pwd"}')
                ),
                trace_id="t",
            )
        dispatch.assert_not_called()
        assert len(cards.contexts) == 1, "a socket-less turn must not wait once per call"

    @pytest.mark.asyncio
    async def test_a_real_answer_does_not_stop_the_asking(
        self, monkeypatch: pytest.MonkeyPatch, broker: SubAgentApprovalBroker
    ) -> None:
        """Seeded negative: an owner's own "deny" is not "unreachable"."""
        cards = _Cards(DENY_ACTION_ID, "user_choice")
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        with _patched_loop(_ok_dispatch(), "bash"):
            await run_sub_agent(
                spec=_operator_spec(["bash"]),
                llm_client=_calls_then_answers(
                    ("bash", '{"command": "ls"}'), ("bash", '{"command": "pwd"}')
                ),
                trace_id="t",
            )
        assert len(cards.contexts) == 2


class TestTheBrokerEdges:
    @pytest.mark.asyncio
    async def test_arguments_too_long_for_a_card_are_refused_without_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        execute = AsyncMock(return_value="ran")
        outcome = await SubAgentApprovalBroker(_ctx()).run_once(
            "bash",
            {"command": "x" * PER_CALL_CARD_MAX_ARGUMENT_CHARS},
            worker_type="operator",
            task="t",
            deadline_monotonic=10_000_000_000.0,
            execute=execute,
        )
        assert outcome.approved is False
        assert outcome.reason == "arguments_too_long_for_card"
        assert cards.contexts == []
        execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_small_worker_budget_is_refused_without_a_card(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import time

        cards = _Cards()
        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", cards)
        outcome = await SubAgentApprovalBroker(_ctx()).run_once(
            "bash",
            {"command": "ls"},
            worker_type="operator",
            task="t",
            deadline_monotonic=time.monotonic() + 10.0,
            execute=AsyncMock(),
        )
        assert outcome.reason == "insufficient_worker_budget"
        assert cards.contexts == []

    @pytest.mark.asyncio
    async def test_a_leader_cancelled_during_the_card_releases_its_followers(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Codex review: the shared future must resolve even when the leader never ran."""
        started = asyncio.Event()

        async def _hanging_pause(**_kw: object) -> ConstraintDecision:
            started.set()
            await asyncio.Event().wait()
            raise AssertionError("unreachable")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _hanging_pause)
        b = SubAgentApprovalBroker(_ctx())
        execute = AsyncMock(return_value="ran")
        kwargs: dict[str, Any] = {
            "worker_type": "operator",
            "task": "t",
            "deadline_monotonic": 10_000_000_000.0,
            "execute": execute,
        }
        leader = asyncio.create_task(b.run_once("bash", {"command": "ls"}, **kwargs))
        await started.wait()
        follower = asyncio.create_task(b.run_once("bash", {"command": "ls"}, **kwargs))
        await asyncio.sleep(0)
        leader.cancel()
        with pytest.raises(asyncio.CancelledError):
            await leader
        outcome = await asyncio.wait_for(follower, timeout=1.0)
        assert outcome.approved is False
        assert outcome.reason == "cancelled_before_completion"
        assert outcome.coalesced is True
        execute.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_cancelled_follower_does_not_cancel_the_leader(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        release = asyncio.Event()

        async def _slow_pause(**_kw: object) -> ConstraintDecision:
            await release.wait()
            return ConstraintDecision(APPROVE_ACTION_ID, "user_choice")

        monkeypatch.setattr(ex, "_maybe_pause_for_constraint", _slow_pause)
        b = SubAgentApprovalBroker(_ctx())
        execute = AsyncMock(return_value="ran")
        kwargs: dict[str, Any] = {
            "worker_type": "operator",
            "task": "t",
            "deadline_monotonic": 10_000_000_000.0,
            "execute": execute,
        }
        leader = asyncio.create_task(b.run_once("bash", {"command": "ls"}, **kwargs))
        await asyncio.sleep(0)
        follower = asyncio.create_task(b.run_once("bash", {"command": "ls"}, **kwargs))
        await asyncio.sleep(0)
        follower.cancel()
        with pytest.raises(asyncio.CancelledError):
            await follower
        release.set()
        outcome = await leader
        assert outcome.approved is True
        assert outcome.executed is True
        execute.assert_awaited_once()
