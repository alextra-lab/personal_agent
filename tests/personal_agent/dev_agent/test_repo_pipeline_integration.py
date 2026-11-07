# ruff: noqa: D101, D102
"""The repo steps of a job against the test stack and local git repos (FRE-1685 AC-3 to AC-6).

The database is the test stack (:5433, FRE-375). ``LocalHost`` stands in for GitHub: each repo is
a local bare repo, and it records every repo creation. The test plays the agent and the handoff
container: it unpacks the workspace tar of ``prepare``, commits, and bundles the job branch.
The handoff container itself, with hostile config, is in ``test_repo_live.py``.

Apply migrations 0039 and 0040 to the test stack first (see ``conftest.py``).
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID, uuid4

import asyncpg  # type: ignore[import-untyped]
import pytest
import pytest_asyncio

from personal_agent.dev_agent.github_app import GitRemote
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.repo_pipeline import GitHubRepoPipeline, job_branch
from personal_agent.dev_agent.supervisor import Fence, SupervisorPolicy
from personal_agent.exceptions import DevJobRefusedError

from .conftest import MakeJob

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

_EMAIL_PREFIX = "fre1685-test-"


class LocalHost:
    """Repos as local bare repos. Every created repo is private."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.created: list[str] = []

    async def create_private_repo(self, name: str) -> None:
        path = self.root / f"{name}.git"
        if not path.exists():
            subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
            self.created.append(name)

    async def remote(self, name: str) -> GitRemote:
        path = self.root / f"{name}.git"
        assert path.exists(), f"no repo {name}"
        return GitRemote(url=str(path))

    async def is_private(self, name: str) -> bool:
        return name in self.created

    def ref(self, name: str, ref: str) -> str | None:
        out = subprocess.run(
            [
                "git",
                "--git-dir",
                str(self.root / f"{name}.git"),
                "rev-parse",
                "--verify",
                "-q",
                ref,
            ],
            capture_output=True,
        )
        return out.stdout.decode().strip() or None

    def blob(self, name: str, spec: str) -> bytes:
        return subprocess.run(
            ["git", "--git-dir", str(self.root / f"{name}.git"), "cat-file", "blob", spec],
            capture_output=True,
            check=True,
        ).stdout


class BundleLauncher:
    """Hands the pipeline the bundle that the test prepared, as the handoff container would."""

    def __init__(self) -> None:
        self.bundles: dict[UUID, bytes] = {}

    async def bundle_workspace(self, job_id: UUID, branch: str) -> bytes:
        assert branch == job_branch(job_id)
        return self.bundles[job_id]


class Pages:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.reads: list[str] = []

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        self.reads.append(r2_key)
        return self.data[r2_key]


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=agent", "-c", "user.email=a@a", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout.decode()


def unpack(workspace: bytes, into: Path) -> Path:
    with tarfile.open(fileobj=io.BytesIO(workspace)) as tar:
        tar.extractall(into, filter="data")
    return into


def bundle(ws: Path, branch: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ws), "bundle", "create", "-", f"refs/heads/{branch}"],
        capture_output=True,
        check=True,
    ).stdout


class Env:
    def __init__(self, pool: asyncpg.Pool, store: JobStore, tmp: Path) -> None:
        self.pool = pool
        self.store = store
        self.tmp = tmp
        self.host = LocalHost(tmp / "github")
        self.host.root.mkdir()
        self.launcher = BundleLauncher()
        self.pages = Pages()
        self.pipeline = GitHubRepoPipeline(
            pool=pool,
            launcher=self.launcher,  # type: ignore[arg-type]
            host=self.host,
            pages=self.pages,
        )
        self.users: list[UUID] = []
        self.artifacts: list[UUID] = []

    async def fence_for(self, job: DevJob) -> Fence:
        taken = await self.store.claim_new("test-owner", 120.0)
        assert taken is not None and taken[0].id == job.id
        return Fence(self.store, taken[1], SupervisorPolicy(), None)

    async def user(self) -> UUID:
        user_id = uuid4()
        await self.pool.execute(
            "INSERT INTO users (user_id, email) VALUES ($1, $2)",
            user_id,
            f"{_EMAIL_PREFIX}{user_id}@test.invalid",
        )
        self.users.append(user_id)
        return user_id

    async def artifact(self, user_id: UUID, html: bytes, slug: str = "big-page") -> UUID:
        artifact_id = uuid4()
        key = f"test/{artifact_id}.html"
        await self.pool.execute(
            """
            INSERT INTO artifacts (id, user_id, type, slug, content_type, size_bytes, r2_key,
                                   created_by)
            VALUES ($1, $2, 'artifact', $3, 'text/html; charset=utf-8', $4, $5, 'agent')
            """,
            artifact_id,
            user_id,
            slug,
            len(html),
            key,
        )
        self.pages.data[key] = html
        self.artifacts.append(artifact_id)
        return artifact_id

    async def succeed(self, job_id: UUID) -> UUID:
        """End a job as succeeded with a result artifact id, as delivery (FRE-1686) will."""
        result_artifact = uuid4()
        await self.pool.execute(
            "UPDATE dev_jobs SET state = 'succeeded', ended_at = clock_timestamp(), "
            "lease_expires_at = NULL, result_artifact_id = $2 WHERE id = $1",
            job_id,
            result_artifact,
        )
        return result_artifact

    async def row(self, job_id: UUID) -> asyncpg.Record:
        return await self.pool.fetchrow(
            "SELECT repo, base_commit, result_commit, main_moved FROM dev_jobs WHERE id = $1",
            job_id,
        )

    async def agent_commit(self, job: DevJob, workspace: bytes, page: str) -> Path:
        ws = unpack(workspace, self.tmp / f"ws-{job.id}")
        (ws / "dist" / "index.html").write_text(page)
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "page")
        self.launcher.bundles[job.id] = bundle(ws, job_branch(job.id))
        return ws


@pytest_asyncio.fixture
async def env(admin_pool: asyncpg.Pool, store: JobStore, tmp_path: Path) -> AsyncIterator[Env]:
    e = Env(admin_pool, store, tmp_path)
    yield e
    await admin_pool.execute("DELETE FROM artifacts WHERE id = ANY($1::uuid[])", e.artifacts)
    await admin_pool.execute("DELETE FROM users WHERE user_id = ANY($1::uuid[])", e.users)


async def build_and_publish(env: Env, make_job: MakeJob, page: str) -> tuple[DevJob, UUID]:
    """Run a build job through prepare, an agent commit, handoff and advance_main."""
    job = await make_job()
    fence = await env.fence_for(job)
    prepared = await env.pipeline.prepare(job, fence)
    await env.agent_commit(prepared.job, prepared.workspace or b"", page)
    result = await env.pipeline.handoff(prepared.job, fence)
    assert result is not None
    done = DevJob(**{**prepared.job.__dict__, "result_commit": result})
    await env.pipeline.advance_main(done, fence)
    return done, await env.succeed(job.id)


async def fix_and_publish(
    env: Env, make_job: MakeJob, user_id: UUID, source: UUID, page: str
) -> DevJob:
    job = await make_job(mode="fix", source_artifact_id=source, user_id=user_id)
    fence = await env.fence_for(job)
    prepared = await env.pipeline.prepare(job, fence)
    await env.agent_commit(prepared.job, prepared.workspace or b"", page)
    result = await env.pipeline.handoff(prepared.job, fence)
    assert result is not None
    done = DevJob(**{**prepared.job.__dict__, "result_commit": result})
    await env.pipeline.advance_main(done, fence)
    await env.succeed(job.id)
    return done


class TestPrepare:
    async def test_build_creates_a_private_repo_and_clones_the_template(
        self, env: Env, make_job: MakeJob
    ) -> None:
        job = await make_job()
        prepared = await env.pipeline.prepare(job, await env.fence_for(job))
        repo = prepared.job.repo
        assert repo == f"page-{job.id.hex[:8]}"
        assert env.host.created == [repo]
        assert await env.host.is_private(repo)
        assert env.host.ref(repo, "refs/heads/main") == prepared.job.base_commit
        row = await env.row(job.id)
        assert (row["repo"], row["base_commit"]) == (repo, prepared.job.base_commit)
        ws = unpack(prepared.workspace or b"", env.tmp / "ws")
        assert git(ws, "rev-parse", "HEAD").strip() == prepared.job.base_commit
        assert git(ws, "symbolic-ref", "HEAD").strip() == f"refs/heads/{job_branch(job.id)}"
        assert (ws / "AGENTS.md").exists()

    async def test_large_page_import_hash(self, env: Env, make_job: MakeJob) -> None:
        """AC: a non-agent page over 256 KB imports, with equal SHA-256 at the base commit."""
        user = await env.user()
        html = b"<!doctype html><html><body>" + b"<p>row</p>" * 40_000 + b"</body></html>"
        assert len(html) > 256 * 1024
        source = await env.artifact(user, html, slug="Big Page")
        job = await make_job(mode="fix", source_artifact_id=source, user_id=user)
        prepared = await env.pipeline.prepare(job, await env.fence_for(job))
        repo, base = prepared.job.repo, prepared.job.base_commit
        assert repo == f"big-page-{job.id.hex[:8]}"
        stored = env.host.blob(repo or "", f"{base}:dist/index.html")
        assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(html).hexdigest()
        assert env.host.ref(repo or "", "refs/heads/main") == base

    async def test_other_users_artifact_creates_no_repo(self, env: Env, make_job: MakeJob) -> None:
        """AC: a fix request with another user's artifact id creates no repo."""
        owner, other = await env.user(), await env.user()
        source = await env.artifact(owner, b"<p>owner's page</p>")
        job = await make_job(mode="fix", source_artifact_id=source, user_id=other)
        with pytest.raises(DevJobRefusedError) as refused:
            await env.pipeline.prepare(job, await env.fence_for(job))
        assert refused.value.reason == "source_not_found"
        assert env.host.created == []
        assert env.pages.reads == [], "the other user's page bytes are never read"
        assert (await env.row(job.id))["repo"] is None

    async def test_unknown_artifact_creates_no_repo(self, env: Env, make_job: MakeJob) -> None:
        job = await make_job(mode="fix", source_artifact_id=uuid4(), user_id=await env.user())
        with pytest.raises(DevJobRefusedError, match="source_not_found"):
            await env.pipeline.prepare(job, await env.fence_for(job))
        assert env.host.created == []

    async def test_a_fix_of_an_agent_page_continues_its_repo(
        self, env: Env, make_job: MakeJob
    ) -> None:
        user = await env.user()
        first, artifact = await build_and_publish(env, make_job, "<p>v1</p>")
        await env.pool.execute("UPDATE dev_jobs SET user_id = $2 WHERE id = $1", first.id, user)
        job = await make_job(mode="fix", source_artifact_id=artifact, user_id=user)
        prepared = await env.pipeline.prepare(job, await env.fence_for(job))
        assert prepared.job.repo == first.repo
        assert prepared.job.base_commit == first.result_commit
        assert env.host.created == [first.repo], "no second repo"
        ws = unpack(prepared.workspace or b"", env.tmp / "ws2")
        assert (ws / "dist" / "index.html").read_text() == "<p>v1</p>"

    async def test_another_users_agent_page_is_not_continued(
        self, env: Env, make_job: MakeJob
    ) -> None:
        _, artifact = await build_and_publish(env, make_job, "<p>v1</p>")
        job = await make_job(mode="fix", source_artifact_id=artifact, user_id=await env.user())
        with pytest.raises(DevJobRefusedError, match="source_not_found"):
            await env.pipeline.prepare(job, await env.fence_for(job))


class TestHandoff:
    async def test_the_result_is_pushed_to_the_job_branch_only(
        self, env: Env, make_job: MakeJob
    ) -> None:
        job = await make_job()
        fence = await env.fence_for(job)
        prepared = await env.pipeline.prepare(job, fence)
        ws = await env.agent_commit(prepared.job, prepared.workspace or b"", "<p>page</p>")
        # The agent also rewrites main in its workspace. Only the job branch leaves it.
        git(ws, "checkout", "-q", "--orphan", "evil")
        git(ws, "commit", "-q", "--allow-empty", "-m", "evil")
        git(ws, "branch", "-f", "main", "evil")
        repo = prepared.job.repo or ""
        result = await env.pipeline.handoff(prepared.job, fence)
        assert result == env.host.ref(repo, f"refs/heads/{job_branch(job.id)}")
        assert env.host.ref(repo, "refs/heads/main") == prepared.job.base_commit
        assert (await env.row(job.id))["result_commit"] == result

    async def test_non_descendant_head_is_not_pushed(self, env: Env, make_job: MakeJob) -> None:
        """AC: a head that does not descend from the base is rejected and not pushed."""
        job = await make_job()
        fence = await env.fence_for(job)
        prepared = await env.pipeline.prepare(job, fence)
        ws = unpack(prepared.workspace or b"", env.tmp / "ws")
        git(ws, "checkout", "-q", "--orphan", "other")
        (ws / "dist" / "index.html").write_text("<p>page</p>")
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "orphan")
        git(ws, "branch", "-f", job_branch(job.id), "HEAD")
        env.launcher.bundles[job.id] = bundle(ws, job_branch(job.id))
        with pytest.raises(DevJobRefusedError) as refused:
            await env.pipeline.handoff(prepared.job, fence)
        assert refused.value.reason == "head_not_descendant"
        repo = prepared.job.repo or ""
        assert env.host.ref(repo, f"refs/heads/{job_branch(job.id)}") is None
        assert (await env.row(job.id))["result_commit"] is None

    async def test_no_commit_is_no_result(self, env: Env, make_job: MakeJob) -> None:
        job = await make_job()
        fence = await env.fence_for(job)
        prepared = await env.pipeline.prepare(job, fence)
        ws = unpack(prepared.workspace or b"", env.tmp / "ws")
        env.launcher.bundles[job.id] = bundle(ws, job_branch(job.id))
        assert await env.pipeline.handoff(prepared.job, fence) is None

    async def test_a_result_without_the_page_is_refused(self, env: Env, make_job: MakeJob) -> None:
        job = await make_job()
        fence = await env.fence_for(job)
        prepared = await env.pipeline.prepare(job, fence)
        ws = unpack(prepared.workspace or b"", env.tmp / "ws")
        (ws / "notes.md").write_text("no page")
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "notes")
        env.launcher.bundles[job.id] = bundle(ws, job_branch(job.id))
        with pytest.raises(DevJobRefusedError, match="page_missing"):
            await env.pipeline.handoff(prepared.job, fence)


class TestAdvanceMain:
    async def test_a_build_moves_main(self, env: Env, make_job: MakeJob) -> None:
        job, _ = await build_and_publish(env, make_job, "<p>v1</p>")
        assert env.host.ref(job.repo or "", "refs/heads/main") == job.result_commit
        assert (await env.row(job.id))["main_moved"] is True

    async def test_older_version_fix_leaves_main(self, env: Env, make_job: MakeJob) -> None:
        """AC: a fix from an older version, while main holds a later fix, leaves main unchanged."""
        user = await env.user()
        first, v1 = await build_and_publish(env, make_job, "<p>v1</p>")
        await env.pool.execute("UPDATE dev_jobs SET user_id = $2 WHERE id = $1", first.id, user)
        later = await fix_and_publish(env, make_job, user, v1, "<p>v2</p>")
        repo = first.repo or ""
        assert env.host.ref(repo, "refs/heads/main") == later.result_commit
        older = await fix_and_publish(env, make_job, user, v1, "<p>v1 fixed another way</p>")
        assert env.host.ref(repo, "refs/heads/main") == later.result_commit
        assert (await env.row(older.id))["main_moved"] is False
        assert env.host.ref(repo, f"refs/heads/{job_branch(older.id)}") == older.result_commit
