#!/bin/bash
# MCP Gateway launcher for cloud/VPS environments (replaces Docker Desktop's `docker mcp gateway run`).
#
# Uses docker/mcp-gateway public image via Docker socket mount.
# Secrets are read from MCP_SECRETS_FILE (a host-path .env file) by the gateway.
#
# Required env vars (set in docker-compose.cloud.yml):
#   MCP_SECRETS_FILE  — absolute HOST path to the .env file with API keys
#                       (e.g. /opt/seshat/mcp-secrets.env)
#
# The inner `docker run -v` must reference a HOST path, not a container path.
# MCP_SECRETS_FILE is the same path from both the host and the gateway container
# because both share the same Docker daemon.

set -euo pipefail

: "${MCP_SECRETS_FILE:=/opt/seshat/mcp-secrets.env}"

exec docker run \
  --rm \
  -i \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${MCP_SECRETS_FILE}:/.env:ro" \
  docker/mcp-gateway \
  --servers "sequentialthinking,context7,linear"
