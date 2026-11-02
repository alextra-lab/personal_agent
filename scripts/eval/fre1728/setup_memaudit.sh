#!/usr/bin/env bash
# FRE-1728: download MemAudit at a pinned commit, outside the repo, and build its own environment.
#
# Downloaded code stays in its own directory. This script runs none of it: git hooks are off. MemAudit's environment
# holds only the OpenAI client, because its requirements.txt is a full freeze that includes vLLM. Inspect Scout gets a
# separate environment, because it brings about 500 MB of packages that MemAudit does not need.
#
# MemAudit pins Python 3.10.20 in environment.yml, and 3.10 is past end of life. The parts that this pilot imports
# need only the standard library and the OpenAI client, and they work on 3.14 (checked 2026-11-02).
set -euo pipefail

COMMIT=0c20cf72f489a00e7614bd5eefb0aa94098ec93b
REMOTE=https://github.com/sora1998/MEMAUDIT-bench.git
ROOT="${FRE1728_HOME:-$HOME/.cache/seshat-fre1728}"
REPO="$ROOT/memaudit"
VENV="$ROOT/venv"
SCOUT_VENV="$ROOT/scout-venv"

mkdir -p "$ROOT"
if [ ! -d "$REPO/.git" ]; then
  git init --quiet "$REPO"
  git -C "$REPO" config core.hooksPath /dev/null
  git -C "$REPO" remote add origin "$REMOTE"
fi
git -C "$REPO" fetch --quiet --depth 1 origin "$COMMIT"
git -C "$REPO" -c advice.detachedHead=false checkout --quiet "$COMMIT"

actual=$(git -C "$REPO" rev-parse HEAD)
if [ "$actual" != "$COMMIT" ]; then
  echo "expected $COMMIT, found $actual" >&2
  exit 1
fi

if [ "${1:-}" != "--download-only" ]; then
  uv venv --quiet --allow-existing --python 3.14 "$VENV"
  uv pip install --quiet --python "$VENV/bin/python" 'openai==2.30.0'
  uv venv --quiet --allow-existing --python 3.14 "$SCOUT_VENV"
  uv pip install --quiet --python "$SCOUT_VENV/bin/python" 'inspect-scout==0.6.2' 'openai==3.42.1'
  echo "python:   $VENV/bin/python"
  echo "scout:    $SCOUT_VENV/bin/python"
fi

echo "memaudit: $REPO at $actual"
