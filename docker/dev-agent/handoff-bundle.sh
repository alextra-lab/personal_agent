#!/bin/sh
# Write a git bundle of one branch of an untrusted workspace (ADR-0159 D8.8; FRE-1682).
#
#   handoff-bundle <branch>      reads /workspace (read-only), writes /out/job.bundle
#
# The workspace is untrusted: the agent controlled .git/config and the hooks. Every setting that
# can run a program is set on the command line, which beats repo-local config. The container has
# no network and no secrets, so this is one layer. The other layer is the container boundary.
set -eu

branch="${1:-}"
case "$branch" in
  ''|-*|*..*|*[!A-Za-z0-9._/-]*)
    echo "handoff-bundle: bad branch name" >&2
    exit 2
    ;;
esac

git_safe() {
  git -C /workspace \
    -c safe.directory='*' \
    -c core.hooksPath=/dev/null \
    -c core.fsmonitor=false \
    -c core.sshCommand=/bin/false \
    -c core.pager=cat \
    -c core.alternateRefsCommand= \
    -c protocol.allow=never \
    -c gc.auto=0 \
    "$@"
}

git_safe bundle create /out/job.bundle "refs/heads/${branch}"
git_safe bundle verify /out/job.bundle
