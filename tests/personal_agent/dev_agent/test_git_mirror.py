"""The gateway's clean git mirror (ADR-0159 D8.4, D8.5, D8.7–D8.9; FRE-1685).

Real git on local bare repos. A local path stands in for the GitHub remote. No network.
"""

from __future__ import annotations

import hashlib
import io
import subprocess
import tarfile
from collections.abc import Mapping, Sequence
from pathlib import Path

import pytest
from personal_agent.dev_agent.git_mirror import GitMirror

from personal_agent.dev_agent.github_app import GitRemote
from personal_agent.dev_agent.launcher import BytesResult, run_bytes
from personal_agent.exceptions import DevAgentRepoError

pytestmark = pytest.mark.asyncio

_BRANCH = "job/0b5e2a1c-0000-4000-8000-000000000000"


def git(cwd: Path, *args: str, stdin: bytes | None = None) -> str:
    """Run plain git in a test repo, with a fixed identity."""
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=cwd,
        input=stdin,
        capture_output=True,
        check=True,
    ).stdout.decode()


@pytest.fixture
def template(tmp_path: Path) -> Path:
    root = tmp_path / "template"
    (root / "dist").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "AGENTS.md").write_text("loop\n")
    (root / "dist" / ".gitkeep").write_text("")
    (root / "tests" / ".gitkeep").write_text("")
    return root


@pytest.fixture
def remote(tmp_path: Path) -> GitRemote:
    path = tmp_path / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(path)], check=True)
    return GitRemote(url=str(path))


def remote_ref(remote: GitRemote, ref: str) -> str | None:
    out = subprocess.run(
        ["git", "--git-dir", remote.url, "rev-parse", "--verify", "--quiet", ref],
        capture_output=True,
    )
    return out.stdout.decode().strip() or None


def workspace_from(remote: GitRemote, tmp_path: Path, base: str, name: str = "ws") -> Path:
    """An agent's workspace: a clone of the base on the job branch."""
    ws = tmp_path / name
    subprocess.run(["git", "clone", "-q", remote.url, str(ws)], check=True)
    git(ws, "checkout", "-q", "-b", _BRANCH, base)
    return ws


def bundle_of(ws: Path, branch: str = _BRANCH) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ws), "bundle", "create", "-", f"refs/heads/{branch}"],
        capture_output=True,
        check=True,
    ).stdout


async def base_on_remote(template: Path, remote: GitRemote) -> str:
    async with GitMirror.open() as mirror:
        base = await mirror.commit_template(template)
        await mirror.push(remote, base, "refs/heads/main")
    return base


class TestBaseCommits:
    async def test_template_commit_holds_exactly_the_template(
        self, template: Path, remote: GitRemote
    ) -> None:
        base = await base_on_remote(template, remote)
        assert remote_ref(remote, "refs/heads/main") == base
        files = subprocess.run(
            ["git", "--git-dir", remote.url, "ls-tree", "-r", "--name-only", base],
            capture_output=True,
            check=True,
        ).stdout.decode()
        assert files.split() == ["AGENTS.md", "dist/.gitkeep", "tests/.gitkeep"]

    async def test_page_commit_holds_the_exact_bytes(
        self, template: Path, remote: GitRemote
    ) -> None:
        page = b"<!doctype html><title>x</title>" + b"x" * 300_000
        async with GitMirror.open() as mirror:
            parent = await mirror.commit_template(template)
            base = await mirror.commit_page(parent, page)
            await mirror.push(remote, base, "refs/heads/main")
            assert await mirror.read_page(base) == page
        stored = subprocess.run(
            ["git", "--git-dir", remote.url, "cat-file", "blob", f"{base}:dist/index.html"],
            capture_output=True,
            check=True,
        ).stdout
        assert hashlib.sha256(stored).hexdigest() == hashlib.sha256(page).hexdigest()
        parents = git(Path(remote.url), "rev-list", "--parents", "-n1", base).split()
        assert parents[1:] == [parent]


class TestHandoffChecks:
    async def test_agent_commit_on_the_base_passes(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        ws = workspace_from(remote, tmp_path, base)
        (ws / "dist" / "index.html").write_text("<p>page</p>")
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "page")
        async with GitMirror.open() as mirror:
            head = await mirror.fetch_bundle(bundle_of(ws), _BRANCH)
            assert head == git(ws, "rev-parse", "HEAD").strip()
            assert await mirror.is_ancestor(base, head) is True
            assert await mirror.result_problem(head) is None

    async def test_orphan_head_does_not_descend(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        ws = workspace_from(remote, tmp_path, base)
        git(ws, "checkout", "-q", "--orphan", "other")
        (ws / "dist" / "index.html").write_text("<p>page</p>")
        git(ws, "add", "-A")
        git(ws, "commit", "-qm", "orphan")
        git(ws, "branch", "-f", _BRANCH, "HEAD")
        async with GitMirror.open() as mirror:
            head = await mirror.fetch_bundle(bundle_of(ws), _BRANCH)
            # The base object is not in this mirror at all: that is "not a descendant" too.
            assert await mirror.is_ancestor(base, head) is False

    @pytest.mark.parametrize(
        ("setup", "problem"),
        [
            ("missing", "page_missing"),
            ("directory", "page_missing"),
            ("symlink", "tree_has_link"),
            ("submodule", "tree_has_link"),
            ("submodule-page", "tree_has_link"),
            ("symlinked-dist", "tree_has_link"),
            ("large", "page_too_large"),
        ],
    )
    async def test_result_tree_problems(
        self, template: Path, remote: GitRemote, tmp_path: Path, setup: str, problem: str
    ) -> None:
        base = await base_on_remote(template, remote)
        ws = workspace_from(remote, tmp_path, base)
        dist = ws / "dist"
        if setup == "directory":
            (dist / "index.html").mkdir()
            (dist / "index.html" / "x").write_text("x")
        elif setup == "symlink":
            (dist / "index.html").write_text("<p>page</p>")
            (ws / "tests" / "link").symlink_to("/etc/passwd")
        elif setup in ("submodule", "submodule-page"):
            (dist / "index.html").write_text("<p>page</p>")
        elif setup == "symlinked-dist":
            (ws / "real").mkdir()
            (ws / "real" / "index.html").write_text("<p>page</p>")
            (dist / ".gitkeep").unlink()
            dist.rmdir()
            dist.symlink_to("real")
        elif setup == "large":
            (dist / "index.html").write_bytes(b"x" * (5 * 1024 * 1024 + 1))
        else:
            (ws / "AGENTS.md").write_text("changed\n")
        git(ws, "add", "-A")
        if setup == "submodule":
            git(ws, "update-index", "--add", "--cacheinfo", f"160000,{base},vendor")
        elif setup == "submodule-page":
            git(ws, "update-index", "--cacheinfo", f"160000,{base},{'dist/index.html'}")
        git(ws, "commit", "-qm", setup)
        async with GitMirror.open() as mirror:
            head = await mirror.fetch_bundle(bundle_of(ws), _BRANCH)
            assert await mirror.result_problem(head) == problem

    async def test_bundle_without_the_branch_is_refused(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        ws = workspace_from(remote, tmp_path, base)
        git(ws, "branch", "other", base)
        async with GitMirror.open() as mirror:
            with pytest.raises(DevAgentRepoError):
                await mirror.fetch_bundle(bundle_of(ws, "other"), _BRANCH)

    async def test_head_that_is_not_a_commit_is_refused(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        ws = workspace_from(remote, tmp_path, base)
        tree = git(ws, "rev-parse", f"{base}^{{tree}}").strip()
        (ws / ".git" / "refs" / "heads" / "job" / _BRANCH.split("/", 1)[1]).write_text(tree + "\n")
        async with GitMirror.open() as mirror:
            with pytest.raises(DevAgentRepoError):
                await mirror.fetch_bundle(bundle_of(ws), _BRANCH)

    async def test_missing_base_object_is_no_ancestor(self, template: Path) -> None:
        async with GitMirror.open() as mirror:
            head = await mirror.commit_template(template)
            assert await mirror.is_ancestor("0" * 40, head) is False

    async def test_garbage_bundle_is_refused(self) -> None:
        async with GitMirror.open() as mirror:
            with pytest.raises(DevAgentRepoError):
                await mirror.fetch_bundle(b"# v2 git bundle\nnot a bundle", _BRANCH)


class TestPush:
    async def test_non_fast_forward_push_is_refused(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        async with GitMirror.open() as mirror:
            other = await mirror.commit_template(tmp_path / "template", message="unrelated")
            with pytest.raises(DevAgentRepoError):
                await mirror.push(remote, other, "refs/heads/main")
        assert remote_ref(remote, "refs/heads/main") == base

    async def test_fetch_returns_the_remote_head(self, template: Path, remote: GitRemote) -> None:
        base = await base_on_remote(template, remote)
        async with GitMirror.open() as mirror:
            assert await mirror.fetch(remote, "refs/heads/main") == base
            assert await mirror.fetch(remote, "refs/heads/nothing") is None

    async def test_remote_env_reaches_git_only_through_the_environment(
        self, template: Path, remote: GitRemote
    ) -> None:
        seen: list[tuple[list[str], dict[str, str]]] = []

        async def recording(
            args: Sequence[str],
            *,
            env: Mapping[str, str] | None = None,
            stdin: bytes | None = None,
            timeout: float = 60.0,
            max_stdout: int = 1 << 26,
        ) -> BytesResult:
            seen.append((list(args), dict(env or {})))
            return await run_bytes(
                args, env=env, stdin=stdin, timeout=timeout, max_stdout=max_stdout
            )

        secret = GitRemote(url=remote.url, env={"GIT_CONFIG_COUNT": "0", "SECRET_MARK": "s3cr3t"})
        async with GitMirror.open(runner=recording) as mirror:
            base = await mirror.commit_template(template)
            await mirror.push(secret, base, "refs/heads/main")
        push_args, push_env = seen[-1]
        assert "s3cr3t" not in " ".join(push_args)
        assert push_env["SECRET_MARK"] == "s3cr3t"
        # The mirror's own git never sees the remote's credential.
        assert all("SECRET_MARK" not in env for _, env in seen[:-1])


class TestWorkspaceTar:
    async def test_tar_is_a_clone_on_the_job_branch_owned_by_the_agent(
        self, template: Path, remote: GitRemote, tmp_path: Path
    ) -> None:
        base = await base_on_remote(template, remote)
        async with GitMirror.open() as mirror:
            assert await mirror.fetch(remote, "refs/heads/main") == base
            data = await mirror.workspace_tar(base, _BRANCH)
        with tarfile.open(fileobj=io.BytesIO(data)) as tar:
            members = tar.getmembers()
            assert {(m.uid, m.gid) for m in members} == {(10001, 10001)}
            assert all(m.isfile() or m.isdir() for m in members)
            assert "./AGENTS.md" in {m.name for m in members}
            out = tmp_path / "out"
            tar.extractall(out, filter="data")
        assert git(out, "rev-parse", "HEAD").strip() == base
        assert git(out, "symbolic-ref", "HEAD").strip() == f"refs/heads/{_BRANCH}"
        assert git(out, "remote").strip() == ""
        assert git(out, "status", "--porcelain").strip() == ""
        assert git(out, "config", "--local", "user.name").strip() == "Seshat dev agent"


class _FakeMirror:
    """A mirror whose remote main the test scripts. Ancestry: every commit in ``ancestors``."""

    def __init__(self, mains: list[str | None], push_fails: bool, ancestors: set[str]) -> None:
        self.mains = mains
        self.push_fails = push_fails
        self.ancestors = ancestors
        self.pushes = 0

    async def fetch(self, remote: GitRemote, ref: str) -> str | None:
        return self.mains.pop(0)

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        return ancestor in self.ancestors

    async def push(self, remote: GitRemote, commit: str, ref: str) -> None:
        self.pushes += 1
        if self.push_fails:
            raise DevAgentRepoError("seeded: the answer was lost")


class TestFastForwardMain:
    """Codex plan finding 2: an unclear push is decided by reading main again."""

    R, OLD, OTHER = "r" * 40, "o" * 40, "x" * 40

    async def _ff(self, mirror: _FakeMirror) -> bool:
        from personal_agent.dev_agent.repo_pipeline import fast_forward_main

        return await fast_forward_main(mirror, GitRemote(url="/x"), self.R)  # type: ignore[arg-type]

    async def test_main_at_the_result_is_moved_without_a_push(self) -> None:
        mirror = _FakeMirror([self.R], push_fails=False, ancestors=set())
        assert await self._ff(mirror) is True
        assert mirror.pushes == 0

    async def test_main_not_an_ancestor_is_not_moved_and_not_pushed(self) -> None:
        mirror = _FakeMirror([self.OTHER], push_fails=False, ancestors=set())
        assert await self._ff(mirror) is False
        assert mirror.pushes == 0

    async def test_a_lost_answer_whose_push_landed_counts_as_moved(self) -> None:
        mirror = _FakeMirror([self.OLD, self.R], push_fails=True, ancestors={self.OLD})
        assert await self._ff(mirror) is True

    async def test_a_refused_push_after_main_moved_elsewhere_is_not_moved(self) -> None:
        mirror = _FakeMirror([self.OLD, self.OTHER], push_fails=True, ancestors={self.OLD})
        assert await self._ff(mirror) is False

    async def test_an_unknown_outcome_raises(self) -> None:
        mirror = _FakeMirror([self.OLD, self.OLD], push_fails=True, ancestors={self.OLD})
        with pytest.raises(DevAgentRepoError):
            await self._ff(mirror)
