#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# transfer-models.sh — Copy GGUF model files from Mac to VPS
#
# Run from your Mac ONCE before first deploy:
#   bash infrastructure/scripts/transfer-models.sh
#
# What it does:
#   1. Creates /opt/seshat/models/{embedding,reranker}/ on the VPS
#   2. Copies the GGUF files from your external drive to the VPS
#   3. Sets correct permissions
#
# Models:
#   Embedding: Qwen3-Embedding-0.6B-f16.gguf   (~1.2 GB, F16)
#   Reranker:  qwen3-reranker-0.6b-q8_0.gguf   (~650 MB, Q8_0)
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

SSH_HOST="${VPS_SSH_HOST:?VPS_SSH_HOST is required — set it to your VPS SSH alias (e.g. export VPS_SSH_HOST=my-vps)}"
VPS_MODELS_PATH="/opt/seshat/models"

EMBEDDING_SRC="/Volumes/EnvoyUltra/lm-studio/models/Qwen/Qwen3-Embedding-0.6B-GGUF/Qwen3-Embedding-0.6B-f16.gguf"
RERANKER_SRC="/Volumes/EnvoyUltra/lm-studio/models/ggml-org/Qwen3-Reranker-0.6B-Q8_0-GGUF/qwen3-reranker-0.6b-q8_0.gguf"

# ── Verify source files exist ─────────────────────────────────────────────────

for path in "$EMBEDDING_SRC" "$RERANKER_SRC"; do
  if [[ ! -f "$path" ]]; then
    echo "Error: model file not found: $path" >&2
    echo "Ensure the EnvoyUltra drive is mounted." >&2
    exit 1
  fi
done

echo "==> Source files verified."
echo "    Embedding: $(du -h "$EMBEDDING_SRC" | cut -f1)  $EMBEDDING_SRC"
echo "    Reranker:  $(du -h "$RERANKER_SRC" | cut -f1)  $RERANKER_SRC"
echo ""

# ── Create directories on VPS ─────────────────────────────────────────────────

echo "==> Creating model directories on VPS..."
ssh "$SSH_HOST" "mkdir -p $VPS_MODELS_PATH/embedding $VPS_MODELS_PATH/reranker"

# ── Transfer (rsync — resumable if interrupted) ───────────────────────────────

echo "==> Transferring embedding model (~1.2 GB)..."
rsync -ah --progress \
  -e "ssh" \
  "$EMBEDDING_SRC" \
  "$SSH_HOST:$VPS_MODELS_PATH/embedding/"

echo ""
echo "==> Transferring reranker model (~650 MB)..."
rsync -ah --progress \
  -e "ssh" \
  "$RERANKER_SRC" \
  "$SSH_HOST:$VPS_MODELS_PATH/reranker/"

# ── Verify on VPS ─────────────────────────────────────────────────────────────

echo ""
echo "==> Verifying files on VPS..."
ssh "$SSH_HOST" "ls -lh $VPS_MODELS_PATH/embedding/ $VPS_MODELS_PATH/reranker/"

echo ""
echo "==> Transfer complete. Ready to deploy."
