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
#   # for 'ab' mode with a non-zero --distractor-background (the default is 40):
#   #   export FRE435_LIVE_NEO4J_PASSWORD=<PRODUCTION Neo4j password>   # FRE-778 — separate
#   #   from AGENT_NEO4J_PASSWORD above; never falls back to it. Omit by passing
#   #   --distractor-background 0 instead.
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
  0.6b)   CONFIG="config/models.yaml";                  DIMS=1024 ;;
  4b)     CONFIG="config/models.benchmark-4b.yaml";     DIMS=2560 ;;  # FRE-656 (Q4 — precision-confounded; retired)
  4b-f16) CONFIG="config/models.benchmark-4b-f16.yaml"; DIMS=2560 ;;  # FRE-694 middle rung (f16)
  8b)     CONFIG="config/models.benchmark-8b.yaml";     DIMS=4096 ;;  # FRE-694 (f16)
  *) echo "unknown embedder: '$EMBEDDER' (want 0.6b|4b|4b-f16|8b)" >&2; exit 2 ;;
esac
# calibrate/ab → ab_relevance_bounded.py (Neo4j). separation → separation_benchmark.py (offline, FRE-694).
case "$MODE" in calibrate|ab|separation) ;; *) echo "unknown mode: '$MODE' (want calibrate|ab|separation)" >&2; exit 2 ;; esac

: "${AGENT_NEO4J_PASSWORD:?export AGENT_NEO4J_PASSWORD (the test stack :7688 password) first}"
case "$EMBEDDER" in
  4b|4b-f16|8b)
    : "${CF_ACCESS_CLIENT_ID:?$EMBEDDER needs CF_ACCESS_CLIENT_ID (Access-gated slm.example.com)}"
    : "${CF_ACCESS_CLIENT_SECRET:?$EMBEDDER needs CF_ACCESS_CLIENT_SECRET}"
    ;;
esac
# 'ab' mode defaults to --distractor-background 40, which reads production Neo4j
# and now (FRE-778) requires its own credential, separate from AGENT_NEO4J_PASSWORD
# above — skip this only if the caller explicitly passed --distractor-background 0.
if [ "$MODE" = "ab" ]; then
  case " $* " in
    *' --distractor-background 0 '*) ;;
    *) : "${FRE435_LIVE_NEO4J_PASSWORD:?export FRE435_LIVE_NEO4J_PASSWORD (the PRODUCTION Neo4j password -- separate from AGENT_NEO4J_PASSWORD, never falls back to it) first, or pass --distractor-background 0 to skip the live-corpus read}" ;;
  esac
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
if [ "$MODE" = "separation" ]; then
  # FRE-694 offline separation benchmark (no substrate); --arm == the embedder label.
  exec uv run python scripts/eval/fre435_memory_recall/separation_benchmark.py \
    --arm "$EMBEDDER" "$@"
fi
exec uv run python scripts/eval/fre435_memory_recall/ab_relevance_bounded.py \
  --mode "$MODE" --run-id "fre656-${EMBEDDER}-${MODE}" "$@"
