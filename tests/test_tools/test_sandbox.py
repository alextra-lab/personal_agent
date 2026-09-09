"""Unit tests for the sandbox execution layer (tools/primitives/sandbox.py).

All Docker subprocess calls are mocked — no real containers are spawned.

FRE-261 Step 5. FRE-1462 (execution bound + cleanup + reaper).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from personal_agent.tools.primitives.sandbox import (
    reap_orphaned_sandbox_containers,
    run_in_sandbox,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_proc(
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
) -> MagicMock:
    """Return a mock asyncio.subprocess.Process for the `docker run` call."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(stdout, stderr))
    proc.kill = MagicMock()
    proc.wait = AsyncMock(return_value=None)
    return proc


def _make_mock_rm_proc(returncode: int = 0, stderr: bytes = b"") -> MagicMock:
    """Return a mock asyncio.subprocess.Process for the `docker rm -f` call."""
    proc = MagicMock()
    proc.returncode = returncode
    proc.communicate = AsyncMock(return_value=(b"", stderr))
    return proc


def _run_and_rm_mocks(
    *,
    returncode: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    rm_returncode: int = 0,
) -> tuple[MagicMock, MagicMock]:
    """Return (run_proc_mock, rm_proc_mock) for the two subprocess calls.

    Every run_in_sandbox() invocation now makes: `docker run` then the
    unconditional `docker rm -f` cleanup in `finally`.
    """
    return (
        _make_mock_proc(returncode=returncode, stdout=stdout, stderr=stderr),
        _make_mock_rm_proc(returncode=rm_returncode),
    )


# ---------------------------------------------------------------------------
# Tests — happy path / error paths (existing behaviour)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_docker_not_available(tmp_path: Path) -> None:
    """When docker binary is absent, returns a graceful SandboxResult."""
    with patch("shutil.which", return_value=None):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('hello')",
            timeout_seconds=10,
            scratch_host_path=tmp_path / "scratch",
        )

    assert result.exit_code == 1
    assert result.timed_out is False
    assert result.oom is False
    assert "docker" in result.stderr.lower()
    assert result.scratch_files == []


@pytest.mark.asyncio
async def test_sandbox_happy_path(tmp_path: Path) -> None:
    """Successful run returns stdout and exit_code=0."""
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"42\n", stderr=b"")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print(42)",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert result.exit_code == 0
    assert result.stdout == "42\n"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.oom is False


@pytest.mark.asyncio
async def test_sandbox_timeout(tmp_path: Path) -> None:
    """asyncio.TimeoutError → timed_out=True and a human-readable reason."""
    scratch = tmp_path / "scratch"
    run_proc = _make_mock_proc(returncode=0)
    # Make communicate itself raise TimeoutError — no need to patch the
    # global asyncio.wait_for, which would also intercept the cleanup
    # path's own wait_for calls and break them.
    run_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    rm_proc = _make_mock_rm_proc(returncode=0)

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="import time; time.sleep(999)",
            timeout_seconds=1,
            scratch_host_path=scratch,
        )

    assert result.timed_out is True
    assert result.exit_code == 1
    assert result.oom is False
    assert "1s limit" in result.stderr


@pytest.mark.asyncio
async def test_sandbox_oom(tmp_path: Path) -> None:
    """Exit code 137 → oom=True in result."""
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=137, stdout=b"", stderr=b"Killed\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="x = bytearray(1024**3)",
            timeout_seconds=30,
            scratch_host_path=scratch,
        )

    assert result.oom is True
    assert result.exit_code == 137
    assert result.timed_out is False


@pytest.mark.asyncio
async def test_sandbox_nonzero_exit(tmp_path: Path) -> None:
    """Non-zero exit that is not 137 → oom=False, success=False."""
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=1, stdout=b"", stderr=b"SyntaxError\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="invalid python !!",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert result.exit_code == 1
    assert result.oom is False
    assert result.timed_out is False
    assert "SyntaxError" in result.stderr


@pytest.mark.asyncio
async def test_sandbox_scratch_files_listed(tmp_path: Path) -> None:
    """Files created in scratch dir are reported in scratch_files."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    # Pre-create a file as if the script wrote it
    (scratch / "output.txt").write_text("result")

    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"done\n")

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="open('/sandbox/output.txt', 'w').write('result')",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert str(scratch / "output.txt") in result.scratch_files


# ---------------------------------------------------------------------------
# Tests — container naming, labels, and cleanup (FRE-1462)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_container_name_and_labels_present(tmp_path: Path) -> None:
    """docker_args carry a unique --name and identifying --label entries."""
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"ok\n")
    captured_args: list[list[str]] = []

    async def _capture(*args: str, **kwargs: object) -> MagicMock:
        captured_args.append(list(args))
        return run_proc if args[1] == "run" else rm_proc

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_capture)),
    ):
        await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('hi')",
            timeout_seconds=10,
            scratch_host_path=scratch,
            trace_id="trace-abc",
            tool="run_python",
        )

    run_args = captured_args[0]
    assert "--name" in run_args
    name = run_args[run_args.index("--name") + 1]
    assert name.startswith("seshat-sbx-")

    labels = [run_args[i + 1] for i, a in enumerate(run_args) if a == "--label"]
    assert "seshat.sandbox=true" in labels
    assert "seshat.sandbox.tool=run_python" in labels
    assert "seshat.sandbox.trace_id=trace-abc" in labels
    assert any(lbl.startswith("seshat.sandbox.deadline=") for lbl in labels)

    # Cleanup (docker rm -f <name>) is the second call, targeting the same name.
    rm_args = captured_args[1]
    assert rm_args[:3] == ["docker", "rm", "-f"]
    assert rm_args[3] == name


@pytest.mark.asyncio
async def test_sandbox_force_removes_container_on_timeout(tmp_path: Path) -> None:
    """On timeout, docker rm -f is still issued against the named container."""
    scratch = tmp_path / "scratch"
    # A genuinely timed-out process has not exited — returncode is None.
    run_proc = _make_mock_proc(returncode=None)
    run_proc.communicate = AsyncMock(side_effect=asyncio.TimeoutError)
    rm_proc = _make_mock_rm_proc(returncode=0)
    rm_calls: list[list[str]] = []

    async def _capture(*args: str, **kwargs: object) -> MagicMock:
        if args[1] == "run":
            return run_proc
        rm_calls.append(list(args))
        return rm_proc

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_capture)),
    ):
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="while True: pass",
            timeout_seconds=1,
            scratch_host_path=scratch,
        )

    assert result.timed_out is True
    assert len(rm_calls) == 1
    assert rm_calls[0][:3] == ["docker", "rm", "-f"]
    # The local CLI client is killed too — it must not go on to attach a
    # container after cleanup already ran.
    run_proc.kill.assert_called_once()


@pytest.mark.asyncio
async def test_sandbox_cleanup_runs_on_cancellation(tmp_path: Path) -> None:
    """A CancelledError mid-run still force-removes the container, then propagates."""
    scratch = tmp_path / "scratch"
    # A cancelled-mid-run process has not exited — returncode is None.
    run_proc = _make_mock_proc(returncode=None)
    run_proc.communicate = AsyncMock(side_effect=asyncio.CancelledError)
    rm_proc = _make_mock_rm_proc(returncode=0)
    rm_calls: list[list[str]] = []

    async def _capture(*args: str, **kwargs: object) -> MagicMock:
        if args[1] == "run":
            return run_proc
        rm_calls.append(list(args))
        return rm_proc

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_capture)),
    ):
        with pytest.raises(asyncio.CancelledError):
            await run_in_sandbox(
                image="seshat-sandbox-python:0.1",
                script="while True: pass",
                timeout_seconds=10,
                scratch_host_path=scratch,
            )

    assert len(rm_calls) == 1
    assert rm_calls[0][:3] == ["docker", "rm", "-f"]


@pytest.mark.asyncio
async def test_sandbox_force_remove_treats_already_gone_as_success(tmp_path: Path) -> None:
    """A concurrent removal ('No such container') is not treated as failure."""
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"ok\n")
    rm_proc.returncode = 1
    rm_proc.communicate = AsyncMock(return_value=(b"", b"Error: No such container: seshat-sbx-x"))

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[run_proc, rm_proc]),
        ),
    ):
        # Must not raise, and the happy-path result must be unaffected.
        result = await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('ok')",
            timeout_seconds=10,
            scratch_host_path=scratch,
        )

    assert result.exit_code == 0
    assert result.stdout == "ok\n"


# ---------------------------------------------------------------------------
# Tests — reap_orphaned_sandbox_containers() (FRE-1462 AC-3)
# ---------------------------------------------------------------------------


def _mock_ps_proc(stdout_lines: list[str], returncode: int = 0) -> MagicMock:
    proc = MagicMock()
    proc.returncode = returncode
    body = "\n".join(stdout_lines).encode("utf-8")
    proc.communicate = AsyncMock(return_value=(body, b""))
    return proc


@pytest.mark.asyncio
async def test_reap_orphaned_removes_past_deadline() -> None:
    """A container whose deadline label is in the past is force-removed and counted."""
    ps_proc = _mock_ps_proc(["abc123\t1.0\ttrace-1\trun_python"])
    rm_proc = _make_mock_rm_proc(returncode=0)

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[ps_proc, rm_proc]),
        ),
    ):
        reaped = await reap_orphaned_sandbox_containers()

    assert reaped == 1


@pytest.mark.asyncio
async def test_reap_orphaned_skips_before_deadline() -> None:
    """A container whose deadline is in the future is left alone (seeded negative)."""
    far_future = "9999999999.0"
    ps_proc = _mock_ps_proc([f"abc123\t{far_future}\ttrace-1\trun_python"])

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(return_value=ps_proc),
        ) as mock_exec,
    ):
        reaped = await reap_orphaned_sandbox_containers()

    assert reaped == 0
    # Only the `docker ps` call — no rm was ever issued for the live container.
    assert mock_exec.await_count == 1


@pytest.mark.asyncio
async def test_reap_orphaned_no_docker_binary_is_noop() -> None:
    """Without the docker binary, the sweep is a safe no-op."""
    with patch("shutil.which", return_value=None):
        reaped = await reap_orphaned_sandbox_containers()

    assert reaped == 0


@pytest.mark.asyncio
async def test_reap_orphaned_malformed_line_skipped_continues() -> None:
    """A malformed line does not block reaping containers on later lines."""
    ps_proc = _mock_ps_proc(
        [
            "malformed-line-with-only-one-field",
            "abc123\tnot-a-number\ttrace-1\trun_python",
            "def456\t1.0\ttrace-2\trun_python",
        ]
    )
    rm_proc = _make_mock_rm_proc(returncode=0)

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[ps_proc, rm_proc]),
        ),
    ):
        reaped = await reap_orphaned_sandbox_containers()

    assert reaped == 1


@pytest.mark.asyncio
async def test_reap_orphaned_does_not_count_failed_removal() -> None:
    """A removal that cannot be confirmed is not counted as reaped."""
    ps_proc = _mock_ps_proc(["abc123\t1.0\ttrace-1\trun_python"])
    rm_proc = _make_mock_rm_proc(returncode=1)
    rm_proc.communicate = AsyncMock(return_value=(b"", b"permission denied"))

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch(
            "asyncio.create_subprocess_exec",
            new=AsyncMock(side_effect=[ps_proc, rm_proc]),
        ),
    ):
        reaped = await reap_orphaned_sandbox_containers()

    assert reaped == 0


# ---------------------------------------------------------------------------
# Tests — network configuration (FRE-1466)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sandbox_network_name_from_config(tmp_path: Path) -> None:
    """When network=True, uses sandbox_network from config, not a hardcoded value.

    AC-3: network name is resolved from configuration.
    """
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"ok\n")
    captured_args: list[list[str]] = []

    async def _capture(*args: str, **kwargs: object) -> MagicMock:
        captured_args.append(list(args))
        return run_proc if args[1] == "run" else rm_proc

    # Mock the config to use a custom network name
    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_capture)),
        patch("personal_agent.tools.primitives.sandbox.settings") as mock_settings,
    ):
        mock_settings.sandbox_network = "custom-network-name"
        await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('hi')",
            timeout_seconds=10,
            scratch_host_path=scratch,
            network=True,
        )

    # Verify docker args contain --network=<config_value>, not hardcoded "cloud-sim"
    run_args = captured_args[0]
    network_args = [arg for arg in run_args if arg.startswith("--network=")]
    assert len(network_args) == 1
    network_arg = network_args[0]
    assert network_arg == "--network=custom-network-name"
    # Verify it's not hardcoded to "cloud-sim"
    assert "--network=cloud-sim" not in run_args


@pytest.mark.asyncio
async def test_sandbox_network_none_when_disabled(tmp_path: Path) -> None:
    """When network=False, uses network=none for isolation.

    AC-2 seeded negative: disabled network still produces isolation.
    """
    scratch = tmp_path / "scratch"
    run_proc, rm_proc = _run_and_rm_mocks(returncode=0, stdout=b"ok\n")
    captured_args: list[list[str]] = []

    async def _capture(*args: str, **kwargs: object) -> MagicMock:
        captured_args.append(list(args))
        return run_proc if args[1] == "run" else rm_proc

    with (
        patch("shutil.which", return_value="/usr/bin/docker"),
        patch("asyncio.create_subprocess_exec", new=AsyncMock(side_effect=_capture)),
    ):
        await run_in_sandbox(
            image="seshat-sandbox-python:0.1",
            script="print('hi')",
            timeout_seconds=10,
            scratch_host_path=scratch,
            network=False,
        )

    run_args = captured_args[0]
    network_args = [arg for arg in run_args if arg.startswith("--network=")]
    assert len(network_args) == 1
    network_arg = network_args[0]
    assert network_arg == "--network=none"
