#!/usr/bin/env python3
"""One-shot, idempotent backfill: stamp a legacy sentinel onto Captain's Log entries whose
``proposed_change.source`` predates the field (FRE-1001, ADR-0125 D1).

ADR-0105 introduced ``ProposedChange.source``; ADR-0125 D1 makes it non-nullable, so a stored
entry written before ADR-0105 (or by an early producer bug) no longer validates via
``CaptainLogEntry.model_validate()`` -- every current disk-read path
(``PromotionPipeline.scan_promotable_entries``, ``run_backfill`` in ``captains_log/backfill.py``)
already catches that exception and skips the entry rather than crash, so nothing breaks at
runtime, but an unmigrated legacy entry becomes permanently unpromotable and unindexable until
its ``source`` key is fixed.

This script stamps ``ProposalSource.LEGACY_UNATTRIBUTABLE`` (never a claim of recovered
provenance -- see ``producer_dimension()``'s docstring in ``captains_log/models.py``) onto any
``CL-*.json`` whose ``proposed_change.source`` is missing or explicitly ``null``. A non-null
``source`` that is NOT a valid ``ProposalSource`` value (malformed data, a different failure
class than "missing") is left untouched and reported separately -- silently overwriting a
malformed-but-present value would hide a real data-integrity problem instead of surfacing it.

Idempotent: only missing/null-source entries are rewritten; an already-valid or
already-migrated file produces zero writes on a re-run. Writes are atomic (temp file +
``Path.replace()``) so an interrupted run cannot leave a truncated JSON file.

Verified against the deployed substrate (2026-07-27, ``cloud-sim-seshat-gateway``'s
``seshat_captains_log_cloud`` volume): 38 entries, 24 with no ``proposed_change`` at all
(not applicable), 14 with one (10 ``reflection``, 4 ``statistical_detector``, 0 null) -- the
live store already satisfies AC-1's zero-null requirement, so this script's live run is
defense-in-depth (any environment that still writes/reads Captain's Log entries outside that
volume) rather than a blocking prerequisite for this ticket's model change.

Usage:
    uv run python scripts/migrate_fre1001_captains_log_source_backfill.py --dry-run --confirm-prod
    uv run python scripts/migrate_fre1001_captains_log_source_backfill.py --confirm-prod
    uv run python scripts/migrate_fre1001_captains_log_source_backfill.py --log-dir /app/telemetry/captains_log --confirm-prod
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from personal_agent.captains_log.models import ProposalSource
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

_SENTINEL = ProposalSource.LEGACY_UNATTRIBUTABLE.value
_VALID_SOURCES = {member.value for member in ProposalSource}


@dataclass
class MigrationReport:
    """Structured, printable record of one migration run."""

    scanned: int = 0
    missing_or_null: int = 0
    already_valid: int = 0
    not_applicable: int = 0
    invalid: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    migrated: list[str] = field(default_factory=list)
    dry_run: bool = False

    @property
    def success(self) -> bool:
        """False if any row remains in a state AC-1 forbids after this run."""
        return not self.invalid and not self.failed


def _classify(json_file: Path) -> tuple[str, dict[str, Any] | None]:
    """Classify one file's proposed_change.source without writing anything.

    Args:
        json_file: The ``CL-*.json`` file to inspect.

    Returns:
        ``(status, data)`` where ``status`` is one of ``"missing_or_null"``,
        ``"already_valid"``, ``"not_applicable"`` (top-level JSON isn't an
        object, or it has no ``proposed_change`` object), ``"invalid"``
        (non-null but not a valid ``ProposalSource`` value, including a
        non-string ``source``), or ``"failed"`` (unreadable / not valid
        JSON). ``data`` is the parsed JSON dict, or ``None`` for
        ``"failed"``/``"not_applicable"`` when the top level isn't a dict.
    """
    try:
        data = json.loads(json_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "failed", None

    # A syntactically valid but non-object top level (`[]`, `"x"`, `123`,
    # `null`, ...) is not a JSONDecodeError -- guard explicitly rather than
    # let `.get()` raise AttributeError below.
    if not isinstance(data, dict):
        return "not_applicable", None

    pc = data.get("proposed_change")
    if not isinstance(pc, dict):
        return "not_applicable", data

    source = pc.get("source")
    if source is None:
        return "missing_or_null", data
    # `source in _VALID_SOURCES` would raise TypeError for an unhashable
    # malformed value (a dict/list) -- an isinstance guard makes any
    # non-string source classify as "invalid" instead of crashing the run.
    if isinstance(source, str) and source in _VALID_SOURCES:
        return "already_valid", data
    return "invalid", data


def select_migration_targets(log_dir: Path) -> list[Path]:
    """Return every ``CL-*.json`` file whose source is missing/null (scan-only, no writes).

    Args:
        log_dir: The Captain's Log directory to scan.

    Returns:
        Matching file paths, sorted.
    """
    return [f for f in sorted(log_dir.glob("CL-*.json")) if _classify(f)[0] == "missing_or_null"]


def migrate_file(json_file: Path, data: dict[str, Any]) -> None:
    """Stamp the legacy sentinel onto one file's proposed_change.source, atomically.

    Writes to a same-directory temp file and ``Path.replace()``s it over the
    original, so an interrupted run cannot leave a truncated JSON file.
    ``tempfile.mkstemp`` always creates its file mode ``0600``; since
    ``replace()`` is a rename, the destination would silently inherit that
    narrowed mode unless explicitly restored to the original file's mode
    first (a real hazard if the app reads this substrate as a different
    user/UID than this script runs as, e.g. inside the Docker-mounted
    volume this script's module docstring describes).

    Args:
        json_file: The file to rewrite in place.
        data: The already-parsed contents of ``json_file`` (as returned by
            ``_classify``) -- mutated in place and re-serialized.
    """
    data["proposed_change"]["source"] = _SENTINEL
    original_mode = json_file.stat().st_mode
    fd, tmp_name = tempfile.mkstemp(
        dir=json_file.parent, prefix=f".{json_file.name}.", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)
    try:
        with open(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(data, indent=2, default=str))
        tmp_path.chmod(original_mode)
        tmp_path.replace(json_file)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def run_migration(log_dir: Path, *, dry_run: bool) -> MigrationReport:
    """Migrate every missing/null-source entry under log_dir. Never mutates on dry_run.

    Args:
        log_dir: The Captain's Log directory to scan and (unless ``dry_run``) rewrite.
        dry_run: When True, classify and report but issue zero writes.

    Returns:
        A populated :class:`MigrationReport`.
    """
    report = MigrationReport(dry_run=dry_run)
    for json_file in sorted(log_dir.glob("CL-*.json")):
        report.scanned += 1
        status, data = _classify(json_file)
        if status == "already_valid":
            report.already_valid += 1
        elif status == "not_applicable":
            report.not_applicable += 1
        elif status == "invalid":
            report.invalid.append(json_file.name)
        elif status == "failed":
            report.failed.append(json_file.name)
        elif status == "missing_or_null":
            report.missing_or_null += 1
            if not dry_run:
                assert data is not None
                migrate_file(json_file, data)
            report.migrated.append(json_file.name)
    return report


def _print_summary(report: MigrationReport, log_dir: Path) -> None:
    mode = "DRY-RUN (no writes)" if report.dry_run else "APPLIED"
    print(f"\n=== FRE-1001 captains_log source backfill [{mode}] dir={log_dir} ===")
    print(f"scanned: {report.scanned}")
    print(f"already_valid: {report.already_valid}  not_applicable: {report.not_applicable}")
    verb = "would migrate" if report.dry_run else "migrated"
    print(f"missing_or_null -> {verb}: {report.missing_or_null}")
    for name in report.migrated:
        print(f"  {verb}: {name}")
    if report.invalid:
        print(f"INVALID (non-null, not a valid ProposalSource -- left untouched): {report.invalid}")
    if report.failed:
        print(f"FAILED to read/parse: {report.failed}")
    print(f"success: {report.success}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "FRE-1001: backfill ProposalSource.LEGACY_UNATTRIBUTABLE onto Captain's Log "
            "entries with a missing/null proposed_change.source. Idempotent."
        )
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=None,
        help="Captain's Log directory to scan (default: telemetry/captains_log under the repo root).",
    )
    parser.add_argument(
        "--dry-run", action="store_true", default=False, help="Preview; write nothing."
    )
    parser.add_argument(
        "--confirm-prod",
        action="store_true",
        default=False,
        help="Required when AGENT_ENVIRONMENT is not 'test'. Confirms intent to write production data.",
    )
    return parser.parse_args()


def main() -> int:
    """CLI entrypoint with the house prod-write env guard."""
    args = _parse_args()

    from personal_agent.config import settings
    from personal_agent.config.env_loader import Environment

    if settings.environment != Environment.TEST and not args.confirm_prod and not args.dry_run:
        print(
            "ERROR: Running against non-TEST environment without --confirm-prod.\n"
            "This script writes to the Captain's Log substrate.\n"
            "Re-run with --confirm-prod if you intend to modify production data, or "
            "--dry-run to preview.",
            file=sys.stderr,
        )
        return 2

    log_dir = args.log_dir
    if log_dir is None:
        project_root = Path(__file__).parent.parent
        log_dir = project_root / "telemetry" / "captains_log"

    if not log_dir.exists():
        print(f"log dir does not exist: {log_dir}", file=sys.stderr)
        return 1

    report = run_migration(log_dir, dry_run=args.dry_run)
    _print_summary(report, log_dir)
    log.info(
        "fre1001_captains_log_source_backfill_done",
        dry_run=args.dry_run,
        scanned=report.scanned,
        migrated=len(report.migrated),
        invalid=len(report.invalid),
        failed=len(report.failed),
    )
    return 0 if report.success else 3


if __name__ == "__main__":
    sys.exit(main())
