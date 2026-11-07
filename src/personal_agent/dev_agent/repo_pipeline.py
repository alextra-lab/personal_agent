"""The repo steps of a dev-agent job: prepare, handoff and advance_main (ADR-0159 D8; FRE-1685).

* :meth:`GitHubRepoPipeline.prepare` finds the base commit and the repo, and returns a tar of a
  clone of the base on ``job/<job id>`` for the workspace (D8.4–D8.7):

  - build: a new private repo whose ``main`` is the template commit;
  - fix of a page that the agent built: that page's repo, at the source job's result commit;
  - fix of another page: an owner-scoped lookup of the artifact, then a new private repo whose
    ``main`` is the template commit plus the page as ``dist/index.html``.

  The lookup runs before any GitHub call, so a refused source creates no repo.

* :meth:`GitHubRepoPipeline.handoff` takes the bundle that the handoff container wrote, checks it
  in a clean mirror, and pushes ``job/<job id>`` with the gateway's token (D8.8.3, D8.8.4).

* :meth:`GitHubRepoPipeline.advance_main` fast-forwards ``main`` to the result after the publish,
  only when the result descends from it (D8.9).

Every GitHub write, every container run and every write of a repo fact goes through ``fence.run``.
The publish check and delivery are FRE-1686.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Protocol
from uuid import UUID

import asyncpg  # type: ignore[import-untyped]
import structlog

from personal_agent.dev_agent.git_mirror import MAX_PAGE_BYTES, GitMirror
from personal_agent.dev_agent.github_app import GitRemote, RepoHost, repo_name
from personal_agent.dev_agent.jobs import DevJob, JobStore
from personal_agent.dev_agent.launcher import AgentLauncher
from personal_agent.dev_agent.supervisor import Fence, PreparedJob
from personal_agent.exceptions import DevAgentLaunchError, DevAgentRepoError, DevJobRefusedError

log = structlog.get_logger(__name__)

#: The page template, resolved from the package and not from the working directory.
DEFAULT_TEMPLATE_DIR: Path = (
    Path(__file__).resolve().parents[3] / "config" / "dev_agent" / "page_template"
)
MAIN = "refs/heads/main"

MirrorFactory = Callable[[], AbstractAsyncContextManager[GitMirror]]


class PageReader(Protocol):
    """Reads an artifact's bytes. :class:`~personal_agent.storage.artifact_store.R2ArtifactStore`."""

    async def get(self, r2_key: str, *, trace_id: str | None = None) -> bytes:
        """Return the bytes at ``r2_key``."""
        ...


def job_branch(job_id: UUID) -> str:
    """Return the job's branch name (D8.4).

    Args:
        job_id: The job.

    Returns:
        ``job/<job id>``.
    """
    return f"job/{job_id}"


@dataclass(frozen=True)
class _AgentSource:
    """A page that an earlier job built: continue its repo from its result commit (D8.6)."""

    job_id: UUID
    repo: str
    commit: str


@dataclass(frozen=True)
class _PageSource:
    """A page that the agent did not build: import it into a new repo (D8.7)."""

    slug: str | None
    html: bytes


def _open_mirror() -> AbstractAsyncContextManager[GitMirror]:
    return GitMirror.open()


class GitHubRepoPipeline:
    """The FRE-1685 steps of :class:`~personal_agent.dev_agent.supervisor.JobResultPipeline`."""

    def __init__(
        self,
        *,
        pool: asyncpg.Pool,
        launcher: AgentLauncher,
        host: RepoHost,
        pages: PageReader | None,
        template_dir: Path = DEFAULT_TEMPLATE_DIR,
        mirror: MirrorFactory = _open_mirror,
    ) -> None:
        """Create the pipeline.

        Args:
            pool: The gateway's pool, for the owner-scoped source lookup.
            launcher: Runs the handoff container.
            host: The repo host (the GitHub App).
            pages: Reads artifact bytes from R2. ``None`` refuses a fix of a non-agent page.
            template_dir: The page template.
            mirror: Opens a clean mirror. Tests pass a recording one.
        """
        self._pool = pool
        self._launcher = launcher
        self._host = host
        self._pages = pages
        self._template = template_dir
        self._mirror = mirror

    async def _resolve_source(self, job: DevJob) -> _AgentSource | _PageSource:
        """Find the fix source, owned by the job's user (D8.6, D8.7).

        Raises:
            DevJobRefusedError: ``source_not_found`` when no page of the user matches,
                ``artifact_store_unavailable`` without R2, ``source_too_large`` over 5 MB.
        """
        source_id = job.source_artifact_id
        if source_id is None:
            raise DevJobRefusedError("source_not_found")
        built = await self._pool.fetchrow(
            """
            SELECT id, repo, result_commit FROM dev_jobs
             WHERE result_artifact_id = $1 AND user_id = $2 AND state = 'succeeded'
               AND repo IS NOT NULL AND result_commit IS NOT NULL
             ORDER BY ended_at DESC LIMIT 1
            """,
            source_id,
            job.user_id,
        )
        if built is not None:
            return _AgentSource(
                job_id=built["id"], repo=built["repo"], commit=built["result_commit"]
            )
        row = await self._pool.fetchrow(
            """
            SELECT slug, r2_key FROM artifacts
             WHERE id = $1 AND user_id = $2 AND type = 'artifact' AND upload_pending = FALSE
               AND (content_type = 'text/html' OR content_type LIKE 'text/html;%')
               AND size_bytes <= $3
            """,
            source_id,
            job.user_id,
            MAX_PAGE_BYTES,
        )
        if row is None:
            raise DevJobRefusedError("source_not_found")
        if self._pages is None:
            raise DevJobRefusedError("artifact_store_unavailable")
        html = await self._pages.get(row["r2_key"], trace_id=job.trace_id.hex)
        if len(html) > MAX_PAGE_BYTES:
            raise DevJobRefusedError("source_too_large")
        return _PageSource(slug=row["slug"], html=html)

    async def prepare(self, job: DevJob, fence: Fence) -> PreparedJob:
        """Make the repo, store the base, and return the workspace tar (D8.4–D8.7).

        Args:
            job: The job.
            fence: The fence of the job's lease.

        Returns:
            The job with ``repo`` and ``base_commit``, and the workspace tar.

        Raises:
            DevJobRefusedError: If the fix source is refused, its repo is no longer private, or its
                commit is not in its repo.
        """
        source = None if job.mode == "build" else await self._resolve_source(job)
        branch = job_branch(job.id)
        if isinstance(source, _AgentSource):
            repo, base = source.repo, source.commit
            # D8.2 holds for every write, not only at creation: refuse a repo that is no longer
            # private.
            if not await self._host.is_private(repo):
                raise DevJobRefusedError("repo_not_private")
            remote = await self._host.remote(repo)
            async with self._mirror() as mirror:
                head = await mirror.fetch(remote, f"refs/heads/{job_branch(source.job_id)}")
                if head is None or not await mirror.is_ancestor(base, head):
                    raise DevJobRefusedError("source_commit_missing")

                async def record_base(conn: asyncpg.Connection) -> None:
                    await JobStore.record(conn, fence.lease, repo=repo, base_commit=base)

                await fence.run("record_base", record_base)
                workspace = await mirror.workspace_tar(base, branch)
        else:
            repo = repo_name(source.slug if source else None, job.id)

            async def create(conn: asyncpg.Connection) -> None:
                await self._host.create_private_repo(repo)
                await JobStore.record(conn, fence.lease, repo=repo)

            await fence.run("create_repo", create)
            async with self._mirror() as mirror:
                base = await mirror.commit_template(self._template)
                if source is not None:
                    base = await mirror.commit_page(base, source.html)
                remote = await self._host.remote(repo)
                pushed = base

                async def push_base(conn: asyncpg.Connection) -> None:
                    await mirror.push(remote, pushed, MAIN)
                    await JobStore.record(conn, fence.lease, base_commit=pushed)

                await fence.run("push_base", push_base)
                workspace = await mirror.workspace_tar(base, branch)
        log.info(
            "dev_job_prepared",
            job_id=str(job.id),
            repo=repo,
            base_commit=base,
            source="agent" if isinstance(source, _AgentSource) else "page" if source else "none",
            trace_id=job.trace_id.hex,
        )
        return PreparedJob(job=replace(job, repo=repo, base_commit=base), workspace=workspace)

    async def handoff(self, job: DevJob, fence: Fence) -> str | None:
        """Bundle, check and push the agent's branch (D8.8.2–D8.8.4).

        Args:
            job: The job, with ``repo`` and ``base_commit``.
            fence: The fence of the job's lease.

        Returns:
            The result commit, or ``None`` when the agent committed nothing.

        Raises:
            DevJobRefusedError: ``handoff_failed`` (no bundle), ``bundle_invalid``,
                ``head_not_descendant``, ``tree_has_link``, ``page_missing``, ``page_too_large`` or
                ``result_push_failed``.
        """
        if job.repo is None or job.base_commit is None:
            raise DevJobRefusedError("no_base")
        repo, base, branch = job.repo, job.base_commit, job_branch(job.id)

        async def bundle_it(conn: asyncpg.Connection) -> bytes:
            del conn
            return await self._launcher.bundle_workspace(job.id, branch)

        try:
            bundle = await fence.run("handoff_bundle", bundle_it)
        except (DevAgentLaunchError, TimeoutError) as exc:
            # TimeoutError: the fence's action limit cut the handoff (code review finding 2).
            log.warning(
                "dev_job_handoff_failed",
                job_id=str(job.id),
                error=str(exc)[:300],
                trace_id=job.trace_id.hex,
            )
            raise DevJobRefusedError("handoff_failed") from exc
        async with self._mirror() as mirror:
            try:
                head = await mirror.fetch_bundle(bundle, branch)
            except DevAgentRepoError as exc:
                raise DevJobRefusedError("bundle_invalid") from exc
            if head == base:
                return None
            if not await mirror.is_ancestor(base, head):
                raise DevJobRefusedError("head_not_descendant")
            try:
                problem = await mirror.result_problem(head)
                if problem is not None:
                    raise DevJobRefusedError(problem)
                remote = await self._host.remote(repo)

                async def push_result(conn: asyncpg.Connection) -> None:
                    await mirror.push(remote, head, f"refs/heads/{branch}")
                    await JobStore.record(conn, fence.lease, result_commit=head)

                await fence.run("push_result", push_result)
            except (DevAgentRepoError, TimeoutError) as exc:
                # A git or GitHub failure keeps a stable reason, so the supervisor can still end
                # the job as timed_out when the limit passed (code review finding 4).
                raise DevJobRefusedError("result_push_failed") from exc
        log.info(
            "dev_job_result_pushed",
            job_id=str(job.id),
            repo=repo,
            result_commit=head,
            trace_id=job.trace_id.hex,
        )
        return head

    async def check(self, job: DevJob, result_commit: str, fence: Fence) -> bool:
        """The publish check (D7.3–D7.6) arrives with FRE-1686.

        Raises:
            NotImplementedError: Always. The supervisor ends the job ``failed``. It is off by
                default until FRE-1686 lands.
        """
        raise NotImplementedError("the publish check arrives with FRE-1686")

    async def deliver(self, job: DevJob, fence: Fence) -> None:
        """Delivery (D9.8–D9.10) arrives with FRE-1686.

        Raises:
            NotImplementedError: Always.
        """
        raise NotImplementedError("delivery arrives with FRE-1686")

    async def advance_main(self, job: DevJob, fence: Fence) -> None:
        """Fast-forward ``main`` to the result, only if the result descends from it (D8.9).

        The job row gets ``main_moved``: ``true`` when ``main`` is at the result, ``false`` when
        ``main`` is not an ancestor of the result.

        Args:
            job: The job, with ``repo`` and ``result_commit``.
            fence: The fence of the job's lease.

        Raises:
            DevAgentRepoError: If the outcome is unknown (the push and the re-read failed). The
                column stays ``NULL``.
        """
        if job.repo is None or job.result_commit is None:
            return
        repo, result = job.repo, job.result_commit
        remote = await self._host.remote(repo)

        async def advance(conn: asyncpg.Connection) -> None:
            async with self._mirror() as mirror:
                head = await mirror.fetch(remote, f"refs/heads/{job_branch(job.id)}")
                if head != result:
                    raise DevAgentRepoError("the job branch is not at the result commit")
                moved = await fast_forward_main(mirror, remote, result)
            await JobStore.record(conn, fence.lease, main_moved=moved)
            log.info(
                "dev_job_main_advanced" if moved else "dev_job_main_not_moved",
                job_id=str(job.id),
                repo=repo,
                result_commit=result,
                trace_id=job.trace_id.hex,
            )

        await fence.run("advance_main", advance)


async def fast_forward_main(mirror: GitMirror, remote: GitRemote, result: str) -> bool:
    """Return whether ``main`` is at ``result`` after a fast-forward try (D8.9).

    The push is never forced, so GitHub refuses it if ``main`` moved since the fetch. After a
    failed push, ``main`` is read again: the push may have landed with a lost answer.

    Args:
        mirror: A mirror that holds ``result``.
        remote: The repo and its credential.
        result: The result commit.

    Returns:
        ``True`` when ``main`` is at ``result``, ``False`` when ``main`` is not its ancestor.

    Raises:
        DevAgentRepoError: If the outcome is unknown.
    """
    main = await mirror.fetch(remote, MAIN)
    if main == result:
        return True
    if main is not None and not await mirror.is_ancestor(main, result):
        return False
    try:
        await mirror.push(remote, result, MAIN)
        return True
    except DevAgentRepoError:
        again = await mirror.fetch(remote, MAIN)
        if again == result:
            return True
        if again is not None and not await mirror.is_ancestor(again, result):
            return False
        raise
