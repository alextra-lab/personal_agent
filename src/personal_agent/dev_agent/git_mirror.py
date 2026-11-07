"""The gateway's own clean git mirror (ADR-0159 D8.4–D8.9; FRE-1685).

The gateway never runs git on the agent's workspace (D8.8.2). It runs git only here: in a new bare
repo in a private temp dir, made for one operation and deleted after it. The data that enters it
is the gateway's own (the template, an imported page), GitHub's, or a bundle that the handoff
container wrote.

Every git process runs with a fixed environment (no system or global config, no prompt) and with
``-c`` overrides that disable hooks, the fsmonitor and every transport other than HTTPS and local
files, and that make git check every object it receives (``transfer.fsckObjects``).

A remote's credential (:class:`~personal_agent.dev_agent.github_app.GitRemote`) reaches only the
one git process that talks to that remote, through its environment.
"""

from __future__ import annotations

import asyncio
import io
import os
import re
import shutil
import tarfile
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from pathlib import Path

import structlog

from personal_agent.dev_agent.github_app import GitRemote
from personal_agent.dev_agent.launcher import (
    MAX_BUNDLE_BYTES,
    BytesResult,
    BytesRunner,
    run_bytes,
)
from personal_agent.exceptions import DevAgentRepoError

log = structlog.get_logger(__name__)

#: The page that v0 publishes (D1.3), and its size cap (the artifact cap).
PAGE_PATH = "dist/index.html"
MAX_PAGE_BYTES = 5 * 1024 * 1024
#: The owner of the workspace files: the agent container's user (container_profiles.yaml).
WORKSPACE_UID = 10001
WORKSPACE_GID = 10001
GIT_TIMEOUT_SECONDS = 120.0

_HARDENING: tuple[str, ...] = (
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.fsmonitor=false",
    "-c", "core.askPass=",
    "-c", "credential.helper=",
    "-c", "protocol.allow=never",
    "-c", "protocol.https.allow=always",
    "-c", "protocol.file.allow=always",
    "-c", "transfer.fsckObjects=true",
    "-c", "gc.auto=0",
    "-c", "advice.detachedHead=false",
)  # fmt: skip
_IDENTITY = {
    "GIT_AUTHOR_NAME": "Seshat dev agent",
    "GIT_AUTHOR_EMAIL": "dev-agent@seshat.invalid",
    "GIT_COMMITTER_NAME": "Seshat dev agent",
    "GIT_COMMITTER_EMAIL": "dev-agent@seshat.invalid",
}
_SHA = re.compile(r"^[0-9a-f]{40}$")
_REF = re.compile(r"^refs/[A-Za-z0-9._/-]+$")
_BRANCH = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
#: Tree entry modes that are a plain file or a folder. A link (120000) or a submodule (160000)
#: is refused anywhere in a result tree, so a later checkout of it writes plain files only.
_PLAIN_MODES = frozenset({"100644", "100755", "040000"})
_PAGE_MODES = frozenset({"100644", "100755"})


def _check_sha(value: str) -> str:
    if not _SHA.match(value):
        raise DevAgentRepoError(f"not a commit id: {value[:60]!r}")
    return value


def _check_ref(ref: str) -> str:
    if not _REF.match(ref) or ".." in ref or ref.endswith(("/", ".lock")):
        raise ValueError(f"{ref!r} is not a plain ref name")
    return ref


def _check_branch(branch: str) -> str:
    if not _BRANCH.match(branch):
        raise ValueError(f"{branch!r} is not a plain branch name")
    _check_ref(f"refs/heads/{branch}")
    return branch


def _check_url(url: str) -> str:
    if url.startswith("-") or not url:
        raise ValueError("a remote URL must not start with '-'")
    return url


class GitMirror:
    """One bare repo in a private temp dir. Open it with :meth:`open`."""

    def __init__(self, root: Path, runner: BytesRunner) -> None:
        """Create the mirror object. Use :meth:`open`, which also creates the repo.

        Args:
            root: The private temp dir.
            runner: Runs git.
        """
        self._root = root
        self._git_dir = root / "mirror.git"
        self._run = runner
        self._env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(root),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_TERMINAL_PROMPT": "0",
            **_IDENTITY,
        }
        self._count = 0

    @classmethod
    @asynccontextmanager
    async def open(cls, *, runner: BytesRunner = run_bytes) -> AsyncIterator[GitMirror]:
        """Create a new bare mirror, and delete it on exit.

        Args:
            runner: Runs git. Tests pass a recording runner.

        Yields:
            The mirror.
        """
        root = Path(await asyncio.to_thread(tempfile.mkdtemp, prefix="seshat-dev-mirror-"))
        mirror = cls(root, runner)
        try:
            await mirror._git("init", "-q", "--bare", str(mirror._git_dir), bare=False)
            yield mirror
        finally:
            await asyncio.to_thread(shutil.rmtree, root, True)

    def _tmp(self, name: str) -> Path:
        self._count += 1
        return self._root / f"{self._count}-{name}"

    async def _git(
        self,
        *args: str,
        bare: bool = True,
        stdin: bytes | None = None,
        extra_env: Mapping[str, str] | None = None,
        max_stdout: int = MAX_BUNDLE_BYTES,
        check: bool = True,
    ) -> BytesResult:
        prefix: list[str] = ["git", *_HARDENING]
        if bare:
            prefix += ["--git-dir", str(self._git_dir)]
        result = await self._run(
            [*prefix, *args],
            env={**self._env, **(extra_env or {})},
            stdin=stdin,
            timeout=GIT_TIMEOUT_SECONDS,
            max_stdout=max_stdout,
        )
        if check and result.returncode != 0:
            # Only the subcommand is named: an argument could be a URL or a path.
            raise DevAgentRepoError(
                f"git {args[0]} failed ({result.returncode}): {result.stderr.strip()[-300:]}"
            )
        return result

    async def _out(self, *args: str, **kwargs: object) -> str:
        result = await self._git(*args, **kwargs)  # type: ignore[arg-type]
        return result.stdout.decode().strip()

    async def _commit(self, tree: str, message: str, parent: str | None) -> str:
        parents = ["-p", _check_sha(parent)] if parent else []
        return _check_sha(await self._out("commit-tree", tree, *parents, "-m", message))

    async def commit_template(self, template_dir: Path, *, message: str = "Template") -> str:
        """Commit the template folder as a root commit (D8.5).

        Args:
            template_dir: The template, a folder in the gateway image.
            message: The commit message.

        Returns:
            The commit id.
        """
        index = {"GIT_INDEX_FILE": str(self._tmp("index"))}
        work = str(template_dir.resolve())
        await self._git(
            "-C", work, "--git-dir", str(self._git_dir), "--work-tree", work, "add", "-A", ".",
            bare=False, extra_env=index,
        )  # fmt: skip
        tree = await self._out("write-tree", extra_env=index)
        return await self._commit(tree, message, None)

    async def commit_page(
        self, parent: str, html: bytes, *, message: str = "Import the page"
    ) -> str:
        """Commit ``html`` as ``dist/index.html`` on top of ``parent`` (D8.7).

        Args:
            parent: The template commit.
            html: The page bytes.
            message: The commit message.

        Returns:
            The commit id.

        Raises:
            DevAgentRepoError: If the page is larger than 5 MB, or git fails.
        """
        if len(html) > MAX_PAGE_BYTES:
            raise DevAgentRepoError("the page is larger than 5 MB")
        blob = _check_sha(await self._out("hash-object", "-w", "--stdin", stdin=html))
        index = {"GIT_INDEX_FILE": str(self._tmp("index"))}
        await self._git("read-tree", _check_sha(parent), extra_env=index)
        await self._git(
            "update-index", "--add", "--cacheinfo", f"100644,{blob},{PAGE_PATH}", extra_env=index
        )
        tree = await self._out("write-tree", extra_env=index)
        return await self._commit(tree, message, parent)

    async def read_page(self, commit: str) -> bytes:
        """Return the bytes of ``dist/index.html`` at ``commit``.

        Args:
            commit: The commit id.

        Returns:
            The page bytes.
        """
        result = await self._git(
            "cat-file", "blob", f"{_check_sha(commit)}:{PAGE_PATH}", max_stdout=MAX_PAGE_BYTES
        )
        return result.stdout

    async def files_under(
        self, commit: str, folder: str, *, max_files: int, max_bytes: int
    ) -> dict[str, bytes]:
        """Return every file under ``folder`` at ``commit``, by its path in the repo.

        The bytes come from ``cat-file``, never from ``git archive``: a ``.gitattributes`` file in
        the tree can change archive bytes (``export-subst``) or drop files (``export-ignore``).

        Args:
            commit: The commit id.
            folder: A top-level folder, for example ``tests``.
            max_files: The most files to accept.
            max_bytes: The most bytes to accept in total.

        Returns:
            The files. Empty when the folder does not exist.

        Raises:
            DevAgentRepoError: If the folder holds a link or a submodule, or passes a cap.
        """
        listing = await self._git(
            "ls-tree", "-r", "-l", "-z", _check_sha(commit), "--", f"{_check_branch(folder)}/"
        )
        entries: list[tuple[str, str]] = []
        total = 0
        for entry in listing.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, _kind, sha, size = meta.decode().split()
            if mode not in _PAGE_MODES:
                raise DevAgentRepoError(f"{folder}/ holds an entry that is not a plain file")
            try:
                path = raw_path.decode()
            except UnicodeDecodeError:
                raise DevAgentRepoError(f"{folder}/ holds a path that is not UTF-8") from None
            total += int(size)
            entries.append((path, _check_sha(sha)))
        if len(entries) > max_files or total > max_bytes:
            raise DevAgentRepoError(
                f"{folder}/ passes the cap of {max_files} files or {max_bytes} B"
            )
        files: dict[str, bytes] = {}
        for path, sha in entries:
            files[path] = (await self._git("cat-file", "blob", sha, max_stdout=max_bytes)).stdout
        return files

    async def fetch(self, remote: GitRemote, ref: str) -> str | None:
        """Fetch one ref of a remote into the mirror.

        Args:
            remote: The remote and its credential.
            ref: The full ref name, for example ``refs/heads/main``.

        Returns:
            The commit id, or ``None`` when the remote has no such ref.

        Raises:
            DevAgentRepoError: If the fetch fails for another reason.
        """
        local = "refs/fetched/" + _check_ref(ref).removeprefix("refs/")
        result = await self._git(
            "fetch", "-q", "--no-tags", "--no-write-fetch-head", _check_url(remote.url),
            f"+{ref}:{local}",
            extra_env=remote.env, check=False,
        )  # fmt: skip
        if result.returncode != 0:
            if "couldn't find remote ref" in result.stderr:
                return None
            raise DevAgentRepoError(f"git fetch failed ({result.returncode})")
        return await self._commit_at(local)

    async def _commit_at(self, ref: str) -> str:
        sha = _check_sha(await self._out("rev-parse", "--verify", "--quiet", ref))
        kind = await self._out("cat-file", "-t", sha)
        if kind != "commit":
            raise DevAgentRepoError(f"{ref} is a {kind}, not a commit")
        return sha

    async def fetch_bundle(self, bundle: bytes, branch: str) -> str:
        """Fetch ``refs/heads/<branch>`` of a handoff bundle into the mirror (D8.8.3).

        Git checks every object of the bundle (``transfer.fsckObjects``).

        Args:
            bundle: The bundle bytes from the handoff container.
            branch: The branch, ``job/<job id>``.

        Returns:
            The bundled head commit.

        Raises:
            DevAgentRepoError: If the bundle is too large, broken, or has no such branch.
        """
        if len(bundle) > MAX_BUNDLE_BYTES:
            raise DevAgentRepoError("the bundle is larger than the cap")
        path = self._tmp("job.bundle")
        await asyncio.to_thread(path.write_bytes, bundle)
        local = "refs/handoff/head"
        await self._git(
            "fetch", "-q", "--no-tags", "--no-write-fetch-head", str(path),
            f"+refs/heads/{_check_branch(branch)}:{local}",
        )  # fmt: skip
        return await self._commit_at(local)

    async def is_ancestor(self, ancestor: str, descendant: str) -> bool:
        """Return whether ``ancestor`` is ``descendant`` or one of its ancestors.

        A commit that is not in the mirror is no ancestor.

        Args:
            ancestor: The commit that must come first.
            descendant: The later commit.

        Returns:
            ``True`` only when git confirms the ancestry.
        """
        result = await self._git(
            "merge-base", "--is-ancestor", _check_sha(ancestor), _check_sha(descendant),
            check=False,
        )  # fmt: skip
        return result.returncode == 0

    async def result_problem(self, head: str) -> str | None:
        """Return why the tree of ``head`` cannot be a result, or ``None`` when it can (D8.8.3).

        Args:
            head: The result commit.

        Returns:
            ``tree_has_link`` (a symlink or a submodule anywhere), ``page_missing`` (no regular
            ``dist/index.html``), ``page_too_large`` (over 5 MB), or ``None``.
        """
        listing = await self._git("ls-tree", "-r", "-t", "-l", "-z", _check_sha(head))
        page: tuple[str, str] | None = None
        for entry in listing.stdout.split(b"\0"):
            if not entry:
                continue
            meta, _, raw_path = entry.partition(b"\t")
            mode, _kind, _sha, size = meta.decode().split()
            if mode not in _PLAIN_MODES:
                return "tree_has_link"
            if raw_path == PAGE_PATH.encode():
                page = (mode, size)
        if page is None or page[0] not in _PAGE_MODES:
            return "page_missing"
        if int(page[1]) > MAX_PAGE_BYTES:
            return "page_too_large"
        return None

    async def push(self, remote: GitRemote, commit: str, ref: str) -> None:
        """Push ``commit`` to ``ref`` on the remote, never by force.

        A push that is not a fast-forward is refused by git, and by GitHub.

        Args:
            remote: The remote and its credential.
            commit: The commit id.
            ref: The full ref name on the remote.

        Raises:
            DevAgentRepoError: If the push fails or is refused.
        """
        await self._git(
            "push", "-q", "--porcelain", _check_url(remote.url),
            f"{_check_sha(commit)}:{_check_ref(ref)}",
            extra_env=remote.env,
        )  # fmt: skip

    async def workspace_tar(self, base: str, branch: str) -> bytes:
        """Return a tar of a clone of ``base`` on ``branch``, for the agent's workspace (D8.4).

        The clone has no remote and a fixed local identity. Every entry belongs to the agent
        container's user, and every entry is a plain file or a folder.

        Args:
            base: The base commit. It must be in the mirror.
            branch: The job branch, ``job/<job id>``.

        Returns:
            The tar bytes, for ``docker cp -a``.

        Raises:
            DevAgentRepoError: If git fails, the tree holds a link, or the tar is too large.
        """
        await self._git("update-ref", f"refs/heads/{_check_branch(branch)}", _check_sha(base))
        work = self._tmp("workspace")
        await self._git(
            "clone", "-q", "--template=", "--no-tags", "--single-branch", "--branch", branch,
            str(self._git_dir), str(work),
            bare=False,
        )  # fmt: skip
        in_work = ["-C", str(work)]
        await self._git(*in_work, "remote", "remove", "origin", bare=False)
        await self._git(*in_work, "config", "user.name", _IDENTITY["GIT_AUTHOR_NAME"], bare=False)
        await self._git(*in_work, "config", "user.email", _IDENTITY["GIT_AUTHOR_EMAIL"], bare=False)
        data = await asyncio.to_thread(_tar_tree, work)
        if len(data) > MAX_BUNDLE_BYTES:
            raise DevAgentRepoError("the workspace is larger than the cap")
        return data


def _tar_tree(root: Path) -> bytes:
    """Tar a folder with every entry owned by the agent user. Refuse anything but files and dirs."""

    def owned(info: tarfile.TarInfo) -> tarfile.TarInfo:
        if not (info.isfile() or info.isdir()):
            raise DevAgentRepoError(f"the workspace holds a non-regular entry: {info.name}")
        info.uid, info.gid = WORKSPACE_UID, WORKSPACE_GID
        info.uname = info.gname = ""
        return info

    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as tar:
        tar.add(str(root), arcname=".", filter=owned)
    return buffer.getvalue()
