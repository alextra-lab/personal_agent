# ruff: noqa: D101, D102
"""The handoff and the workspace clone with real Docker containers (FRE-1685 AC-1, AC-2).

* A hostile workspace (``core.fsmonitor``, a clean/smudge filter, hooks, an alias) runs nothing
  during the handoff. A marker volume outside the handoff container is the proof. A control run of
  plain git in the same workspace writes the markers, so the seeded config is live.
* The whole path, with the real launcher: ``prepare`` → the clone copied into the agent
  container → the agent commits and rewrites ``main`` → the real handoff container → ``handoff``.
  ``main`` on the remote does not change.

A local bare repo stands in for GitHub (no network). Images: ``SESHAT_DEV_HANDOFF_TEST_IMAGE``,
else ``seshat-dev-handoff:fre1685-test``, else the profile's image, and ``seshat-dev-agent:0.1``.
Each test uses its own scope label and removes what it created.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from uuid import uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.dev_agent.container_spec import (
    ContainerProfile,
    agent_container_name,
    build_create_args,
    load_profiles,
)
from personal_agent.dev_agent.jobs import JobStore
from personal_agent.dev_agent.launcher import JOB_LABEL, SCOPE_LABEL, DockerLauncher
from personal_agent.dev_agent.repo_pipeline import GitHubRepoPipeline, job_branch
from personal_agent.dev_agent.supervisor import Fence, SupervisorPolicy

from .conftest import MakeJob
from .test_repo_pipeline_integration import LocalHost

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]


def _image(env: str, *candidates: str) -> str:
    chosen = os.environ.get(env)
    if chosen:
        return chosen
    for image in candidates:
        if (
            subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode
            == 0
        ):
            return image
    pytest.skip(f"none of {candidates} is built")


def docker(*args: str, check: bool = True, stdin: bytes | None = None) -> str:
    out = subprocess.run(["docker", *args], input=stdin, capture_output=True, check=False)
    if check and out.returncode != 0:
        raise AssertionError(f"docker {args[0]}: {out.stderr.decode()[-500:]}")
    return out.stdout.decode()


@pytest.fixture
def handoff_profile() -> ContainerProfile:
    if shutil.which("docker") is None:
        pytest.skip("docker is not available")
    profile = load_profiles().handoff
    image = _image(
        "SESHAT_DEV_HANDOFF_TEST_IMAGE", "seshat-dev-handoff:fre1685-test", profile.image
    )
    return profile.model_copy(update={"image": image})


@pytest.fixture
def scratch() -> Iterator[list[str]]:
    """Names of volumes and containers to remove after the test."""
    names: list[str] = []
    yield names
    for name in names:
        docker("rm", "-f", name, check=False)
        docker("volume", "rm", "-f", name, check=False)


_HOSTILE = r"""
set -e
cd /workspace
git init -q -b "$1" .
git config user.email a@b.c
git config user.name a
echo hi > f
git add f
git commit -qm base
git config core.fsmonitor 'touch /marker/fsmonitor #'
git config filter.x.clean 'touch /marker/filter-clean; cat'
git config filter.x.smudge 'touch /marker/filter-smudge; cat'
git config filter.x.required true
git config alias.bundle '!touch /marker/alias'
git config core.alternateRefsCommand 'touch /marker/alternate-refs #'
echo '* filter=x' > .gitattributes
for hook in pre-commit post-checkout reference-transaction post-index-change pre-auto-gc; do
  printf '#!/bin/sh\ntouch /marker/hook-%s\n' "$hook" > ".git/hooks/$hook"
  chmod +x ".git/hooks/$hook"
done
"""

_CONTROL = r"""
cd /workspace
git -c safe.directory='*' status >/dev/null 2>&1
echo two > g
git -c safe.directory='*' add g .gitattributes >/dev/null 2>&1
git -c safe.directory='*' commit -qm two >/dev/null 2>&1
git -c safe.directory='*' checkout -q -- f >/dev/null 2>&1
git -c safe.directory='*' bundle >/dev/null 2>&1
true
"""


def _markers(image: str, volume: str) -> list[str]:
    out = docker(
        "run", "--rm", "-v", f"{volume}:/marker:ro", "--entrypoint", "ls", image, "/marker"
    )
    return sorted(out.split())


def _volume(scratch: list[str], name: str) -> str:
    docker("volume", "create", name)
    scratch.append(name)
    return name


class TestHostileWorkspace:
    async def test_hostile_config_runs_nothing(
        self, handoff_profile: ContainerProfile, scratch: list[str]
    ) -> None:
        """AC: a malicious .git/config (fsmonitor, a filter, a hook) runs none of them."""
        tag = uuid4().hex[:8]
        image = handoff_profile.image
        branch = f"job/{uuid4()}"
        ws = _volume(scratch, f"fre1685-ws-{tag}")
        control = _volume(scratch, f"fre1685-marker-control-{tag}")
        marker = _volume(scratch, f"fre1685-marker-{tag}")
        out = _volume(scratch, f"fre1685-out-{tag}")
        for volume in (control, marker):
            docker("run", "--rm", "-u", "0", "-v", f"{volume}:/marker", "--entrypoint", "chmod",
                   image, "0777", "/marker")  # fmt: skip
        docker("run", "--rm", "-v", f"{ws}:/workspace", "--entrypoint", "sh", image, "-c",
               _HOSTILE, "sh", branch)  # fmt: skip

        # Control: plain git in the same workspace runs the seeded config.
        docker("run", "--rm", "--network", "none", "-v", f"{ws}:/workspace", "-v",
               f"{control}:/marker", "--entrypoint", "sh", image, "-c", _CONTROL)  # fmt: skip
        seen = _markers(image, control)
        print(f"control markers: {seen}")
        assert "fsmonitor" in seen
        assert any(m.startswith("filter-") for m in seen)
        assert any(m.startswith("hook-") for m in seen)

        # The handoff: the profile's boundary, plus the marker volume as the only addition.
        name = f"fre1685-handoff-{tag}"
        scratch.append(name)
        args = build_create_args(
            handoff_profile, name=name, network_name="none", labels={},
            volumes={"/workspace": ws, "/out": out},
        )  # fmt: skip
        args = [*args[:-1], "-v", f"{marker}:/marker", args[-1]]
        docker(*args[1:], branch)
        result = subprocess.run(["docker", "start", "-a", name], capture_output=True)
        assert result.returncode == 0, result.stderr.decode()
        assert _markers(image, marker) == [], "the hostile config ran during the handoff"
        heads = docker("run", "--rm", "-v", f"{out}:/out:ro", "--entrypoint", "git", image,
                       "bundle", "list-heads", "/out/job.bundle")  # fmt: skip
        assert heads.split()[1:] == [f"refs/heads/{branch}"]

    async def test_a_symbolic_ref_is_refused(
        self, handoff_profile: ContainerProfile, scratch: list[str]
    ) -> None:
        tag = uuid4().hex[:8]
        image = handoff_profile.image
        branch = f"job/{uuid4()}"
        ws = _volume(scratch, f"fre1685-ws-{tag}")
        out = _volume(scratch, f"fre1685-out-{tag}")
        setup = (
            "set -e; cd /workspace; git init -q -b main .; git -c user.name=a -c user.email=a@b "
            "commit -q --allow-empty -m x; mkdir -p .git/refs/heads/job; "
            'echo "ref: refs/heads/main" > ".git/refs/heads/$1"'
        )
        docker("run", "--rm", "-v", f"{ws}:/workspace", "--entrypoint", "sh", image, "-c", setup,
               "sh", branch)  # fmt: skip
        name = f"fre1685-handoff-{tag}"
        scratch.append(name)
        args = build_create_args(
            handoff_profile, name=name, network_name="none", labels={},
            volumes={"/workspace": ws, "/out": out},
        )  # fmt: skip
        docker(*args[1:], branch)
        result = subprocess.run(["docker", "start", "-a", name], capture_output=True)
        assert result.returncode == 3
        assert b"not a commit id" in result.stderr


@pytest_asyncio.fixture
async def live(
    admin_pool: asyncpg.Pool, handoff_profile: ContainerProfile, tmp_path: Path
) -> AsyncIterator[tuple[DockerLauncher, LocalHost, str]]:
    scope = f"fre1685-test-{uuid4().hex[:8]}"
    toolkit = f"fre1685-toolkit-{uuid4().hex[:8]}"
    docker("volume", "create", toolkit)
    agent = load_profiles().agent
    image = _image("SESHAT_DEV_AGENT_TEST_IMAGE", "seshat-dev-agent:fre1684-test", agent.image)
    launcher = DockerLauncher(
        agent.model_copy(update={"image": image}),
        network_name="none",
        toolkit_volume=toolkit,
        scope=scope,
        handoff_profile=handoff_profile,
    )
    host = LocalHost(tmp_path / "github")
    host.root.mkdir()
    try:
        yield launcher, host, scope
    finally:
        for kind in ("ps -aq", "volume ls -q"):
            ids = docker(*kind.split(), "--filter", f"label={SCOPE_LABEL}={scope}").split()
            if ids:
                docker(*(["rm", "-f"] if kind.startswith("ps") else ["volume", "rm", "-f"]), *ids,
                       check=False)  # fmt: skip
        docker("volume", "rm", "-f", toolkit, check=False)


class TestWholePath:
    async def test_main_rewrite_in_workspace_does_not_reach_remote(
        self,
        live: tuple[DockerLauncher, LocalHost, str],
        store: JobStore,
        admin_pool: asyncpg.Pool,
        make_job: MakeJob,
    ) -> None:
        """AC: an agent commit that rewrites main in the workspace leaves main unchanged."""
        launcher, host, _ = live
        job = await make_job()
        taken = await store.claim_new("fre1685-live", 120.0)
        assert taken is not None
        fence = Fence(store, taken[1], SupervisorPolicy(), None)
        pipeline = GitHubRepoPipeline(pool=admin_pool, launcher=launcher, host=host, pages=None)

        prepared = await pipeline.prepare(job, fence)
        repo, base = prepared.job.repo or "", prepared.job.base_commit
        await launcher.start(
            job.id, job_token="t", server_password="p", workspace=prepared.workspace
        )
        name = agent_container_name(job.id)
        branch = job_branch(job.id)
        agent = (
            "set -e; cd /workspace; "
            f'test "$(git symbolic-ref HEAD)" = "refs/heads/{branch}"; '
            "echo '<p>page</p>' > dist/index.html; git add -A; git commit -qm page; "
            "git checkout -q --orphan evil; git commit -q --allow-empty -m evil; "
            "git branch -f main evil; git checkout -q " + branch
        )
        docker("exec", name, "sh", "-c", agent)
        agent_head = docker("exec", name, "git", "-C", "/workspace", "rev-parse", branch).strip()
        await launcher.stop(job.id)

        result = await pipeline.handoff(prepared.job, fence)

        assert result == agent_head
        assert host.ref(repo, f"refs/heads/{branch}") == agent_head
        assert host.ref(repo, "refs/heads/main") == base, "main on the remote did not change"
        labelled = docker("ps", "-aq", "--filter", f"label={JOB_LABEL}={job.id}").split()
        assert len(labelled) == 1, "only the stopped agent container remains; the handoff is gone"
        await launcher.remove(job.id)
