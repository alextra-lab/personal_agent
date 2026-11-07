# ruff: noqa: D101, D102, D103, D107
"""The harness of the check and delivery tests (FRE-1686).

A whole job runs through the real :class:`JobSupervisor` and the real
:class:`GitHubRepoPipeline` on the test stack. ``LocalHost`` stands in for GitHub. The agent is
played by :class:`CommittingLauncher`: at the container start it commits a seeded page and tests
into the workspace clone, and at the handoff it returns their bundle. R2 is :class:`FakeR2`, which
can hang at a chosen point to seed a crash. The checker is a fake or the real ``DockerLauncher``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from personal_agent.config import settings
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.launcher import BytesResult
from personal_agent.dev_agent.publish_check import CheckLauncher, CheckMode, CheckSetup
from personal_agent.dev_agent.repo_pipeline import GitHubRepoPipeline, job_branch
from personal_agent.dev_agent.supervisor import JobSupervisor
from personal_agent.tools import artifact_tools

from .supervisor_fakes import FakeLauncher, ManualClock, adapter_factory, succeed
from .test_repo_pipeline_integration import LocalHost, bundle, git, unpack

CrashPoint = Literal["before_upload", "after_upload", "after_row_insert", "before_end_state"]

GOOD_TEST = """
import { test, expect } from '@playwright/test';
test('add counts up', async ({ page }) => {
  await page.goto(process.env.PAGE_URL);
  await page.click('#add');
  await expect(page.locator('#out')).toHaveText('1');
});
"""


@dataclass
class Seed:
    """What the played agent commits: the page, and the files of ``tests/`` (``None`` = none)."""

    page: str
    tests: Mapping[str, str] | None = field(default_factory=lambda: {"page.spec.mjs": GOOD_TEST})


class Hang:
    """One seeded crash point. The first time it is reached, it waits until the task is cancelled."""

    def __init__(self) -> None:
        self.at: CrashPoint | None = None
        self.reached = asyncio.Event()

    async def point(self, name: CrashPoint) -> None:
        if self.at == name:
            self.at = None
            self.reached.set()
            await asyncio.Event().wait()


class CommittingLauncher(FakeLauncher):
    """Plays the agent: commits the seed into the workspace at the start, bundles it at the handoff."""

    def __init__(self, tmp: Path, hang: Hang) -> None:
        super().__init__()
        self.tmp = tmp
        self.hang = hang
        self.seeds: dict[UUID, Seed] = {}
        self.bundles: dict[UUID, bytes] = {}

    async def start(
        self,
        job_id: UUID,
        *,
        job_token: str,
        server_password: str,
        workspace: bytes | None = None,
    ) -> None:
        await super().start(
            job_id, job_token=job_token, server_password=server_password, workspace=workspace
        )
        seed = self.seeds[job_id]
        ws = unpack(workspace or b"", self.tmp / f"ws-{job_id}")
        (ws / "dist" / "index.html").write_text(seed.page)
        tests = ws / "tests"
        for old in tests.iterdir():
            old.unlink()
        for name, body in (seed.tests or {}).items():
            (tests / name).write_text(body)
        if seed.tests is None:
            tests.rmdir()
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "the page")
        self.bundles[job_id] = bundle(ws, job_branch(job_id))

    async def bundle_workspace(self, job_id: UUID, branch: str) -> bytes:
        self.calls.append(("bundle", job_id, self.who))
        return self.bundles[job_id]

    async def remove(self, job_id: UUID) -> None:
        await self.hang.point("before_end_state")
        await super().remove(job_id)


class FakeR2:
    """An in-memory artifact store that can hang before or after a put."""

    def __init__(self, hang: Hang) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0
        self.fail_puts = 0
        self.hang = hang

    async def put(
        self,
        *,
        r2_key: str,
        content: bytes,
        content_type: str,
        metadata: object = None,
        trace_id: str | None = None,
    ) -> None:
        await self.hang.point("before_upload")
        if self.fail_puts:
            self.fail_puts -= 1
            raise ConnectionError("seeded: R2 unreachable")
        self.puts += 1
        self.objects[r2_key] = content
        await self.hang.point("after_upload")

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        return self.objects[r2_key]

    async def delete(self, r2_key: str, *, trace_id: str | None = None) -> None:
        self.objects.pop(r2_key, None)


def check_output(mode: CheckMode, *, passed: bool) -> BytesResult:
    """Return the output that a check run would print."""
    names = ["load", "interaction"] if mode == "page" else ["repo_tests"]
    body: dict[str, object] = {
        "mode": mode,
        "steps": [
            {"name": n, "passed": passed, "messages": [] if passed else ["seeded"]} for n in names
        ],
    }
    if mode == "page":
        body["csp_manifest_sha256"] = "d" * 64
    return BytesResult(0, b"SESHAT_CHECK_RESULT " + json.dumps(body).encode() + b"\n", "")


class FakeChecker:
    """A checker whose verdict the test chooses. It records the inputs that it received."""

    def __init__(self, *, passes: bool = True, fail: Exception | None = None) -> None:
        self.passes = passes
        self.fail = fail
        self.inputs: dict[UUID, bytes] = {}
        self.removed: list[UUID] = []

    async def prepare_check(self, job_id: UUID, inputs: bytes) -> CheckSetup:
        if self.fail is not None:
            raise self.fail
        self.inputs[job_id] = inputs
        return CheckSetup(image_id="sha256:" + "e" * 64, toolkit_digest="sha256:" + "f" * 64)

    async def run_check(
        self, job_id: UUID, setup: CheckSetup, mode: CheckMode, timeout: float
    ) -> BytesResult:
        return check_output(mode, passed=self.passes)

    async def remove_check(self, job_id: UUID) -> None:
        self.removed.append(job_id)


class Delivery:
    """The harness. One per test."""

    def __init__(self, pool: asyncpg.Pool, tmp: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self.pool = pool
        self.hang = Hang()
        self.host = LocalHost(tmp / "github")
        self.host.root.mkdir()
        self.launcher = CommittingLauncher(tmp, self.hang)
        self.r2 = FakeR2(self.hang)
        self.events: list[tuple[UUID, Mapping[str, object]]] = []
        self.users: list[UUID] = []
        self.supervisors: list[JobSupervisor] = []
        # A NullPool engine of this test's loop: the module engine's pool keeps connections of
        # an earlier test's loop, and a second test would wait on them.
        self.engine = create_async_engine(settings.database_url, poolclass=NullPool)
        monkeypatch.setattr(
            artifact_tools,
            "AsyncSessionLocal",
            async_sessionmaker(self.engine, class_=AsyncSession, expire_on_commit=False),
        )
        monkeypatch.setattr(artifact_tools, "get_artifact_store", lambda: self.r2)
        monkeypatch.setattr(artifact_tools, "generate_embedding", _zero_embedding)
        monkeypatch.setattr(settings, "artifact_envelope_probe_enabled", False)
        monkeypatch.setattr(settings, "artifacts_public_base_url", "https://artifacts.test")

    async def notify(self, job: DevJob, written: Mapping[str, object]) -> None:
        await self.hang.point("after_row_insert")
        self.events.append((job.id, written))

    def pipeline(self, checker: CheckLauncher) -> GitHubRepoPipeline:
        return GitHubRepoPipeline(
            pool=self.pool,
            launcher=self.launcher,
            host=self.host,
            pages=None,
            checker=checker,
            notify=self.notify,
        )

    def supervisor(self, owner: str, checker: CheckLauncher) -> JobSupervisor:
        sup = JobSupervisor(
            store=JobStore(self.pool),
            launcher=self.launcher,
            adapter_factory=adapter_factory(succeed, []),  # type: ignore[arg-type]
            pipeline=self.pipeline(checker),
            clock=ManualClock(),
            owner=owner,
        )
        self.supervisors.append(sup)
        return sup

    async def owner(self) -> tuple[UUID, UUID]:
        """Insert a user and a session for the job, as the artifacts row requires."""
        user_id, session_id = uuid4(), uuid4()
        await self.pool.execute(
            "INSERT INTO users (user_id, email) VALUES ($1, $2)",
            user_id,
            f"fre1686-test-{user_id}@test.invalid",
        )
        await self.pool.execute(
            "INSERT INTO sessions (session_id, user_id) VALUES ($1, $2)", session_id, user_id
        )
        self.users.append(user_id)
        return user_id, session_id

    async def job(self, make_job, seed: Seed) -> DevJob:  # type: ignore[no-untyped-def]
        user_id, session_id = await self.owner()
        job: DevJob = await make_job(user_id=user_id, session_id=session_id)
        self.launcher.seeds[job.id] = seed
        return job

    async def row(self, job_id: UUID) -> asyncpg.Record:
        return await self.pool.fetchrow(
            "SELECT state, failure_reason, repo, result_commit, result_artifact_id, check_result "
            "FROM dev_jobs WHERE id = $1",
            job_id,
        )

    async def artifact_rows(self, user_id: UUID) -> list[asyncpg.Record]:
        return list(
            await self.pool.fetch(
                "SELECT id, r2_key, session_id, content_type, title FROM artifacts "
                "WHERE user_id = $1",
                user_id,
            )
        )

    def committed_page(self, repo: str, job_id: UUID) -> bytes:
        return self.host.blob(repo, f"{job_branch(job_id)}:dist/index.html")

    async def cleanup(self) -> None:
        for sup in self.supervisors:
            await sup.stop()
        users = self.users
        await self.pool.execute("DELETE FROM artifacts WHERE user_id = ANY($1::uuid[])", users)
        await self.pool.execute("DELETE FROM sessions WHERE user_id = ANY($1::uuid[])", users)
        await self.pool.execute("DELETE FROM users WHERE user_id = ANY($1::uuid[])", users)
        await self.engine.dispose()


async def _zero_embedding(*args: object, **kwargs: object) -> list[float]:
    return [0.0] * 1024


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def wait_end(sup: JobSupervisor, timeout: float = 60.0) -> None:
    """Wait until the supervisor's current task ends."""
    assert sup._current is not None
    await asyncio.wait_for(asyncio.shield(sup._current), timeout=timeout)
