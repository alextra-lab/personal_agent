#!/bin/sh
# Write a git bundle of one branch of an untrusted workspace (ADR-0159 D8.8; FRE-1682, FRE-1685).
#
#   handoff-bundle <branch>      reads /workspace (read-only), writes /out/job.bundle
#
# The workspace is untrusted: the agent controlled all of /workspace/.git. So git never opens it
# as a repository, and never reads its config, hooks or refs through git:
#
#   1. The script reads the branch head from the loose ref file or packed-refs, as text. It accepts
#      40 hex characters only.
#   2. git runs in a new bare repo in /tmp. The workspace gives only its object store
#      (GIT_OBJECT_DIRECTORY). The script writes the one ref there itself, from its own argument.
#   3. Every setting that can run a program is also set on the command line (a second layer).
#
# The container has no network and no secrets. That is the other boundary. The gateway checks the
# bundle again in its own mirror (fsck, ancestry, the tree).
set -eu

branch="${1:-}"
case "$branch" in
  ''|-*|*..*|*[!A-Za-z0-9._/-]*)
    echo "handoff-bundle: bad branch name" >&2
    exit 2
    ;;
esac

fail() {
  echo "handoff-bundle: $1" >&2
  exit 3
}

ws=/workspace/.git
ref="refs/heads/${branch}"
[ -d "$ws" ] && [ ! -L "$ws" ] || fail "the workspace has no .git folder"
[ -d "$ws/objects" ] || fail "the workspace has no object store"

head=""
if [ -f "$ws/$ref" ] && [ ! -L "$ws/$ref" ]; then
  head=$(sed -n '1p' "$ws/$ref")
elif [ -f "$ws/packed-refs" ] && [ ! -L "$ws/packed-refs" ]; then
  head=$(awk -v r="$ref" '$2 == r { print $1; exit }' "$ws/packed-refs")
fi
case "$head" in
  *[!0-9a-f]*|'') fail "the branch head is not a commit id" ;;
esac
[ "${#head}" -eq 40 ] || fail "the branch head is not a commit id"

clean=/tmp/handoff.git

git_safe() {
  GIT_DIR="$clean" GIT_OBJECT_DIRECTORY="$ws/objects" git \
    -c safe.directory='*' \
    -c core.hooksPath=/dev/null \
    -c core.fsmonitor=false \
    -c core.sshCommand=/bin/false \
    -c core.pager=cat \
    -c core.alternateRefsCommand= \
    -c core.commitGraph=false \
    -c core.multiPackIndex=false \
    -c protocol.allow=never \
    -c gc.auto=0 \
    "$@"
}

git -c init.templateDir= init -q --bare --template= "$clean"
git_safe update-ref "$ref" "$head"
git_safe bundle create /out/job.bundle "$ref"
git_safe bundle verify -q /out/job.bundle
