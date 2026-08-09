#!/bin/sh
# Install the Mac-local OTel Collector and its launchd unit (FRE-1224).
#
# Homebrew carries no collector formula (`otel-cli` is a different tool), so the pinned upstream
# release tarball is fetched and checksum-verified here. The version is pinned to the SAME build as
# the VPS Collector in docker-compose.yml, and to the core distribution — not contrib, not a vendor
# distro (ADR-0129 D5).
#
# Idempotent: safe to re-run. Re-running after editing the config is in fact how you reload it.

set -eu

OTELCOL_VERSION="0.158.0"
OTELCOL_SHA256="a4fc106889faa4ffcd43c062cc8fd14c1eff6d2f53bef64930b3199bd8303095"
RELEASE_BASE="https://github.com/open-telemetry/opentelemetry-collector-releases/releases/download"

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PREFIX="${SESHAT_OTELCOL_PREFIX:-${HOME}/.local/bin}"
LABEL="com.seshat.otelcol"
PLIST_DIR="${HOME}/Library/LaunchAgents"
PLIST_PATH="${PLIST_DIR}/${LABEL}.plist"
LOG_PATH="${HOME}/Library/Logs/seshat-otelcol.log"

# The launchd agent runs FROZEN copies, not the working tree. It is persistent and holds a live
# Cloudflare Access token, so if it executed ${REPO_ROOT}/scripts/... directly then checking out a
# branch — an act reviewers reasonably treat as read-only — would change both the code it runs and
# the endpoint it sends that credential to. Re-running this installer stays the documented way to
# pick up a config change; a bare `git checkout` no longer is.
LIBEXEC_DIR="${SESHAT_OTELCOL_LIBEXEC:-${HOME}/.local/libexec/seshat}"
WRAPPER_PATH="${LIBEXEC_DIR}/otelcol_launch.sh"
CONFIG_PATH="${LIBEXEC_DIR}/mac-collector-config.yaml"

EX_CONFIG=78

arch="$(uname -m)"
case "$(uname -s)" in
    Darwin) ;;
    *) printf 'install_otelcol: this installer targets macOS only (found %s).\n' "$(uname -s)" >&2
       exit "$EX_CONFIG" ;;
esac
case "$arch" in
    arm64) asset="otelcol_${OTELCOL_VERSION}_darwin_arm64.tar.gz" ;;
    x86_64) printf 'install_otelcol: the pinned checksum is for darwin_arm64; refusing to install an unverified x86_64 build.\n' >&2
            exit "$EX_CONFIG" ;;
    *) printf 'install_otelcol: unsupported architecture %s\n' "$arch" >&2; exit "$EX_CONFIG" ;;
esac

workdir="$(mktemp -d)"
# shellcheck disable=SC2064
trap "rm -rf '$workdir'" EXIT INT TERM

printf 'install_otelcol: downloading %s\n' "$asset"
curl -fsSL "${RELEASE_BASE}/v${OTELCOL_VERSION}/${asset}" -o "${workdir}/${asset}"

# Verify BEFORE extracting. A tarball that fails this check is never unpacked, let alone executed.
printf 'install_otelcol: verifying checksum\n'
actual="$(shasum -a 256 "${workdir}/${asset}" | awk '{print $1}')"
if [ "$actual" != "$OTELCOL_SHA256" ]; then
    printf 'install_otelcol: CHECKSUM MISMATCH — refusing to install.\n' >&2
    printf '  expected %s\n  actual   %s\n' "$OTELCOL_SHA256" "$actual" >&2
    exit 1
fi

tar -xzf "${workdir}/${asset}" -C "$workdir" otelcol
mkdir -p "$PREFIX"
mv "${workdir}/otelcol" "${PREFIX}/otelcol"
chmod 755 "${PREFIX}/otelcol"
printf 'install_otelcol: installed %s\n' "${PREFIX}/otelcol"

# Freeze the runtime artifacts out of the working tree, then point the unit at those copies.
mkdir -p "$LIBEXEC_DIR"
cp "${REPO_ROOT}/scripts/mac/otelcol_launch.sh" "$WRAPPER_PATH"
cp "${REPO_ROOT}/config/otel/mac-collector-config.yaml" "$CONFIG_PATH"
chmod 755 "$WRAPPER_PATH"
chmod 644 "$CONFIG_PATH"
printf 'install_otelcol: froze runtime artifacts into %s\n' "$LIBEXEC_DIR"

# Render the launchd unit from its template. The plist deliberately carries no credentials — the
# wrapper loads those from a mode-0600 file outside the repo. The config path IS passed here: it is
# not a secret, and passing it explicitly is what pins the agent to the frozen copy.
mkdir -p "$PLIST_DIR" "$(dirname "$LOG_PATH")"
sed -e "s|@@WRAPPER_PATH@@|${WRAPPER_PATH}|g" \
    -e "s|@@CONFIG_PATH@@|${CONFIG_PATH}|g" \
    -e "s|@@LOG_PATH@@|${LOG_PATH}|g" \
    "${REPO_ROOT}/config/otel/com.seshat.otelcol.plist.template" > "$PLIST_PATH"
printf 'install_otelcol: wrote %s\n' "$PLIST_PATH"

# bootout first so a re-run reloads rather than erroring on an already-loaded label. It is
# expected to fail when nothing is loaded, hence the guard.
launchctl bootout "gui/$(id -u)/${LABEL}" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST_PATH"
printf 'install_otelcol: loaded %s\n' "$LABEL"
printf 'install_otelcol: logs at %s\n' "$LOG_PATH"
