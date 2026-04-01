"""Captain's Log Manager for creating and managing agent reflection entries.

Extended by ADR-0030: fingerprint-based deduplication on write.
"""

import json as _json
import pathlib
import re
import subprocess
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    TelemetryRef,
)
from personal_agent.captains_log.suppression import is_fingerprint_suppressed
from personal_agent.telemetry import CAPTAINS_LOG_ENTRY_CREATED, get_logger

log = get_logger(__name__)

# Index name pattern for Captain's Log reflections (Phase 2.3)
REFLECTIONS_INDEX_PREFIX = "agent-captains-reflections"

if TYPE_CHECKING:
    from personal_agent.telemetry.es_handler import ElasticsearchHandler


def _normalize_reflection_doc_for_es(doc: dict[str, object]) -> dict[str, object]:
    """Normalize reflection document field types for stable ES mappings.

    Elasticsearch cannot accept mixed numeric mappings for the same field in one
    index (e.g., `float` then `long`). We normalize `metrics_structured.value`
    integers to floats before indexing.
    """
    metrics = doc.get("metrics_structured")
    if not isinstance(metrics, list):
        return doc

    for metric in metrics:
        if not isinstance(metric, dict):
            continue
        value = metric.get("value")
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            metric["value"] = float(value)
    return doc


def _get_captains_log_dir() -> pathlib.Path:
    """Get the Captain's Log directory path.

    Returns:
        Path to telemetry/captains_log directory.
    """
    # Get project root and navigate to telemetry/captains_log
    project_root = pathlib.Path(__file__).parent.parent.parent.parent
    log_dir = project_root / "telemetry" / "captains_log"
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir


def _generate_entry_id(date: datetime | None = None, trace_id: str | None = None) -> str:
    """Generate a unique entry ID with timestamp and optional trace tracking.

    Format: CL-YYYYMMDD-HHMMSS-<trace_prefix>-NNN
    - YYYYMMDD-HHMMSS: ISO timestamp for chronological sorting
    - trace_prefix: First 8 chars of trace_id for scenario grouping (optional)
    - NNN: Sequence number within same second

    Args:
        date: Optional datetime to use (defaults to now UTC).
        trace_id: Optional trace_id for scenario tracking (enables test comparison).

    Returns:
        Entry ID string.

    Examples:
        CL-20260117-170613-a9e965fb-001  (with trace_id for scenario grouping)
        CL-20260117-170613-001            (without trace_id)
    """
    if date is None:
        date = datetime.now(timezone.utc)

    # Format: YYYYMMDD-HHMMSS for sortable timestamp
    timestamp_str = date.strftime("%Y%m%d-%H%M%S")
    log_dir = _get_captains_log_dir()

    # Add trace prefix if provided (for scenario grouping/comparison)
    # Defensive: trace_id can be a coroutine if reflection runs in wrong context (e.g. to_thread).
    if trace_id is not None and not isinstance(trace_id, str):
        trace_id = None
    if trace_id and hasattr(trace_id, "__await__"):  # coroutine
        trace_id = None
    trace_prefix = f"{trace_id[:8]}-" if trace_id else ""

    # Find existing entries for this timestamp+trace combo
    pattern = re.compile(rf"CL-{timestamp_str}-{re.escape(trace_prefix)}(\d{{3}})")
    existing_numbers: list[int] = []

    if log_dir.exists():
        # Fixed: Look for .json files, not .yaml (bug causing all to be 001)
        for file in log_dir.glob(f"CL-{timestamp_str}-{trace_prefix}*.json"):
            match = pattern.match(file.stem)
            if match:
                existing_numbers.append(int(match.group(1)))

    # Get next sequence number
    if existing_numbers:
        next_num = max(existing_numbers) + 1
    else:
        next_num = 1

    return f"CL-{timestamp_str}-{trace_prefix}{next_num:03d}"


def _sanitize_filename(title: str) -> str:
    """Sanitize title for use in filename.

    Args:
        title: Entry title.

    Returns:
        Sanitized filename-safe string.
    """
    # Defensive: title can be non-string (e.g. coroutine) from DSPy in to_thread context.
    if not isinstance(title, str) or hasattr(title, "__await__"):
        title = "task"
    # Convert to lowercase, replace spaces and special chars with hyphens
    sanitized = re.sub(r"[^\w\s-]", "", title.lower())
    sanitized = re.sub(r"[-\s]+", "-", sanitized)
    # Limit length
    return sanitized[:50]


class CaptainLogManager:
    """Manager for Captain's Log entries.

    Handles creation, writing, and git committing of agent reflection entries.
    """

    _default_es_handler: "ElasticsearchHandler | None" = None

    @classmethod
    def set_default_es_handler(cls, es_handler: "ElasticsearchHandler | None") -> None:
        """Set default ES handler used by manager instances.

        Args:
            es_handler: Elasticsearch handler or None.
        """
        cls._default_es_handler = es_handler

    def __init__(
        self,
        log_dir: pathlib.Path | None = None,
        es_handler: "ElasticsearchHandler | None" = None,
    ):
        """Initialize Captain's Log Manager.

        Args:
            log_dir: Optional custom log directory (defaults to ../../docs/architecture_decisions/captains_log).
            es_handler: Optional Elasticsearch handler for reflection indexing.
        """
        self.log_dir = log_dir or _get_captains_log_dir()
        self.es_handler = es_handler or self.__class__._default_es_handler
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def save_entry(
        self,
        entry: CaptainLogEntry,
        es_handler: "ElasticsearchHandler | None" = None,
    ) -> pathlib.Path | None:
        """Save a Captain's Log entry to a JSON file.

        ADR-0030 dedup: if the entry has a proposed_change with a fingerprint,
        check for an existing entry (any status) that shares the same
        fingerprint. When found, merge (increment seen_count) instead of
        writing a new file.

        ADR-0040: if the fingerprint is under Linear rejection suppression,
        skip the write and return None.

        Args:
            entry: The entry to write.
            es_handler: Optional Elasticsearch handler override.

        Returns:
            Path to the written (or updated) file, or None if suppressed.

        Raises:
            OSError: If file cannot be written.
        """
        # --- ADR-0040: rejection suppression ---
        fingerprint = (
            entry.proposed_change.fingerprint
            if entry.proposed_change and entry.proposed_change.fingerprint
            else None
        )
        if fingerprint and is_fingerprint_suppressed(fingerprint):
            log.info(
                "captains_log_proposal_suppressed",
                fingerprint=fingerprint,
                reason="rejected_via_feedback",
            )
            return None

        # --- ADR-0030 / ADR-0040: fingerprint-based dedup ---
        if fingerprint:
            existing_path = self._find_entry_by_fingerprint(fingerprint)
            if existing_path is not None:
                return self._merge_into_existing(existing_path, entry, es_handler)

        # --- Normal write path ---
        if not entry.entry_id:
            trace_id = None
            if entry.telemetry_refs and len(entry.telemetry_refs) > 0:
                raw = entry.telemetry_refs[0].trace_id
                # Defensive: avoid passing coroutine to _generate_entry_id (re.escape fails).
                if isinstance(raw, str) and not getattr(raw, "__await__", None):
                    trace_id = raw
            entry.entry_id = _generate_entry_id(entry.timestamp, trace_id=trace_id)

        title_slug = _sanitize_filename(entry.title)
        filename = f"{entry.entry_id}-{title_slug}.json"
        file_path = self.log_dir / filename

        json_content = entry.model_dump_json_pretty()
        file_path.write_text(json_content, encoding="utf-8")

        log.info(
            CAPTAINS_LOG_ENTRY_CREATED,
            entry_id=entry.entry_id,
            entry_type=entry.type.value,
            title=entry.title,
            file_path=str(file_path),
        )

        if entry.type in {CaptainLogEntryType.REFLECTION, CaptainLogEntryType.CONFIG_PROPOSAL}:
            date_str = entry.timestamp.strftime("%Y-%m-%d")
            index_name = f"{REFLECTIONS_INDEX_PREFIX}-{date_str}"
            doc = _normalize_reflection_doc_for_es(entry.model_dump(mode="json"))
            handler = es_handler or self.es_handler
            schedule_es_index(index_name, doc, es_handler=handler, doc_id=entry.entry_id)

        return file_path

    # ------------------------------------------------------------------
    # ADR-0030 dedup helpers
    # ------------------------------------------------------------------

    def _find_entry_by_fingerprint(self, fingerprint: str) -> pathlib.Path | None:
        """Scan on-disk entries for a proposal with matching fingerprint (any status).

        ADR-0040: matches any status so promoted (APPROVED) entries still absorb
        duplicate reflections without creating a new promotable CL file.

        Args:
            fingerprint: The fingerprint to match.

        Returns:
            Path to the matching file, or None.
        """
        for json_file in sorted(self.log_dir.glob("CL-*.json"), reverse=True):
            try:
                data = _json.loads(json_file.read_text(encoding="utf-8"))
            except Exception:
                continue

            pc = data.get("proposed_change")
            if pc and pc.get("fingerprint") == fingerprint:
                return json_file

        return None

    def _merge_into_existing(
        self,
        existing_path: pathlib.Path,
        new_entry: CaptainLogEntry,
        es_handler: "ElasticsearchHandler | None" = None,
    ) -> pathlib.Path:
        """Increment seen_count on an existing entry instead of creating a duplicate.

        Also appends the new entry's entry_id (if set) to related_entry_ids and
        refreshes supporting_metrics if the new entry carries different ones.

        Args:
            existing_path: Path to the existing JSON file.
            new_entry: The incoming (duplicate) entry.
            es_handler: Optional Elasticsearch handler override.

        Returns:
            Path to the updated file.
        """
        data = _json.loads(existing_path.read_text(encoding="utf-8"))
        pc = data.get("proposed_change", {})

        pc["seen_count"] = pc.get("seen_count", 1) + 1

        new_id = new_entry.entry_id or ""
        related = pc.get("related_entry_ids", [])
        if new_id and new_id not in related:
            related.append(new_id)
        pc["related_entry_ids"] = related

        data["proposed_change"] = pc
        existing_path.write_text(_json.dumps(data, indent=2, default=str), encoding="utf-8")

        log.info(
            "captains_log_proposal_merged",
            existing_entry_id=data.get("entry_id"),
            fingerprint=pc.get("fingerprint"),
            seen_count=pc["seen_count"],
        )

        # Re-index the updated doc
        existing_entry = CaptainLogEntry.model_validate(data)
        if existing_entry.type in {
            CaptainLogEntryType.REFLECTION,
            CaptainLogEntryType.CONFIG_PROPOSAL,
        }:
            date_str = existing_entry.timestamp.strftime("%Y-%m-%d")
            index_name = f"{REFLECTIONS_INDEX_PREFIX}-{date_str}"
            doc = _normalize_reflection_doc_for_es(existing_entry.model_dump(mode="json"))
            handler = es_handler or self.es_handler
            schedule_es_index(index_name, doc, es_handler=handler, doc_id=existing_entry.entry_id)

        return existing_path

    def write_entry(
        self,
        entry: CaptainLogEntry,
        es_handler: "ElasticsearchHandler | None" = None,
    ) -> pathlib.Path | None:
        """Write entry compatibility wrapper (delegates to save_entry)."""
        return self.save_entry(entry, es_handler=es_handler)

    def commit_to_git(
        self, entry_id: str, message: str | None = None, file_path: pathlib.Path | None = None
    ) -> bool:
        """Commit a Captain's Log entry to git.

        Args:
            entry_id: Entry ID to commit.
            message: Optional commit message (defaults to "Captain's Log: [title]").
            file_path: Optional path to entry file (will search if not provided).

        Returns:
            True if commit succeeded, False otherwise.
        """
        # Find file if not provided
        if file_path is None:
            matching_files = list(self.log_dir.glob(f"{entry_id}-*.json"))
            if not matching_files:
                log.warning(
                    "captains_log_file_not_found",
                    entry_id=entry_id,
                    log_dir=str(self.log_dir),
                )
                return False
            file_path = matching_files[0]

        # Use default message if not provided
        if message is None:
            # Try to read title from file
            try:
                import json

                content = json.loads(file_path.read_text(encoding="utf-8"))
                title = content.get("title", entry_id)
                message = f"Captain's Log: {title}"
            except Exception as e:
                log.warning(
                    "captains_log_read_title_failed",
                    entry_id=entry_id,
                    error=str(e),
                )
                message = f"Captain's Log: {entry_id}"

        # Check if we're in a git repository
        try:
            subprocess.run(
                ["git", "rev-parse", "--git-dir"],
                cwd=self.log_dir.parent.parent.parent,  # Project root
                capture_output=True,
                check=True,
                timeout=5,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            log.warning(
                "captains_log_git_not_available",
                entry_id=entry_id,
                reason="Not in git repository or git not available",
            )
            return False

        # Stage and commit
        try:
            # Stage the file
            subprocess.run(
                ["git", "add", str(file_path.relative_to(self.log_dir.parent.parent.parent))],
                cwd=self.log_dir.parent.parent.parent,
                check=True,
                timeout=5,
            )

            # Commit
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=self.log_dir.parent.parent.parent,
                check=True,
                timeout=5,
            )

            from personal_agent.telemetry import CAPTAINS_LOG_ENTRY_COMMITTED

            log.info(
                CAPTAINS_LOG_ENTRY_COMMITTED,
                entry_id=entry_id,
                commit_message=message,
            )
            return True

        except subprocess.CalledProcessError as e:
            log.warning(
                "captains_log_commit_failed",
                entry_id=entry_id,
                error=str(e),
            )
            return False
        except subprocess.TimeoutExpired:
            log.warning(
                "captains_log_commit_timeout",
                entry_id=entry_id,
            )
            return False

    def create_reflection_entry(
        self,
        title: str,
        rationale: str,
        trace_id: str | None = None,
        supporting_metrics: list[str] | None = None,
        auto_commit: bool = False,
    ) -> CaptainLogEntry:
        """Create and write a reflection entry.

        Convenience method for creating reflection entries after tasks.

        Args:
            title: Entry title.
            rationale: Reflection rationale.
            trace_id: Optional trace ID for telemetry reference.
            supporting_metrics: Optional list of supporting metrics.
            auto_commit: Whether to automatically commit to git.

        Returns:
            Created entry.
        """
        entry_id = _generate_entry_id(trace_id=trace_id)
        entry = CaptainLogEntry(
            entry_id=entry_id,
            type=CaptainLogEntryType.REFLECTION,
            title=title,
            rationale=rationale,
            proposed_change=None,
            supporting_metrics=supporting_metrics or [],
            metrics_structured=None,
            impact_assessment=None,
            reviewer_notes=None,
            experiment_design=None,
            expected_outcome=None,
            potential_implementation=None,
            telemetry_refs=[TelemetryRef(trace_id=trace_id, metric_name=None, value=None)]
            if trace_id
            else [],
        )

        file_path = self.write_entry(entry)

        if auto_commit and file_path is not None:
            self.commit_to_git(entry_id, file_path=file_path)

        return entry
