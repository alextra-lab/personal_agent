"""Captain's Log Manager for creating and managing agent reflection entries."""

import pathlib
import re
import subprocess
from datetime import datetime, timezone

from personal_agent.captains_log.models import (
    CaptainLogEntry,
    CaptainLogEntryType,
    TelemetryRef,
)
from personal_agent.telemetry import CAPTAINS_LOG_ENTRY_CREATED, get_logger

log = get_logger(__name__)


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
    # Convert to lowercase, replace spaces and special chars with hyphens
    sanitized = re.sub(r"[^\w\s-]", "", title.lower())
    sanitized = re.sub(r"[-\s]+", "-", sanitized)
    # Limit length
    return sanitized[:50]


class CaptainLogManager:
    """Manager for Captain's Log entries.

    Handles creation, writing, and git committing of agent reflection entries.
    """

    def __init__(self, log_dir: pathlib.Path | None = None):
        """Initialize Captain's Log Manager.

        Args:
            log_dir: Optional custom log directory (defaults to ../../docs/architecture_decisions/captains_log).
        """
        self.log_dir = log_dir or _get_captains_log_dir()
        self.log_dir.mkdir(parents=True, exist_ok=True)

    def write_entry(self, entry: CaptainLogEntry) -> pathlib.Path:
        """Write a Captain's Log entry to a JSON file.

        Args:
            entry: The entry to write.

        Returns:
            Path to the written file.

        Raises:
            OSError: If file cannot be written.
        """
        # Generate entry ID if not set
        if not entry.entry_id:
            # Extract trace_id from telemetry_refs for scenario tracking
            trace_id = None
            if entry.telemetry_refs and len(entry.telemetry_refs) > 0:
                trace_id = entry.telemetry_refs[0].trace_id
            entry.entry_id = _generate_entry_id(entry.timestamp, trace_id=trace_id)

        # Generate filename: CL-YYYYMMDD-HHMMSS-<trace>-NNN-title.json
        title_slug = _sanitize_filename(entry.title)
        filename = f"{entry.entry_id}-{title_slug}.json"
        file_path = self.log_dir / filename

        # Write JSON content (pretty-printed)
        json_content = entry.model_dump_json_pretty()
        file_path.write_text(json_content, encoding="utf-8")

        # Emit telemetry
        log.info(
            CAPTAINS_LOG_ENTRY_CREATED,
            entry_id=entry.entry_id,
            entry_type=entry.type.value,
            title=entry.title,
            file_path=str(file_path),
        )

        return file_path

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
            supporting_metrics=supporting_metrics or [],
            telemetry_refs=[TelemetryRef(trace_id=trace_id)] if trace_id else [],
        )

        file_path = self.write_entry(entry)

        if auto_commit:
            self.commit_to_git(entry_id, file_path=file_path)

        return entry
