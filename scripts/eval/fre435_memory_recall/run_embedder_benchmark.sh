#!/usr/bin/env bash
# FRE-656 — safe embedder A/B runner (ADR-0100 / FRE-655 floor calibration).
#
# Runs ONE (embedder × mode) per process — sidesteps the module-global embedding
# client / settings / model-config-cache singletons, so a 0.6B run and a 4B run
# never cross-contaminate (codex review, 2026-06-28).
#
# Why this wrapper exists rather than calling the driver directly:
#   * the driver pins the test substrate with os.environ.setdefault(), so a STRAY
#     prod AGENT_NEO4J_URI in the shell would SURVIVE and ensure_vector_index()
#     would drop+recreate the *production* entity_embedding index at the new
#     dimension — catastrophic. We force-export (not setdefault) the test stack.
#   * a preflight refuses to seed unless (a) neo4j is the :7688 test stack and
#     (b) the embedder actually returns a correctly-sized NON-ZERO vector over
#     the wire — catching CF-Access failures and the dimensions= trap, both of
#     which otherwise degrade silently to zero vectors.
#
# Usage:
#   export AGENT_NEO4J_PASSWORD=<test-stack :7688 password>   # from .env NEO4J_PASSWORD
#   # for the 4b embedder also: export CF_ACCESS_CLIENT_ID=... CF_ACCESS_CLIENT_SECRET=...
#   scripts/eval/fre435_memory_recall/run_embedder_benchmark.sh <0.6b|4b> <calibrate|ab> [extra driver args]
#
# Recommended sequence (apples-to-apples, drift-free separation):
#   run ... 0.6b calibrate --distractor-background 0
#   run ... 4b   calibrate --distractor-background 0
#   run ... 0.6b ab
#   run ... 4b   ab
set -euo pipefail

EMBEDDER="${1:?usage: run_embedder_benchmark.sh <0.6b|4b> <calibrate|ab> [extra driver args]}"
MODE="${2:?usage: run_embedder_benchmark.sh <0.6b|4b> <calibrate|ab> [extra driver args]}"
shift 2

case "$EMBEDDER" in
  # 0.6b uses models.yaml (localhost:8503/8504) — host-reachable; models.cloud.yaml
  # points at the docker-internal 'embeddings' host which does not resolve from a
  # host shell. Same physical 0.6B llama.cpp server either way.
  0.6b) CONFIG="config/models.yaml";              DIMS=1024 ;;
  4b)   CONFIG="config/models.benchmark-4b.yaml"; DIMS=2560 ;;
  *) echo "unknown embedder: '$EMBEDDER' (want 0.6b|4b)" >&2; exit 2 ;;
esac
case "$MODE" in calibrate|ab) ;; *) echo "unknown mode: '$MODE' (want calibrate|ab)" >&2; exit 2 ;; esac

: "${AGENT_NEO4J_PASSWORD:?export AGENT_NEO4J_PASSWORD (the test stack :7688 password) first}"
if [ "$EMBEDDER" = "4b" ]; then
  : "${CF_ACCESS_CLIENT_ID:?4b needs CF_ACCESS_CLIENT_ID (Access-gated slm.frenchforet.com)}"
  : "${CF_ACCESS_CLIENT_SECRET:?4b needs CF_ACCESS_CLIENT_SECRET}"
fi

# Force-set (NOT setdefault) the TEST substrate so no stray prod value survives.
export APP_ENV=test
export AGENT_NEO4J_URI="bolt://localhost:7688"
export AGENT_NEO4J_USER="${AGENT_NEO4J_USER:-neo4j}"
export AGENT_ELASTICSEARCH_URL="http://localhost:9201"
export AGENT_DATABASE_URL="postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
export AGENT_ELASTICSEARCH_INDEX_PREFIX="agent-logs-test"
export AGENT_CAPTAINS_LOG_INDEX_PREFIX="agent-captains-test"
export AGENT_MODEL_CONFIG_PATH="$CONFIG"
export AGENT_EMBEDDING_DIMENSIONS="$DIMS"

# Preflight: refuse to touch the substrate unless it is the test stack AND the
# embedder returns a non-zero, correctly-sized vector over the wire.
uv run python - "$EMBEDDER" "$DIMS" <<'PY'
import asyncio
import os
import sys

from personal_agent.config import settings
from personal_agent.memory.embeddings import generate_embedding

embedder, dims = sys.argv[1], int(sys.argv[2])
uri = settings.neo4j_uri
if not uri.endswith(":7688"):
    sys.exit(f"[preflight] REFUSING: neo4j_uri={uri!r} is not the test stack :7688")
if settings.embedding_dimensions != dims:
    sys.exit(f"[preflight] REFUSING: embedding_dimensions={settings.embedding_dimensions} != {dims}")

vec = asyncio.run(generate_embedding("calibration preflight probe", mode="query"))
nonzero = any(x != 0.0 for x in vec)
print(
    f"[preflight] embedder={embedder} model_config={os.environ['AGENT_MODEL_CONFIG_PATH']} "
    f"neo4j={uri} dims={settings.embedding_dimensions} len(vec)={len(vec)} nonzero={nonzero}"
)
if len(vec) != dims:
    sys.exit(f"[preflight] REFUSING: vector width {len(vec)} != {dims} (wrong model / dimensions= ignored)")
if not nonzero:
    sys.exit("[preflight] REFUSING: zero vector — CF-Access/endpoint failure (silent degradation)")
print("[preflight] OK")
PY

echo "[run] embedder=$EMBEDDER mode=$MODE config=$CONFIG dims=$DIMS args=$*"
# Repo root on the path so the driver's `from scripts.eval...` imports resolve.
export PYTHONPATH="${PYTHONPATH:+$PYTHONPATH:}$(pwd)"
exec uv run python scripts/eval/fre435_memory_recall/ab_relevance_bounded.py \
  --mode "$MODE" --run-id "fre656-${EMBEDDER}-${MODE}" "$@"
