#!/usr/bin/env python3
"""Live check of the dev-agent GitHub App against the real organization (ADR-0159 D8; FRE-1685).

It reads the App from the ``AGENT_DEV_AGENT_GITHUB_*`` settings, as the gateway does. Run it from
the repo root. The private key can come from ``pass`` without being printed::

    AGENT_DEV_AGENT_GITHUB_APP_PRIVATE_KEY_B64="$(pass show seshat/DEV_AGENT_GITHUB_APP_PRIVATE_KEY | base64 -w0)" \\
        uv run python scripts/verify_dev_agent_github.py --output report.json

It creates ONE new private repo, ``probe-<8 hex>``, and keeps it (v0 deletes no repo). Then:

1. GitHub reports the repo as private.
2. The template commit is pushed to ``main``.
3. A local "agent workspace", cloned from that base, commits a page and rewrites ``main`` in the
   workspace. Only ``job/<id>`` is bundled, checked and pushed. ``main`` on GitHub is unchanged.
4. ``main`` fast-forwards to that result.
5. A second fix from that result moves ``main`` again. A third fix from the OLDER result does not
   descend from ``main``: ``main`` stays, and a direct push of it to ``main`` is refused.

It uses the same code as the gateway (``GitHubAppHost``, ``GitMirror``). It prints one JSON
report, with no token or key, and exits 1 if any check fails.
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from uuid import uuid4

from personal_agent.dev_agent.gateway import github_config
from personal_agent.dev_agent.git_mirror import GitMirror
from personal_agent.dev_agent.github_app import GitHubAppHost, GitRemote, repo_name
from personal_agent.dev_agent.repo_pipeline import (
    DEFAULT_TEMPLATE_DIR,
    MAIN,
    fast_forward_main,
    job_branch,
)
from personal_agent.exceptions import DevAgentRepoError
from personal_agent.security import create_guarded_http_client


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=probe", "-c", "user.email=probe@seshat.invalid", *args],
        cwd=cwd,
        capture_output=True,
        check=True,
    ).stdout.decode()


def _agent(workspace: bytes, root: Path, page: str, *, rewrite_main: bool) -> Path:
    """Play the agent in a local copy of the workspace: commit a page, maybe rewrite main."""
    ws = root / uuid4().hex[:8]
    with tarfile.open(fileobj=io.BytesIO(workspace)) as tar:
        tar.extractall(ws, filter="data")
    (ws / "dist" / "index.html").write_text(page)
    _git(ws, "add", "-A")
    _git(ws, "commit", "-qm", "page")
    if rewrite_main:
        branch = _git(ws, "symbolic-ref", "--short", "HEAD").strip()
        _git(ws, "checkout", "-q", "--orphan", "evil")
        _git(ws, "commit", "-q", "--allow-empty", "-m", "rewrite main")
        _git(ws, "branch", "-f", "main", "evil")
        _git(ws, "checkout", "-q", branch)
    return ws


def _bundle(ws: Path, branch: str) -> bytes:
    return subprocess.run(
        ["git", "-C", str(ws), "bundle", "create", "-", f"refs/heads/{branch}"],
        capture_output=True,
        check=True,
    ).stdout


async def _job(
    mirror: GitMirror, remote: GitRemote, base: str, root: Path, page: str, *, rewrite: bool
) -> str:
    """One job: workspace from ``base``, an agent commit, the handoff checks, the push."""
    branch = job_branch(uuid4())
    workspace = await mirror.workspace_tar(base, branch)
    ws = _agent(workspace, root, page, rewrite_main=rewrite)
    head = await mirror.fetch_bundle(_bundle(ws, branch), branch)
    if not await mirror.is_ancestor(base, head):
        raise DevAgentRepoError("the probe head does not descend from its base")
    problem = await mirror.result_problem(head)
    if problem is not None:
        raise DevAgentRepoError(f"the probe result is refused: {problem}")
    await mirror.push(remote, head, f"refs/heads/{branch}")
    return head


async def run() -> dict[str, object]:
    """Run the probe and return the report."""
    config = github_config()
    if config is None:
        return {"ok": False, "error": "the AGENT_DEV_AGENT_GITHUB_* settings are not all set"}
    report: dict[str, object] = {"org": config.org}
    failures: list[str] = []
    name = repo_name("probe", uuid4())
    report["repo"] = name
    async with create_guarded_http_client() as http:
        host = GitHubAppHost(config, http)
        await host.create_private_repo(name)
        private = await host.is_private(name)
        report["private"] = private
        if private is not True:
            failures.append("the repo is not private")
        remote = await host.remote(name)
        with tempfile.TemporaryDirectory(prefix="fre1685-probe-") as tmp:
            root = Path(tmp)
            async with GitMirror.open() as mirror:
                base = await mirror.commit_template(DEFAULT_TEMPLATE_DIR)
                await mirror.push(remote, base, MAIN)
                report["base"] = base
                if await mirror.fetch(remote, MAIN) != base:
                    failures.append("main is not the template commit after the push")

                r1 = await _job(mirror, remote, base, root, "<p>v1</p>", rewrite=True)
                main_after_handoff = await mirror.fetch(remote, MAIN)
                report["main_after_handoff_with_rewrite"] = main_after_handoff
                if main_after_handoff != base:
                    failures.append("main changed after a handoff whose workspace rewrote main")

                moved = await fast_forward_main(mirror, remote, r1)
                report["fast_forward_to_r1"] = moved
                if not moved or await mirror.fetch(remote, MAIN) != r1:
                    failures.append("main did not fast-forward to the first result")

                r2 = await _job(mirror, remote, r1, root, "<p>v2</p>", rewrite=False)
                moved2 = await fast_forward_main(mirror, remote, r2)
                r3 = await _job(mirror, remote, r1, root, "<p>v1, another fix</p>", rewrite=False)
                moved3 = await fast_forward_main(mirror, remote, r3)
                report["fast_forward_to_r2"] = moved2
                report["fast_forward_older_r3"] = moved3
                if not moved2 or moved3:
                    failures.append("the fast-forward outcomes are wrong")
                if await mirror.fetch(remote, MAIN) != r2:
                    failures.append("main moved off the later fix")
                try:
                    await mirror.push(remote, r3, MAIN)
                    failures.append("GitHub accepted a non-fast-forward push to main")
                    report["github_refuses_non_fast_forward"] = False
                except DevAgentRepoError:
                    report["github_refuses_non_fast_forward"] = True
                report["main_final"] = await mirror.fetch(remote, MAIN)
                report["results"] = {"r1": r1, "r2": r2, "r3": r3}
    report["failures"] = failures
    report["ok"] = not failures
    return report


def main() -> int:
    """Run the probe, print the JSON report, and return 0 only if every check passed."""
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--output", type=Path, help="also write the JSON report to this file")
    args = parser.parse_args()
    try:
        report = asyncio.run(run())
    except DevAgentRepoError as exc:
        report = {"ok": False, "error": str(exc)}
    text = json.dumps(report, indent=2)
    print(text)
    if args.output is not None:
        args.output.write_text(text + "\n", encoding="utf-8")
    return 0 if report.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
