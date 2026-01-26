#!/usr/bin/env bash
set -euo pipefail

BASE_DIR="personal_agent"

echo "Initializing project structure in: $BASE_DIR"
[ ! -d "$BASE_DIR" ] && echo "Directory '$BASE_DIR' does not exist." && exit 1

# --- Core Directories ---
declare -a DIRS=(
  "$BASE_DIR/functional-spec"
  "$BASE_DIR/architecture"
  "$BASE_DIR/governance"
  "$BASE_DIR/governance/captains_log"
  "$BASE_DIR/governance/config_proposals"
  "$BASE_DIR/governance/reviews"
  "$BASE_DIR/governance/experiments"
  "$BASE_DIR/research-knowledge"
  "$BASE_DIR/models"
  "$BASE_DIR/tools"
  "$BASE_DIR/telemetry"
  "$BASE_DIR/telemetry/logs"
  "$BASE_DIR/telemetry/metrics"
  "$BASE_DIR/telemetry/evaluation"
  "$BASE_DIR/docs"
)

for D in "${DIRS[@]}"; do
  mkdir -p "$D"
done

# --- Initial Key Files ---
declare -a FILES=(
  "$BASE_DIR/README.md"
  "$BASE_DIR/ROADMAP.md"
  "$BASE_DIR/functional-spec/functional_spec_v0.1.md"
  "$BASE_DIR/architecture/system_architecture_v0.1.md"
  "$BASE_DIR/governance/GOVERNANCE_MODEL.md"
  "$BASE_DIR/governance/ADR_TEMPLATE.md"
  "$BASE_DIR/research-knowledge/README.md"
  "$BASE_DIR/models/MODEL_STRATEGY.md"
  "$BASE_DIR/tools/TOOLS_OVERVIEW.md"
  "$BASE_DIR/telemetry/TELEMETRY_OVERVIEW.md"
  "$BASE_DIR/docs/NOTES.md"
)

for F in "${FILES[@]}"; do
  [ ! -f "$F" ] && touch "$F"
done

# --- ADR bootstrap ---
INITIAL_ADR="$BASE_DIR/governance/ADR-0001-project-init.md"
if [ ! -f "$INITIAL_ADR" ]; then
cat <<EOF > "$INITIAL_ADR"
# ADR-0001: Initialize Personal Local Agent Project

## Status
Accepted

## Context
We are establishing a structured, research-backed architecture and governance framework for a secure, local, autonomous IT assistant.

## Decision
Create initial repository structure with functional specs, architecture design, governance model, research storage, telemetry, and models strategy.

## Consequences
We now have a disciplined foundation for future architectural development, experimentation, and governance transparency.
EOF
fi

echo "Project structure created successfully."
