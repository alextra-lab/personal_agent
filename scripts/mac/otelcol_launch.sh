#!/bin/sh
# Launch wrapper for the Mac-local OTel Collector (FRE-1224).
#
# Exists so the Cloudflare Access service token never lands in a launchd plist. launchd's
# EnvironmentVariables would put the credential in a plaintext plist under LaunchAgents; instead
# this wrapper loads it from a mode-0600 file outside the repo and execs the Collector.
#
# THE `set -a` IS LORE-BEARING, NOT STYLE. A bare `. "$ENV_FILE"` creates shell variables, not
# child-process environment variables. The Collector resolves ${env:...} against its *environment*,
# so without the export the references would resolve to empty and it would ship spans to the
# Cloudflare edge with blank Access headers — a custody component authenticating with nothing, and
# failing open. The presence check below turns that into a refusal to start.
#
# Overrides, both for tests (tests/scripts/test_mac_otelcol_launch_contract.py):
#   SESHAT_OTLP_ENV_FILE  — where the credentials live
#   SESHAT_OTELCOL_BIN    — the binary to exec

set -eu

REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"

ENV_FILE="${SESHAT_OTLP_ENV_FILE:-${HOME}/.config/seshat/otlp-collector.env}"
OTELCOL_BIN="${SESHAT_OTELCOL_BIN:-${HOME}/.local/bin/otelcol}"

# Config path, in precedence order: explicit argument > env override > repo-relative default.
#
# The installed launchd unit passes the argument, pointing at the FROZEN install-owned copy under
# ~/.local/libexec/seshat — deliberately not this repo. A launchd agent is persistent and holds a
# live Cloudflare Access token; binding its config to a git working tree would mean a branch
# checkout silently changes where that credential is sent, and merely reviewing a branch is
# normally a read-only act. The repo-relative default remains for direct invocation and tests.
CONFIG_FILE="${1:-${SESHAT_OTELCOL_CONFIG:-${REPO_ROOT}/config/otel/mac-collector-config.yaml}}"

# sysexits.h EX_CONFIG: a configuration error, distinct from a crash, so a launchd respawn loop is
# diagnosable from `launchctl print` alone. Paired with ThrottleInterval in the plist so a bad
# config throttles rather than spinning.
EX_CONFIG=78

REQUIRED_VARS="SESHAT_OTLP_INGRESS_URL SESHAT_OTLP_CF_ACCESS_CLIENT_ID SESHAT_OTLP_CF_ACCESS_CLIENT_SECRET"

if [ ! -f "$ENV_FILE" ]; then
    printf 'seshat-otelcol: credential file not found: %s\n' "$ENV_FILE" >&2
    printf 'seshat-otelcol: copy config/otel/mac-collector.env.example and chmod 600 it.\n' >&2
    exit "$EX_CONFIG"
fi

# The env file is SOURCED, which means its contents are EXECUTED, not merely parsed. A file any
# other principal can write is therefore arbitrary code execution inside a launchd-persistent
# context, re-run at every login, with KeepAlive guaranteeing the retry. The documented `chmod 600`
# is a manual step, so enforce it here rather than trusting it: refuse unless we own the file and
# neither group nor other can write it.
#
# Branch on the platform explicitly rather than trying one form and falling back on failure.
# BSD `stat -f` formats a file; GNU `stat -f` reports FILESYSTEM status and *succeeds* with
# unrelated output, so a `cmd-a || cmd-b` chain silently parses garbage on Linux instead of
# falling through. (Caught by CI, which runs these contract tests on Linux even though the
# component is macOS-only.)
case "$(uname -s)" in
    Darwin) env_meta="$(stat -f '%u %Op' "$ENV_FILE" 2>/dev/null || true)" ;;
    *)      env_meta="$(stat -c '%u %a'  "$ENV_FILE" 2>/dev/null || true)" ;;
esac

env_owner="${env_meta%% *}"
env_mode="${env_meta##* }"

# Fail closed on an unparseable result. Without this, a stat whose output shape we did not expect
# yields empty fields and the checks below compare emptiness — which is how a security control
# quietly stops controlling anything.
case "$env_owner" in
    ''|*[!0-9]*)
        printf 'seshat-otelcol: cannot determine ownership of %s — refusing to source it.\n' \
            "$ENV_FILE" >&2
        exit "$EX_CONFIG"
        ;;
esac
# Last three octal digits. BSD %Op prefixes the file type (100600); GNU %a does not (600).
env_perm="$(printf '%s' "$env_mode" | sed 's/.*\(...\)$/\1/')"
env_group_other="$(printf '%s' "$env_perm" | cut -c2-3)"

if [ "$env_owner" != "$(id -u)" ]; then
    printf 'seshat-otelcol: %s is owned by uid %s, not %s — refusing to source it.\n' \
        "$ENV_FILE" "$env_owner" "$(id -u)" >&2
    exit "$EX_CONFIG"
fi
case "$env_group_other" in
    *[2367]*)
        printf 'seshat-otelcol: %s is group- or world-writable (mode %s) — refusing to source it.\n' \
            "$ENV_FILE" "$env_perm" >&2
        printf 'seshat-otelcol: run chmod 600 on it.\n' >&2
        exit "$EX_CONFIG"
        ;;
esac

set -a
# shellcheck source=/dev/null
. "$ENV_FILE"
set +a

# Diagnostics name variables, never values — this stream is redirected to a persistent log file.
for var in $REQUIRED_VARS; do
    eval "value=\${${var}:-}"
    if [ -z "$value" ]; then
        printf 'seshat-otelcol: required variable %s is unset or empty in %s\n' "$var" "$ENV_FILE" >&2
        printf 'seshat-otelcol: refusing to start — exporting with an empty Cloudflare Access header would fail open.\n' >&2
        exit "$EX_CONFIG"
    fi
done

if [ ! -x "$OTELCOL_BIN" ]; then
    printf 'seshat-otelcol: collector binary not found or not executable: %s\n' "$OTELCOL_BIN" >&2
    printf 'seshat-otelcol: run scripts/mac/install_otelcol.sh first.\n' >&2
    exit "$EX_CONFIG"
fi

exec "$OTELCOL_BIN" --config="$CONFIG_FILE"
