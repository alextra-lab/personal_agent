#!/usr/bin/env bash
# FRE-817 -- ADR-0112 AC-4 corpus A/B runner (0.6B local vs OVH-managed 8B).
#
# Owner-authorized one-off exception to ADR-0112 D3 (corpus-A/B jobs default
# to off-host): this run is small/cheap (a few dozen embedding calls to a
# managed REST endpoint, not a local GPU/CPU batch job) and the owner
# explicitly approved running it from the current session.
#
# OVH credentials are read directly from `pass` by corpus_ab.py itself
# (never exported to the shell, never logged). This wrapper only pins the
# LOCAL 0.6b arm's config (mirrors run_embedder_benchmark.sh's 0.6b case)
# and runs a preflight before spending on the full corpus.
#
# Usage:
#   scripts/eval/fre817_corpus_ab_embedder/run_corpus_ab.sh [extra driver args]
set -euo pipefail

# Force-set (not setdefault) so no stray value survives from the shell.
export AGENT_MODEL_CONFIG_PATH="config/models.yaml"
export AGENT_EMBEDDING_DIMENSIONS=1024

# Preflight: refuse to spend on the full corpus unless the LOCAL 0.6b embedder
# returns a non-zero, correctly-sized vector over the wire. The OVH arm's
# equivalent gate is corpus_ab.py's own `_sanity_check_ovh` (run first, inside
# the driver, before any full-corpus embedding call).
uv run python - <<'PY'
import asyncio

from personal_agent.config import settings
from personal_agent.memory.embeddings import generate_embedding

if settings.embedding_dimensions != 1024:
    raise SystemExit(
        f"[preflight] REFUSING: embedding_dimensions={settings.embedding_dimensions} != 1024"
    )
vec = asyncio.run(generate_embedding("fre-817 preflight probe", mode="query"))
nonzero = any(x != 0.0 for x in vec)
print(f"[preflight] 0.6b len(vec)={len(vec)} nonzero={nonzero}")
if len(vec) != 1024:
    raise SystemExit(f"[preflight] REFUSING: vector width {len(vec)} != 1024")
if not nonzero:
    raise SystemExit("[preflight] REFUSING: zero vector -- local embedder failure")
print("[preflight] OK")
PY

echo "[run] fre817 corpus A/B: 0.6b (local) vs 8b-ovh (managed) args=$*"
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$(pwd)"
exec uv run python scripts/eval/fre817_corpus_ab_embedder/corpus_ab.py \
  --run-id "fre817-$(date +%Y%m%d)" "$@"
