"""The general worker's read-only file grant (FRE-1580 AC-3, ADR-0156 D9.4 and AC-9).

A ``general`` worker may ``read`` the files that its task names in ``read_paths``, and nothing
else. *Fails if* the worker cannot read the named file, or if any of these succeeds:

- a read of a file that the task does not name;
- a write or an edit of the named file;
- a read through ``../`` or a symlink that resolves to another file;
- a read after the named file is replaced by a symlink to another file;
- a read of a directory named in ``read_paths``;
- a read through a hard link to the named file at another path;
- a read of the named file after the task ends;
- a read after the named file is replaced by another regular file at the same path.

The rows that change a file between the grant and the read call the shared dispatch boundary
with a grant built for the task, because the grant is made at task start. The other rows run a
whole worker. The tool layer, the ``read`` tool, its path rules and governance are real.

Seeded negative: with ``ReadGrant.check_opened`` made a no-op, the rename-over row reads the
new file, so the second check is load-bearing (``test_seeded_negative_...``).
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest
from personal_agent.governance.worker_read_grant import ReadGrant

from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.tool_dispatch import dispatch_tool_call
from personal_agent.orchestrator.worker_types import WorkerType
from personal_agent.telemetry import TraceContext
from personal_agent.tools.executor import ToolExecutionLayer
from tests._helpers.worker_run import run_worker, shared_layer, worker_layer

_GENERAL_TOOLS = ["read", "read_skill", "run_python"]


@pytest.fixture
def files(tmp_path: Path) -> dict[str, Path]:
    """A named file, another file beside it, and a directory."""
    work = tmp_path / "agent_workspace"
    (work / "sub").mkdir(parents=True)
    named = work / "review_me.md"
    named.write_text("the named file\n", encoding="utf-8")
    other = work / "other.md"
    other.write_text("another file\n", encoding="utf-8")
    return {"work": work, "named": named, "other": other, "dir": work / "sub"}


def _spec(read_paths: tuple[str, ...]) -> SubAgentSpec:
    return SubAgentSpec(
        task="review the file",
        context=[],
        timeout_seconds=30.0,
        tools=list(_GENERAL_TOOLS),
        worker_type=WorkerType.GENERAL,
        read_paths=read_paths,
    )


async def _read(layer: ToolExecutionLayer, grant: ReadGrant | None, path: str) -> dict[str, Any]:
    """One worker ``read`` through the shared dispatch boundary."""
    result = await dispatch_tool_call(
        tool_call_id="c",
        tool_name="read",
        arguments={"path": path},
        tool_layer=layer,
        trace_ctx=TraceContext.new_trace(),
        trace_id="t-1580",
        session_id=None,
        loaded_skills=set(),
        principal="sub_agent",
        worker="general",
        read_grant=grant,
    )
    payload: dict[str, Any] = json.loads(result["content"])
    return payload


def _refused(payload: dict[str, Any], code: str) -> None:
    assert payload["ok"] is False, payload
    assert payload["error"]["code"] == code, payload["error"]


class TestTheWorkerReadsTheNamedFile:
    @pytest.mark.asyncio
    async def test_the_named_file_is_read_and_listed_in_the_task(
        self, files: dict[str, Path]
    ) -> None:
        with shared_layer(worker_layer()):
            run = await run_worker(
                _spec((str(files["named"]),)), [("read", {"path": str(files["named"])})]
            )
        payload = run.payload("c0")
        assert payload["ok"] is True
        assert payload["content"] == "the named file\n"
        task_message = run.calls[0]["messages"][-1]["content"]
        assert str(files["named"].resolve()) in task_message

    @pytest.mark.asyncio
    async def test_a_path_through_dot_dot_to_the_named_file_is_the_named_file(
        self, files: dict[str, Path]
    ) -> None:
        layer = worker_layer()
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        payload = await _read(layer, grant, str(files["dir"] / ".." / "review_me.md"))
        assert payload["ok"] is True


class TestEveryOtherReadOrWriteIsRefused:
    @pytest.mark.asyncio
    async def test_a_file_the_task_does_not_name(self, files: dict[str, Path]) -> None:
        with shared_layer(worker_layer()):
            run = await run_worker(
                _spec((str(files["named"]),)), [("read", {"path": str(files["other"])})]
            )
        _refused(run.payload("c0"), "not_granted")
        assert "another file" not in run.tool_messages["c0"]["content"]

    @pytest.mark.asyncio
    async def test_a_write_or_an_edit_of_the_named_file(self, files: dict[str, Path]) -> None:
        named = str(files["named"])
        with shared_layer(worker_layer()):
            run = await run_worker(
                _spec((named,)),
                [
                    ("write", {"path": named, "content": "overwritten"}),
                    ("edit", {"path": named, "old_string": "named", "new_string": "edited"}),
                ],
            )
        _refused(run.payload("c0"), "not_available_to_worker")
        _refused(run.payload("c1"), "not_available_to_worker")
        assert files["named"].read_text(encoding="utf-8") == "the named file\n"

    @pytest.mark.asyncio
    async def test_dot_dot_to_another_file(self, files: dict[str, Path]) -> None:
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        payload = await _read(worker_layer(), grant, str(files["dir"] / ".." / "other.md"))
        _refused(payload, "not_granted")

    @pytest.mark.asyncio
    async def test_a_symlink_that_resolves_to_another_file(self, files: dict[str, Path]) -> None:
        link = files["work"] / "link.md"
        link.symlink_to(files["other"])
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        _refused(await _read(worker_layer(), grant, str(link)), "not_granted")

    @pytest.mark.asyncio
    async def test_the_named_file_replaced_by_a_symlink_to_another_file(
        self, files: dict[str, Path]
    ) -> None:
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        files["named"].unlink()
        files["named"].symlink_to(files["other"])
        payload = await _read(worker_layer(), grant, str(files["named"]))
        _refused(payload, "not_granted")
        assert "another file" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_a_directory_named_in_read_paths(self, files: dict[str, Path]) -> None:
        grant = ReadGrant.for_task([str(files["dir"])], trace_id="t")
        assert grant.files == ()
        _refused(await _read(worker_layer(), grant, str(files["dir"])), "not_granted")

    @pytest.mark.asyncio
    async def test_a_hard_link_to_the_named_file_at_another_path(
        self, files: dict[str, Path]
    ) -> None:
        hard = files["work"] / "hard.md"
        os.link(files["named"], hard)
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        _refused(await _read(worker_layer(), grant, str(hard)), "not_granted")

    @pytest.mark.asyncio
    async def test_the_named_file_after_the_task_ends(
        self, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        made: list[ReadGrant] = []
        original = ReadGrant.for_task

        def _record(paths: Any, *, trace_id: str) -> ReadGrant:
            grant = original(paths, trace_id=trace_id)
            made.append(grant)
            return grant

        monkeypatch.setattr(ReadGrant, "for_task", staticmethod(_record))
        with shared_layer(worker_layer()):
            run = await run_worker(
                _spec((str(files["named"]),)), [("read", {"path": str(files["named"])})]
            )
        assert run.payload("c0")["ok"] is True
        (grant,) = made
        assert grant.closed is True
        _refused(await _read(worker_layer(), grant, str(files["named"])), "grant_ended")

    @pytest.mark.asyncio
    async def test_the_named_file_replaced_by_another_regular_file(
        self, files: dict[str, Path]
    ) -> None:
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        replacement = files["work"] / "replacement.md"
        replacement.write_text("a replaced file\n", encoding="utf-8")
        os.replace(replacement, files["named"])
        payload = await _read(worker_layer(), grant, str(files["named"]))
        _refused(payload, "file_replaced")
        assert "a replaced file" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_seeded_negative_without_the_inode_check_the_rename_over_is_read(
        self, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(ReadGrant, "check_opened", lambda self, entry, st: None)
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        replacement = files["work"] / "replacement.md"
        replacement.write_text("a replaced file\n", encoding="utf-8")
        os.replace(replacement, files["named"])
        payload = await _read(worker_layer(), grant, str(files["named"]))
        assert payload["ok"] is True
        assert payload["content"] == "a replaced file\n"


class TestTheGrantIsBounded:
    @pytest.mark.asyncio
    async def test_a_worker_read_with_no_grant_is_refused(self, files: dict[str, Path]) -> None:
        """Fail closed: a worker path that forgets the grant cannot read freely."""
        _refused(await _read(worker_layer(), None, str(files["named"])), "not_available_to_worker")

    @pytest.mark.asyncio
    async def test_the_grant_never_widens_the_path_rules(self) -> None:
        """A forbidden path stays forbidden even when the task names it."""
        grant = ReadGrant.for_task(["/etc/passwd"], trace_id="t")
        payload = await _read(worker_layer(), grant, "/etc/passwd")
        _refused(payload, "path_refused")

    def test_relative_and_missing_paths_grant_nothing(self, files: dict[str, Path]) -> None:
        grant = ReadGrant.for_task(
            ["review_me.md", str(files["work"] / "missing.md"), str(files["named"])],
            trace_id="t",
        )
        assert [f.real_path for f in grant.files] == [str(files["named"].resolve())]

    def test_a_path_with_a_nul_byte_grants_nothing_and_never_raises(
        self, files: dict[str, Path]
    ) -> None:
        """A planner value must grant nothing, not drop the task (code review, FRE-1580)."""
        grant = ReadGrant.for_task([f"{files['named']}\x00x", str(files["named"])], trace_id="t")
        assert [f.real_path for f in grant.files] == [str(files["named"].resolve())]

    def test_at_most_ten_files_are_granted(self, files: dict[str, Path]) -> None:
        paths = []
        for i in range(12):
            path = files["work"] / f"f{i}.md"
            path.write_text("x", encoding="utf-8")
            paths.append(str(path))
        assert len(ReadGrant.for_task(paths, trace_id="t").files) == 10

    @pytest.mark.asyncio
    async def test_the_primary_read_is_unchanged(self, files: dict[str, Path]) -> None:
        result = await dispatch_tool_call(
            tool_call_id="c",
            tool_name="read",
            arguments={"path": str(files["other"])},
            tool_layer=worker_layer(),
            trace_ctx=TraceContext.new_trace(),
            trace_id="t",
            session_id=None,
            loaded_skills=set(),
        )
        assert json.loads(result["content"])["ok"] is True


class TestTheChecksHoldUnderARace:
    """The second check runs on the opened file, so a swap after the first check is refused."""

    @staticmethod
    def _swap_after_check_path(monkeypatch: pytest.MonkeyPatch, swap: Any) -> None:
        original = ReadGrant.check_path

        def _check_then_swap(self: ReadGrant, real_path: Path) -> Any:
            entry = original(self, real_path)
            swap()
            return entry

        monkeypatch.setattr(ReadGrant, "check_path", _check_then_swap)

    @pytest.mark.asyncio
    async def test_a_rename_over_between_the_checks_is_refused(
        self, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")
        replacement = files["work"] / "replacement.md"
        replacement.write_text("swapped in\n", encoding="utf-8")
        self._swap_after_check_path(
            monkeypatch, lambda: os.replace(replacement, files["named"])
        )
        payload = await _read(worker_layer(), grant, str(files["named"]))
        _refused(payload, "file_replaced")
        assert "swapped in" not in json.dumps(payload)

    @pytest.mark.asyncio
    async def test_a_symlink_swapped_in_between_the_checks_is_refused(
        self, files: dict[str, Path], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        grant = ReadGrant.for_task([str(files["named"])], trace_id="t")

        def _swap() -> None:
            files["named"].unlink()
            files["named"].symlink_to(files["other"])

        self._swap_after_check_path(monkeypatch, _swap)
        payload = await _read(worker_layer(), grant, str(files["named"]))
        _refused(payload, "file_replaced")
        assert "another file" not in json.dumps(payload)


class TestTheLayerFailsClosedToo:
    """A worker call that reaches the execution layer directly, without a grant, cannot read."""

    @pytest.mark.asyncio
    async def test_a_worker_call_to_execute_tool_without_a_grant_is_refused(
        self, files: dict[str, Path]
    ) -> None:
        result = await worker_layer().execute_tool(
            "read", {"path": str(files["named"])}, TraceContext.new_trace(), worker="general"
        )
        assert result.success is False
        assert result.error_detail is not None
        assert result.error_detail.code == "not_available_to_worker"
        assert not result.output

    @pytest.mark.asyncio
    async def test_a_model_supplied_read_grant_argument_is_dropped(
        self, files: dict[str, Path]
    ) -> None:
        """The reserved name never reaches the executor from the model's arguments."""
        result = await worker_layer().execute_tool(
            "read",
            {"path": str(files["named"]), "read_grant": "anything"},
            TraceContext.new_trace(),
        )
        assert result.success is True
        assert result.output["content"] == "the named file\n"
