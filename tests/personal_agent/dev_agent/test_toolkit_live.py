# ruff: noqa: D101, D102, D103
"""The toolkit volume with real Docker (ADR-0159 D3.3; FRE-1751 AC-1, AC-2, AC-3).

Each test uses its own volume, internal network and scope label, so it never touches a production
volume or container on the shared Docker host. The assets are synthetic with real SHA-384 pins.
Image: ``SESHAT_DEV_AGENT_TEST_IMAGE``, else ``seshat-dev-agent:fre1684-test``, else
``seshat-dev-agent:0.1``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Mapping, Sequence
from dataclasses import dataclass
from uuid import uuid4

import pytest
import pytest_asyncio

from personal_agent.dev_agent.container_spec import agent_container_name, load_profiles
from personal_agent.dev_agent.launcher import (
    SCOPE_LABEL,
    CommandResult,
    DockerLauncher,
    run_command,
)
from personal_agent.dev_agent.toolkit import (
    HELPER_LABEL,
    MANIFEST_FILE,
    TOOLKIT_MOUNT,
    fill_toolkit_volume,
    toolkit_digest,
)
from personal_agent.exceptions import DevAgentToolkitError
from personal_agent.storage.artifact_export import verify_sri

from .toolkit_fakes import SYNTHETIC_FILES, fetch_synthetic, synthetic_assets

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _docker(*args: str, check: bool = True) -> subprocess.CompletedProcess[bytes]:
    result = subprocess.run(["docker", *args], capture_output=True, timeout=120, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"docker {' '.join(args)}: {result.stderr.decode()}")
    return result


def _image() -> str | None:
    for candidate in (
        os.environ.get("SESHAT_DEV_AGENT_TEST_IMAGE"),
        "seshat-dev-agent:fre1684-test",
        "seshat-dev-agent:0.1",
    ):
        if candidate and _docker("image", "inspect", candidate, check=False).returncode == 0:
            return candidate
    return None


@dataclass
class Env:
    volume: str
    network: str
    scope: str
    image: str

    def launcher(self) -> DockerLauncher:
        profile = load_profiles().agent.model_copy(update={"image": self.image})
        return DockerLauncher(
            profile,
            network_name=self.network,
            toolkit_volume=self.volume,
            toolkit_assets=synthetic_assets(),
            scope=self.scope,
        )

    async def fill(self) -> None:
        await fill_toolkit_volume(
            self.volume, synthetic_assets(), fetch_synthetic, image=self.image, runner=run_command
        )

    def volume_exists(self) -> bool:
        return _docker("volume", "inspect", self.volume, check=False).returncode == 0

    def read(self, path: str) -> bytes:
        """Read a file of the volume through a container with a read-only mount."""
        return _docker(
            "run", "--rm", "--network", "none", "-v", f"{self.volume}:{TOOLKIT_MOUNT}:ro",
            "--entrypoint", "cat", self.image, f"{TOOLKIT_MOUNT}/{path}",
        ).stdout  # fmt: skip


@pytest_asyncio.fixture
async def env() -> AsyncIterator[Env]:
    if shutil.which("docker") is None:
        pytest.skip("no docker")
    image = _image()
    if image is None:
        pytest.skip("no dev-agent image: run make dev-agent-build")
    tag = uuid4().hex[:8]
    live = Env(
        volume=f"fre1751-toolkit-{tag}",
        network=f"fre1751-net-{tag}",
        scope=f"fre1751-test-{tag}",
        image=image,
    )
    _docker("network", "create", "--internal", live.network)
    try:
        yield live
    finally:
        label = f"label={SCOPE_LABEL}={live.scope}"
        for container in _docker("ps", "-aq", "--filter", label).stdout.decode().split():
            _docker("rm", "-f", container, check=False)
        for volume in _docker("volume", "ls", "-q", "--filter", label).stdout.decode().split():
            _docker("volume", "rm", "-f", volume, check=False)
        _docker("network", "rm", live.network, check=False)
        _docker("volume", "rm", "-f", live.volume, check=False)


class TestFill:
    async def test_the_volume_holds_every_asset_and_each_file_matches_its_pin(
        self, env: Env
    ) -> None:
        """AC-1."""
        await env.fill()
        for asset in synthetic_assets():
            data = env.read(asset.path)
            assert data == SYNTHETIC_FILES[asset.path]
            assert verify_sri(data, asset.sri), asset.path
        assert toolkit_digest(synthetic_assets()) in env.read(MANIFEST_FILE).decode()

    async def test_a_corrupt_asset_leaves_no_volume(self, env: Env) -> None:
        """AC-2."""

        async def corrupt(path: str) -> bytes:
            return SYNTHETIC_FILES[path] + (b"x" if path.endswith(".js") else b"")

        with pytest.raises(DevAgentToolkitError, match="differs from the pin"):
            await fill_toolkit_volume(
                env.volume, synthetic_assets(), corrupt, image=env.image, runner=run_command
            )
        assert not env.volume_exists()

    async def test_bytes_that_change_after_the_copy_leave_no_volume_and_no_helper(
        self, env: Env
    ) -> None:
        """AC-2, the read-back path: the bytes are right in memory and wrong in the volume."""
        first = synthetic_assets()[0].path

        async def tampering(
            args: Sequence[str], *, env: Mapping[str, str] | None = None
        ) -> CommandResult:
            result = await run_command(args, env=env)
            if list(args[1:2]) == ["cp"] and args[3].startswith("seshat-dev-toolkit-"):
                _docker(
                    "run", "--rm", "--user", "0", "--network", "none", "-v", f"{env_.volume}:/t",
                    "--entrypoint", "sh", env_.image, "-c", f"printf x >> /t/{first}",
                )  # fmt: skip
            return result

        env_ = env
        with pytest.raises(DevAgentToolkitError, match="differs from the pin"):
            await fill_toolkit_volume(
                env.volume, synthetic_assets(), fetch_synthetic, image=env.image, runner=tampering
            )
        assert not env.volume_exists()
        leftovers = _docker("ps", "-aq", "--filter", f"label={HELPER_LABEL}=1").stdout
        assert leftovers.decode().split() == []

    async def test_a_second_fill_replaces_the_volume(self, env: Env) -> None:
        await env.fill()
        _docker(
            "run", "--rm", "--user", "0", "--network", "none", "-v", f"{env.volume}:/t",
            "--entrypoint", "sh", env.image, "-c", "echo evil > /t/planted.js",
        )  # fmt: skip
        await env.fill()
        listing = _docker(
            "run", "--rm", "--network", "none", "-v", f"{env.volume}:/t:ro",
            "--entrypoint", "ls", env.image, "-R", "/t",
        ).stdout.decode()  # fmt: skip
        assert "planted.js" not in listing


class TestAgentContainer:
    async def test_the_launcher_container_reads_the_toolkit_and_cannot_write_it(
        self, env: Env
    ) -> None:
        """AC-3."""
        await env.fill()
        launcher = env.launcher()
        job = uuid4()
        asset = synthetic_assets()[0]
        name = agent_container_name(job)
        try:
            await launcher.start(job, job_token="t", server_password="p")  # noqa: S106
            path = f"{TOOLKIT_MOUNT}/{asset.path}"
            read = _docker("exec", name, "cat", path)
            assert read.stdout == SYNTHETIC_FILES[asset.path]
            # Control: the same exec can write where the profile allows it. A failed write to the
            # toolkit below then comes from the read-only mount, not from a broken exec.
            assert _docker("exec", name, "sh", "-c", "echo x > /tmp/control").returncode == 0
            for command in (f"echo x >> {path}", f"rm {path}", f"touch {TOOLKIT_MOUNT}/new.js"):
                result = _docker("exec", name, "sh", "-c", command, check=False)
                assert result.returncode != 0, command
                assert b"Read-only" in result.stderr or b"denied" in result.stderr, result.stderr
            assert env.read(asset.path) == SYNTHETIC_FILES[asset.path]
        finally:
            await launcher.remove(job)

    async def test_a_volume_with_a_planted_file_starts_no_container(self, env: Env) -> None:
        await env.fill()
        _docker(
            "run", "--rm", "--user", "0", "--network", "none", "-v", f"{env.volume}:/t",
            "--entrypoint", "sh", env.image, "-c", "echo evil > /t/planted.js",
        )  # fmt: skip
        job = uuid4()
        with pytest.raises(DevAgentToolkitError, match="unexpected file: planted.js"):
            await env.launcher().start(job, job_token="t", server_password="p")  # noqa: S106
        assert _docker("ps", "-aq", "--filter", f"name={agent_container_name(job)}").stdout == b""
