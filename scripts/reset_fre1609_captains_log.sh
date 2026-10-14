#!/usr/bin/env bash
# FRE-1609 — one-off reset of the Captain's Log proposal history to zero (owner decision
# 2026-10-14: "clear the file history and let the process start from zero").
#
# Clears: live telemetry/captains_log/CL-*.json, the reflections archive
# telemetry/archive/captains_log_reflections/, and the sysgraph proposal state
# (proposal, ticket, promoted_to, outcome, produced, signal, derives_from, and the
# correlates_with / influence rows that reference a proposal).
# Keeps: captains_log/captures*, sysgraph.stat, ES indices, Linear tickets,
# feedback_history/suppressed_fingerprints.json.
#
# Run on the VPS host, in this order (master runs it, FRE-1608 must be deployed first):
#   scripts/reset_fre1609_captains_log.sh dry-run          # read-only; post the output on FRE-1609 (AC-5)
#   docker stop cloud-sim-seshat-gateway                   # no writer may run from backup to verify
#   scripts/reset_fre1609_captains_log.sh backup <dir>
#   scripts/reset_fre1609_captains_log.sh verify-backup <dir>
#   scripts/reset_fre1609_captains_log.sh reset <dir>      # refuses unless verify-backup passed
#   scripts/reset_fre1609_captains_log.sh verify           # AC-1: every count 0, exit 1 otherwise
#   docker start cloud-sim-seshat-gateway
#
# <dir> must be outside every Docker volume, e.g. ~/seshat-backups/fre1609-$(date -u +%Y%m%dT%H%M%SZ).
# Delete it when FRE-1611 closes.
#
# File work runs in a throwaway container of the gateway image with --volumes-from, so it
# works while the gateway is stopped. The sysgraph restore check runs in a throwaway
# Postgres container of the production image; it never touches the production database.

set -euo pipefail

GATEWAY="${GATEWAY:-cloud-sim-seshat-gateway}"
PG="${PG:-cloud-sim-postgres}"
PG_USER="${PG_USER:-agent}"
PG_DB="${PG_DB:-personal_agent}"
RESTORE_CONTAINER="fre1609-restore-check"

# Every table the reset empties, in the order the counts are printed.
COUNT_SQL="
SELECT 'proposal', count(*) FROM sysgraph.proposal
UNION ALL SELECT 'ticket', count(*) FROM sysgraph.ticket
UNION ALL SELECT 'promoted_to', count(*) FROM sysgraph.promoted_to
UNION ALL SELECT 'outcome', count(*) FROM sysgraph.outcome
UNION ALL SELECT 'produced', count(*) FROM sysgraph.produced
UNION ALL SELECT 'signal', count(*) FROM sysgraph.signal
UNION ALL SELECT 'derives_from', count(*) FROM sysgraph.derives_from
UNION ALL SELECT 'correlates_with_proposal', count(*) FROM sysgraph.correlates_with
    WHERE from_node_type = 'proposal' OR to_node_type = 'proposal'
UNION ALL SELECT 'influence_proposal', count(*) FROM sysgraph.influence
    WHERE from_node_type = 'proposal' OR to_node_type = 'proposal';
"

RESET_SQL="
BEGIN;
DELETE FROM sysgraph.correlates_with WHERE from_node_type = 'proposal' OR to_node_type = 'proposal';
DELETE FROM sysgraph.influence WHERE from_node_type = 'proposal' OR to_node_type = 'proposal';
DELETE FROM sysgraph.outcome;   -- cascades produced
DELETE FROM sysgraph.ticket;    -- cascades promoted_to, produced
DELETE FROM sysgraph.proposal;  -- cascades promoted_to, derives_from
DELETE FROM sysgraph.signal;
COMMIT;
"

# Python run inside the gateway image (it has no gzip or find). argv[1] is the action.
FILES_PY='
import json, pathlib, sys, tarfile
root = pathlib.Path("/app/telemetry")
live_dir = root / "captains_log"
archive_dir = root / "archive" / "captains_log_reflections"
live = sorted(live_dir.glob("CL-*.json"))
archived = sorted(archive_dir.rglob("*.gz")) if archive_dir.is_dir() else []
action = sys.argv[1]
if action == "counts":
    print(f"live_cl_files|{len(live)}")
    print(f"archive_cl_files|{len(archived)}")
elif action == "list":
    for p in live:
        d = json.loads(p.read_text(encoding="utf-8"))
        pc = d.get("proposed_change") or {}
        print("live", p.name[:31], d.get("status"), "eval" if d.get("eval_mode") else "prod",
              (pc.get("fingerprint") or "-")[:12], pc.get("seen_count", "-"),
              d.get("linear_issue_id") or "-")
    for p in archived:
        print("archive", p.relative_to(archive_dir).parent, p.name[:31])
elif action == "tar":
    with tarfile.open(fileobj=sys.stdout.buffer, mode="w|gz") as tar:
        for p in live + archived:
            tar.add(p, arcname=str(p.relative_to(root)))
elif action == "delete":
    for p in live + archived:
        p.unlink()
    print(f"deleted live={len(live)} archive={len(archived)}")
elif action == "eligible":
    from personal_agent.captains_log.promotion import PromotionPipeline
    ids = [e.entry_id for e in PromotionPipeline(log_dir=live_dir).scan_promotable_entries()]
    print(f"promotion_eligible|{len(ids)}")
    for i in ids:
        print("eligible", i)
'

die() { echo "ERROR: $*" >&2; exit 1; }

gateway_image() { docker inspect -f '{{.Config.Image}}' "$GATEWAY"; }

files() {
  # AGENT_LOG_DIR keeps the app's own log lines (the eligible scan imports the app) out of
  # the gateway's telemetry/logs, which is on the mounted volume.
  docker run --rm -i --volumes-from "$GATEWAY" -w /app -e AGENT_LOG_DIR=/tmp/fre1609-logs \
    --entrypoint .venv/bin/python \
    "$(gateway_image)" -c "$FILES_PY" "$@"
}

psql_prod() {
  docker exec -i "$PG" psql -U "$PG_USER" -d "$PG_DB" -v ON_ERROR_STOP=1 -At -c "$1"
}

require_stopped() {
  [ "$(docker inspect -f '{{.State.Running}}' "$GATEWAY")" = "false" ] \
    || die "$GATEWAY is running. Stop it first: docker stop $GATEWAY"
}

require_dir_arg() {
  [ -n "${1:-}" ] || die "missing <dir> argument"
}

all_counts() {
  files counts
  psql_prod "$COUNT_SQL"
}

cmd_dry_run() {
  echo "== Entries the reset deletes (name, status, provenance, fingerprint, seen_count, linear) =="
  files list
  echo "== Counts =="
  all_counts
  echo "== Promotion scan (read-only) =="
  files eligible | grep -E '^(promotion_eligible[|]|eligible )'
}

cmd_backup() {
  local dir="$1"
  require_dir_arg "$dir"
  require_stopped
  [ ! -e "$dir" ] || die "$dir already exists; use a new directory"
  mkdir -p "$dir"
  all_counts > "$dir/counts.txt"
  files tar > "$dir/captains_log.tar.gz"
  docker exec "$PG" pg_dump -U "$PG_USER" -d "$PG_DB" -n sysgraph -Fc > "$dir/sysgraph.dump"
  (cd "$dir" && sha256sum captains_log.tar.gz sysgraph.dump counts.txt > SHA256SUMS)
  echo "== Backup in $dir =="
  ls -l "$dir"
  cat "$dir/SHA256SUMS"
}

cleanup_restore_container() {
  docker rm -f "$RESTORE_CONTAINER" > /dev/null 2>&1 || true
}

wait_for_scratch_postgres() {
  local i
  for i in $(seq 1 90); do
    if docker exec "$RESTORE_CONTAINER" psql -U postgres -At -c 'SELECT 1' > /dev/null 2>&1; then
      return 0
    fi
    sleep 1
  done
  die "scratch Postgres does not accept queries"
}

cmd_verify_backup() {
  local dir="$1"
  require_dir_arg "$dir"
  require_stopped
  rm -f "$dir/VERIFIED"
  (cd "$dir" && sha256sum -c SHA256SUMS)

  # The production state must still equal the frozen manifest: nothing ran since the backup.
  diff <(all_counts) "$dir/counts.txt" || die "production counts changed since the backup"

  local want_files got_files
  want_files=$(awk -F'|' '$1 ~ /_cl_files$/ {s += $2} END {print s + 0}' "$dir/counts.txt")
  got_files=$(tar tzf "$dir/captains_log.tar.gz" | wc -l)
  [ "$want_files" -eq "$got_files" ] || die "tarball holds $got_files files, manifest says $want_files"
  echo "tarball: $got_files files (manifest $want_files)"

  local pg_image
  pg_image=$(docker inspect -f '{{.Config.Image}}' "$PG")
  cleanup_restore_container
  trap cleanup_restore_container EXIT
  docker run -d --name "$RESTORE_CONTAINER" \
    -e POSTGRES_PASSWORD="$(head -c 16 /dev/urandom | od -An -tx1 | tr -d ' \n')" \
    "$pg_image" > /dev/null
  wait_for_scratch_postgres
  # The image's first-start init restarts the server once; wait again past the restart.
  sleep 3
  wait_for_scratch_postgres
  docker exec -i "$RESTORE_CONTAINER" pg_restore -U postgres -d postgres --no-owner --no-privileges \
    --exit-on-error < "$dir/sysgraph.dump"
  local restored
  restored=$(docker exec -i "$RESTORE_CONTAINER" psql -U postgres -d postgres -v ON_ERROR_STOP=1 -At \
    -c "$COUNT_SQL")
  diff <(echo "$restored") <(grep -v '_cl_files|' "$dir/counts.txt") \
    || die "restored sysgraph counts differ from production"
  echo "restored sysgraph counts match production:"
  echo "$restored"
  cleanup_restore_container
  trap - EXIT

  sha256sum "$dir/SHA256SUMS" | cut -d' ' -f1 > "$dir/VERIFIED"
  echo "VERIFIED: backup checksums, file count and sysgraph restore all pass"
}

cmd_reset() {
  local dir="$1"
  require_dir_arg "$dir"
  require_stopped
  [ -f "$dir/VERIFIED" ] || die "no $dir/VERIFIED: run verify-backup first"
  (cd "$dir" && sha256sum -c --quiet SHA256SUMS) || die "backup checksums no longer match"
  [ "$(sha256sum "$dir/SHA256SUMS" | cut -d' ' -f1)" = "$(cat "$dir/VERIFIED")" ] \
    || die "SHA256SUMS changed after verification"
  diff <(all_counts) "$dir/counts.txt" || die "production counts changed since the backup"

  echo "== Deleting sysgraph proposal state =="
  psql_prod "$RESET_SQL"
  echo "== Deleting Captain's Log files =="
  files delete
  cmd_verify
}

cmd_verify() {
  local counts eligible bad
  counts=$(all_counts)
  eligible=$(files eligible | grep '^promotion_eligible|')
  echo "$counts"
  echo "$eligible"
  bad=$(printf '%s\n%s\n' "$counts" "$eligible" | awk -F'|' '$2 != 0')
  [ -z "$bad" ] || die "non-zero counts after reset: $bad"
  echo "OK: every count is 0"
}

case "${1:-}" in
  dry-run) cmd_dry_run ;;
  backup) cmd_backup "${2:-}" ;;
  verify-backup) cmd_verify_backup "${2:-}" ;;
  reset) cmd_reset "${2:-}" ;;
  verify) cmd_verify ;;
  *) die "usage: $0 dry-run | backup <dir> | verify-backup <dir> | reset <dir> | verify" ;;
esac
