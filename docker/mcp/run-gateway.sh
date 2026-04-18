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
#
# NOTE: linear is intentionally excluded from --servers.
# docker/mcp-gateway routes linear through Docker Desktop OAuth (DCR), which
# requires a Docker Desktop secrets engine socket (engine.sock) that is not
# present on a plain VPS.  Passing LINEAR_PERSONAL_ACCESS_TOKEN in /.env does
# not bypass the OAuth path — the gateway still tries DCR and fails with
# "no DCR client found".  sequentialthinking and context7 use no credentials
# so they work fine.  Linear MCP must be wired up separately (see ADR or
# future work to spawn mcp/linear container directly with the PAT).

set -euo pipefail

: "${MCP_SECRETS_FILE:=/opt/seshat/mcp-secrets.env}"

exec docker run \
  --rm \
  -i \
  -v /var/run/docker.sock:/var/run/docker.sock \
  -v "${MCP_SECRETS_FILE}:/.env:ro" \
  docker/mcp-gateway \
  --servers "sequentialthinking,context7"
