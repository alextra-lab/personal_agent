#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# deploy.sh — Deploy the latest main branch to the VPS
#
# Run from your Mac: bash infrastructure/scripts/deploy.sh [--build] [--full]
#
# Flags:
#   (none)   Pull latest code, restart services (no rebuild — fast)
#   --build  Pull + rebuild seshat-gateway image + restart (after code changes)
#   --full   Pull + rebuild ALL images + restart (after dependency changes)
#
# Prerequisites:
#   - VPS_SSH_HOST set in environment (required — your SSH alias for the VPS)
#   - Repo already cloned on VPS at $DEPLOY_PATH
#   - .env file already present on VPS at $DEPLOY_PATH/.env
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SSH_HOST="${VPS_SSH_HOST:?VPS_SSH_HOST is required — set it to your VPS SSH alias (e.g. export VPS_SSH_HOST=my-vps)}"
DEPLOY_PATH="${VPS_DEPLOY_PATH:-/opt/seshat}"
COMPOSE_FILE="docker-compose.cloud.yml"
BUILD=false
FULL=false

for arg in "$@"; do
  case "$arg" in
    --build) BUILD=true ;;
    --full)  FULL=true  ;;
  esac
done

# ── Local checks ──────────────────────────────────────────────────────────────

CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [[ "$CURRENT_BRANCH" != "main" ]]; then
  echo "Warning: you are on branch '$CURRENT_BRANCH', not main."
  read -rp "Deploy this branch anyway? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
fi

if ! git diff-index --quiet HEAD --; then
  echo "Warning: you have uncommitted changes."
  read -rp "Deploy without committing? [y/N] " confirm
  [[ "$confirm" =~ ^[Yy]$ ]] || exit 0
fi

# ── Build remote command ──────────────────────────────────────────────────────

if $FULL; then
  REMOTE_CMD="
    cd $DEPLOY_PATH &&
    test -f .env || { echo 'ERROR: .env not found at $DEPLOY_PATH — see .env.example'; exit 1; } &&
    git pull --ff-only &&
    docker compose -f $COMPOSE_FILE build &&
    docker compose -f $COMPOSE_FILE up -d
  "
  echo "==> Full rebuild + deploy to $SSH_HOST:$DEPLOY_PATH"
elif $BUILD; then
  REMOTE_CMD="
    cd $DEPLOY_PATH &&
    test -f .env || { echo 'ERROR: .env not found at $DEPLOY_PATH — see .env.example'; exit 1; } &&
    git pull --ff-only &&
    docker compose -f $COMPOSE_FILE build seshat-gateway &&
    docker compose -f $COMPOSE_FILE up -d seshat-gateway
  "
  echo "==> Build seshat-gateway + deploy to $SSH_HOST:$DEPLOY_PATH"
else
  REMOTE_CMD="
    cd $DEPLOY_PATH &&
    test -f .env || { echo 'ERROR: .env not found at $DEPLOY_PATH — see .env.example'; exit 1; } &&
    git pull --ff-only &&
    docker compose -f $COMPOSE_FILE up -d
  "
  echo "==> Pull + restart (no rebuild) to $SSH_HOST:$DEPLOY_PATH"
fi

# ── Execute ───────────────────────────────────────────────────────────────────

ssh "$SSH_HOST" "$REMOTE_CMD"

echo ""
echo "==> Verifying health..."
sleep 5
ssh "$SSH_HOST" "docker compose -f $DEPLOY_PATH/$COMPOSE_FILE ps --format 'table {{.Name}}\t{{.Status}}'"
