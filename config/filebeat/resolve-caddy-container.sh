#!/bin/sh
# Resolve the caddy container's Docker-assigned ID and exec Filebeat (FRE-1146 / ADR-0132 D3).
#
# Docker doesn't expose a stable, statically-known container ID for a compose
# service across recreations — only the container_name is stable. Rather than
# mounting /var/run/docker.sock to ask the Docker API for it (a read-only
# socket bind mount does not make the Docker API itself read-only — any
# request over that socket reaches the full Engine API, including host-mount
# container creation), this scans config.v2.json under the already-mounted,
# read-only /var/lib/docker/containers directory for the matching "Name"
# field. Filesystem read access only, scoped to what the log-shipping already
# needs to mount.
set -eu

CONTAINER_NAME="${CADDY_CONTAINER_NAME:-cloud-sim-caddy}"
CONTAINERS_DIR="${DOCKER_CONTAINERS_DIR:-/var/lib/docker/containers}"

for cfg in "$CONTAINERS_DIR"/*/config.v2.json; do
  if grep -q "\"Name\":\"/${CONTAINER_NAME}\"" "$cfg" 2>/dev/null; then
    export CADDY_CONTAINER_ID
    CADDY_CONTAINER_ID="$(basename "$(dirname "$cfg")")"
    exec filebeat -e -c /usr/share/filebeat/filebeat.yml
  fi
done

echo "resolve-caddy-container: no container named ${CONTAINER_NAME} found under ${CONTAINERS_DIR}" >&2
exit 1
