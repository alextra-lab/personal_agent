# FRE-1609 — Captain's Log reset, archive-aware dedup, no reinforcement after promotion

Ticket: FRE-1609 (part 2 of 4). Spec: `docs/specs/SELF_IMPROVEMENT_FEEDBACK_LOOP_SPEC.md` §3, §6.4.
Owner decision 2026-10-14: clear the history and start from zero, and fix the bugs.

## Scope

1. **Defect 2 — dedup and `_mark_promoted` see the archive.** The lifecycle job moves a CL file
   that is older than 14 days (mtime) to `<telemetry>/archive/captains_log_reflections/YYYY-MM/<name>.json.gz`.
   Write-time dedup and `_mark_promoted` read only the live directory.
2. **Defect 3 — no reinforcement after promotion.** `read_before_emit` picks the newest row of the
   `(source, category, scope)` group, also a promoted one, and increments its `seen_count`.
3. **Reset tooling.** A host-side script for master: dry run, backup, backup restore check, reset,
   final count check.
4. **Docs.** Spec §3 records the reset. Spec §6.4 matches the fixed code.

## Design

### D1 — archive-aware dedup (new module `src/personal_agent/captains_log/archive.py`)

- `reflection_archive_dir(log_dir: Path) -> Path` returns `log_dir.parent / "archive" / "captains_log_reflections"`.
  This is the layout that `telemetry/lifecycle_manager.py` writes (`_telemetry_root()/archive/<data_type>/YYYY-MM/`).
  A contract test pins the two layouts together.
- `find_archived(log_dir, predicate) -> Path | None` scans `*/CL-*.json.gz`, newest name first, and
  returns the first file whose JSON matches the predicate. An unreadable file is skipped.
- `restore_archived(gz_path, log_dir) -> Path` decompresses the file to `log_dir/<name>` (atomic
  temp file + replace), then deletes the `.gz`. The entry keeps its `entry_id`, status and
  `linear_issue_id`.

`CaptainLogManager._find_entry_by_fingerprint`: live scan first (unchanged). On a miss, it calls
`find_archived` with the same predicate (same fingerprint, same `eval_mode`). On a hit, it restores
the file and returns the live path. `_merge_into_existing` then merges as today.

Why restore and not merge in the `.gz`: the archive is a storage tier, not a decision. A
re-sighting of an awaiting idea must behave as if the file never aged out. A promoted (APPROVED)
entry stays APPROVED after the restore, so it absorbs the sighting and is never rescanned
(spec Guard 1).

`PromotionPipeline._mark_promoted`: if the live glob `{entry_id}-*.json` finds nothing, it calls
`find_archived` for that `entry_id`, restores the file, and marks it.

### D2 — exclude promoted rows from reinforcement (`sysgraph/repository.py`)

- `_FIND_AWAITING_PROPOSAL_QUERY` adds
  `AND NOT EXISTS (SELECT 1 FROM sysgraph.promoted_to pt WHERE pt.proposal_id = sysgraph.proposal.id)`.
  The name already says "awaiting". The query now does what the name says.
- `_GENERATION_TIME_UPSERT_PROPOSAL`: the `ON CONFLICT (fingerprint) DO UPDATE` gets the same
  `WHERE NOT EXISTS (...)` clause. Without it, a sighting whose fingerprint equals the promoted
  row's fingerprint increments that row through the conflict path.
- If the guarded upsert returns no row (the fingerprint belongs to a promoted row), the result is
  `generate_new` with `proposal_id=None`. The producer keeps its proposal. The CL write-time dedup
  (D1) then absorbs it into the promoted entry. No count rises.
- After a promotion, a new sighting of the same group starts a new row at `seen_count` 1. It must
  reach the bar again from zero. Gating of decided kinds stays with FRE-1610.

### D3 — reset script `scripts/reset_fre1609_captains_log.sh` (host side, run by master)

Subcommands. Each one is safe to re-run.

| Command | Effect | Writes |
| -- | -- | -- |
| `dry-run` | Lists every live `CL-*.json` (name, status, fingerprint prefix, `linear_issue_id`) and every archive file, then the sysgraph row counts per table and the promotion scan eligible count | none |
| `backup <dir>` | `<dir>/captains_log.tar.gz` (live `CL-*.json` + `archive/captains_log_reflections`), `<dir>/sysgraph.dump` (`pg_dump -n sysgraph -Fc`), `<dir>/SHA256SUMS`, sizes | host `<dir>` only |
| `verify-backup <dir>` | Checks the checksums. Counts tar members against the dry-run counts. Restores the dump into a throwaway container of the same image (`pgvector/pgvector:pg17`), compares `sysgraph.proposal`/`ticket`/`promoted_to` counts with production, removes the container, writes `<dir>/VERIFIED` | host `<dir>` + a throwaway container |
| `reset <dir>` | Refuses unless `<dir>/VERIFIED` exists and the checksums still match. One transaction: delete `correlates_with`/`influence` rows that reference proposals, then `outcome`, `ticket`, `proposal`, `signal` (cascades remove `promoted_to`, `produced`, `derives_from`). Then delete live `CL-*.json` and the archive files. | production |
| `verify` | Counts must be 0: live CL files, archive files, `proposal`, `ticket`, `promoted_to`; promotion scan eligible = 0. Exit 1 otherwise. | none |

Codex plan review (accepted): ordering alone cannot make two stores atomic. `backup`,
`verify-backup` and `reset` therefore refuse while the gateway runs, and `reset` refuses if any
count differs from the frozen `counts.txt` manifest. `verify` checks every table the reset
empties. The file work runs in a throwaway container of the gateway image (`--volumes-from`,
`AGENT_LOG_DIR` redirected), because the gateway is stopped.

Order inside `reset`: sysgraph first, files second. If a sighting lands between the two steps, it
can only merge into an old file that the file step then deletes. The reverse order lets an old
sysgraph row stamp its count onto a new file.

Kept: `captains_log/captures*`, `sysgraph.stat`, ES indices, Linear tickets,
`feedback_history/suppressed_fingerprints.json` (Guard 2 tombstones).

The backup directory is outside every Docker volume (default `/opt/seshat-backups/fre1609-<UTC stamp>`).
Delete it when FRE-1611 closes.

## Steps (TDD)

1. AC-4 tests in `tests/personal_agent/sysgraph/test_read_before_emit.py` (integration, test
   Postgres :5433): promoted row + matching proposal with a different fingerprint → promoted
   `seen_count` unchanged, a new row is created. Same fingerprint → unchanged, `proposal_id` None.
   Run: `uv run pytest tests/personal_agent/sysgraph/test_read_before_emit.py -q` → fail, fix, pass.
2. AC-3 tests in `tests/test_captains_log/test_archive_dedup.py` (tmp_path only): archived entry with
   fingerprint F + new sighting of F → no new file, the archived entry is restored with
   `seen_count` + 1 and keeps its status. Eval provenance mismatch → no match. `_mark_promoted`
   restores and marks an archived entry. Layout contract test with `lifecycle_manager`.
   Run: `uv run pytest tests/test_captains_log/test_archive_dedup.py -q`.
3. Implement D1 and D2.
4. Write the script. Check it with `bash -n` and `shellcheck`. Run `dry-run` against production
   (read-only) and post the list on the ticket (AC-5).
5. Spec §3 and §6.4.
6. Gates: `make test`, `make mypy`, `make ruff-check`, `make ruff-format`, `pre-commit run --all-files`.

## Acceptance criteria → proof

| AC | Proof |
| -- | -- |
| AC-1 reset complete | `reset` + `verify` output after master runs it (post-deploy, after FRE-1608 deploy) |
| AC-2 backup exists and restores | `backup` + `verify-backup` output: sizes, sha256, restored counts |
| AC-3 dedup sees the archive | `test_archive_dedup.py` |
| AC-4 no reinforcement after promotion | `test_read_before_emit.py` new tests |
| AC-5 dry-run list posted before any write | ticket comment from `dry-run` before `reset` |

Diff class: escalated (destructive code, production write path).
