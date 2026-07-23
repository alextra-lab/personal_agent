"""Fast capture system for Captain's Log (Phase 2.2).

This module provides structured capture of task execution data without LLM processing.
Captures are written immediately during request processing, then processed later by
the second brain for deep reflection.
"""

import pathlib
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any
from uuid import UUID

import orjson
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.config import get_settings as _get_settings
from personal_agent.telemetry import get_logger

log = get_logger(__name__)

# Captain's Log capture index prefix (settings-driven for test/prod isolation — FRE-375)
_cl_settings = _get_settings()
CAPTURES_INDEX_PREFIX = f"{_cl_settings.captains_log_index_prefix}-captures"
# Per-sub-agent audit records (FRE-505) — sibling index in the captures family.
# Separate from the TaskCapture daily index so the differing doc shape does not
# pollute that index's mapping; still matched by the agent-captains-captures-*
# template (explicit text/float/nested properties added there for the new fields).
SUBAGENT_CAPTURES_INDEX_PREFIX = f"{CAPTURES_INDEX_PREFIX}-subagents"

if TYPE_CHECKING:
    from personal_agent.telemetry.es_handler import ElasticsearchHandler

_default_es_handler: "ElasticsearchHandler | None" = None


def set_default_es_handler(es_handler: "ElasticsearchHandler | None") -> None:
    """Set default ES handler used by write_capture when one is not provided.

    Args:
        es_handler: Elasticsearch handler or None.
    """
    global _default_es_handler
    _default_es_handler = es_handler


class TaskCapture(BaseModel):
    """Fast capture of task execution (no LLM, structured JSON).

    This is written immediately during request processing for later
    analysis by the second brain.
    """

    model_config = ConfigDict(populate_by_name=True)

    trace_id: str
    session_id: str
    timestamp: datetime
    user_message: str
    assistant_response: str | None = None
    steps: list[dict[str, Any]] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    duration_ms: float | None = None
    metrics_summary: dict[str, Any] | None = None
    outcome: str  # "completed", "failed", "timeout"
    memory_context_used: bool = False
    memory_conversations_found: int = 0
    input_tokens: int = Field(
        default=0, validation_alias=AliasChoices("input_tokens", "prompt_tokens")
    )
    output_tokens: int = Field(
        default=0, validation_alias=AliasChoices("output_tokens", "completion_tokens")
    )
    total_tokens: int = 0
    # Raw tool results (tool_name, success, output, error, latency_ms) for comparing LLM reply vs actual tool output
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    # FRE-343: user_id is non-optional. get_request_user always resolves one
    # (CF Access header or settings.agent_owner_email fallback or 401),
    # so user_id=None at write time is a real bug, not a fallback.
    user_id: UUID
    # FRE-523: identifies eval-derived captures so KG/consolidation content from
    # eval runs stays traceable (joins with FRE-521/522). Legacy on-disk capture
    # files predate this key — Pydantic defaults it to False on read.
    eval_mode: bool = False

    @field_validator("user_id", mode="before")
    @classmethod
    def _coerce_user_id(cls, v: Any) -> UUID:
        if type(v) is UUID:
            return v
        return UUID(str(v))


class SubAgentCapture(BaseModel):
    """Per-sub-agent audit record (FRE-505).

    Makes a decomposition turn reconstructable from telemetry alone: what each
    sub-agent was fed (input-context breakdown + memory presence), what it was
    allowed to do vs actually did, and what it returned (full output + the
    injected digest that crossed into parent synthesis). Identity-threaded with
    ``trace_id``/``session_id``/``task_id`` (ADR-0074); the parent turn joins by
    ``trace_id``. Indexed to ``SUBAGENT_CAPTURES_INDEX_PREFIX`` via
    ``write_sub_agent_capture``. Immutable once built.
    """

    model_config = ConfigDict(frozen=True)

    # Identity (ADR-0074)
    trace_id: str
    session_id: str | None = None
    task_id: str
    timestamp: datetime

    # Input context — "what was the sub fed"
    system_prompt_chars: int
    skill_index_block_chars: int
    spec_task: str
    context_message_count: int
    context_chars: int
    context_messages: list[dict[str, Any]] = Field(default_factory=list)
    memory_in_context: bool = False
    mode: str
    model_role: str
    max_tokens: int

    # Task surface — granted vs actually exercised
    tools_granted: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)

    # Output — full text, the injected digest, and the truncation ratio
    full_output: str
    full_output_chars: int
    injected_digest: str
    digest_chars: int
    truncation_ratio: float
    success: bool
    error: str | None = None
    duration_ms: float
    cost_usd: float = 0.0
    # FRE-523: EVAL provenance, uniform with TaskCapture (the sub-agent audit
    # record is written unconditionally; this flags eval-run origin).
    eval_mode: bool = False


def write_sub_agent_capture(
    capture: SubAgentCapture,
    es_handler: "ElasticsearchHandler | None" = None,
) -> None:
    """Index a sub-agent audit record to the captures family (best-effort, ES-only).

    No disk write: one file per ``trace_id`` would collide across the N sub-agents
    of a single turn, so these live only in Elasticsearch. ``schedule_es_index`` is
    non-blocking and never raises; any unexpected error here is swallowed so a
    telemetry failure can never break the sub-agent (mirrors ``capture_write_failed``).

    Args:
        capture: The sub-agent audit record to index.
        es_handler: Optional Elasticsearch handler; falls back to the default.
    """
    try:
        date_str = capture.timestamp.strftime("%Y-%m-%d")
        index_name = f"{SUBAGENT_CAPTURES_INDEX_PREFIX}-{date_str}"
        handler = es_handler or _default_es_handler
        schedule_es_index(
            index_name,
            capture.model_dump(mode="json"),
            es_handler=handler,
            doc_id=f"{capture.trace_id}:{capture.task_id}",
        )
    except Exception as exc:
        log.warning(
            "sub_agent_capture_write_failed",
            trace_id=capture.trace_id,
            task_id=capture.task_id,
            error=str(exc),
        )


def _get_captures_dir() -> pathlib.Path:
    """Get the captures directory path.

    Returns:
        Path to telemetry/captains_log/captures directory.
    """
    project_root = pathlib.Path(__file__).parent.parent.parent.parent
    captures_dir = project_root / "telemetry" / "captains_log" / "captures"
    captures_dir.mkdir(parents=True, exist_ok=True)
    return captures_dir


def write_capture(
    capture: TaskCapture,
    es_handler: "ElasticsearchHandler | None" = None,
) -> pathlib.Path:
    """Write a fast capture to disk (structured JSON, no LLM).

    Args:
        capture: Task capture to write
        es_handler: Optional Elasticsearch handler for indexing.

    Returns:
        Path to the written capture file
    """
    captures_dir = _get_captures_dir()

    # Organize by date: captures/YYYY-MM-DD/trace-id.json
    date_str = capture.timestamp.strftime("%Y-%m-%d")
    date_dir = captures_dir / date_str
    date_dir.mkdir(parents=True, exist_ok=True)

    # Filename: trace-id.json
    filename = f"{capture.trace_id}.json"
    file_path = date_dir / filename

    # Write JSON (pretty-printed with orjson for speed)
    json_content = orjson.dumps(
        capture.model_dump(),
        option=orjson.OPT_INDENT_2 | orjson.OPT_APPEND_NEWLINE,
    ).decode()
    file_path.write_text(json_content, encoding="utf-8")

    log.info(
        "capture_written",
        trace_id=capture.trace_id,
        file_path=str(file_path),
        outcome=capture.outcome,
    )

    # Optional ES indexing (Phase 2.3): non-blocking, best-effort; doc_id for idempotent backfill
    doc = capture.model_dump(mode="json")
    index_name = f"{CAPTURES_INDEX_PREFIX}-{date_str}"
    handler = es_handler or _default_es_handler
    schedule_es_index(index_name, doc, es_handler=handler, doc_id=capture.trace_id)

    return file_path


def read_captures(
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = 100,
    session_id: str | None = None,
) -> list[TaskCapture]:
    """Read captures from disk.

    Args:
        start_date: Optional start date filter
        end_date: Optional end date filter
        limit: Maximum number of captures to return
        session_id: Optional session filter, applied **inside** the scan so that
            ``limit`` bounds the matching captures rather than the captures
            examined. Filtering after the fact would let a busy window's other
            sessions consume the whole budget and silently drop the target
            session's earliest turns (FRE-947).

    Returns:
        List of task captures
    """
    captures_dir = _get_captures_dir()
    captures: list[TaskCapture] = []

    if not captures_dir.exists():
        return captures

    # Iterate through date directories
    for date_dir in sorted(captures_dir.iterdir(), reverse=True):
        if not date_dir.is_dir():
            continue

        # Parse date from directory name
        try:
            dir_date = datetime.strptime(date_dir.name, "%Y-%m-%d").replace(tzinfo=timezone.utc)
            if start_date and dir_date < start_date:
                continue
            if end_date and dir_date > end_date:
                continue
        except ValueError:
            continue

        # Read all JSON files in this date directory
        for json_file in date_dir.glob("*.json"):
            try:
                content = json_file.read_text(encoding="utf-8")
                data = orjson.loads(content)
                # FRE-343: pre-FRE-343 capture files on disk have user_id=null.
                # Inject the nil UUID so model validation succeeds; the
                # PARTICIPATED_IN MERGE downstream will MATCH no :Person and
                # silently skip the edge — which is the correct behavior for
                # historical, owner-attribution-pending data.
                if data.get("user_id") is None:
                    data["user_id"] = "00000000-0000-0000-0000-000000000000"
                capture = TaskCapture(**data)
                if session_id is not None and capture.session_id != session_id:
                    continue
                captures.append(capture)

                if len(captures) >= limit:
                    return captures
            except Exception as e:
                log.warning(
                    "capture_read_failed",
                    file_path=str(json_file),
                    error=str(e),
                    # ADR-0074 §I3: threaded now that the scan is session-scoped.
                    # An unreadable capture inside a session's own read is a hole in
                    # that session's evidence, so it must be attributable to it.
                    session_id=session_id,
                )

    return captures


def read_session_captures(
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    limit: int = 1000,
) -> list[TaskCapture]:
    """Read one session's captures, ordered oldest first (ADR-0124 D1, FRE-947).

    The idle sweep regenerates a digest **wholesale from canonical captures** —
    never by patching the previous digest — so it needs every turn of one session,
    not a recent slice across all of them. Wholesale regeneration is
    ``f(canonical captures)`` rather than ``f(previous digest, delta)``, which is
    self-correcting: a bad generation is fixed by the next sweep instead of
    becoming a permanent input to every later one.

    The date window is derived from the session's own span and widened by a day at
    each end, because captures are filed under a UTC date directory and a session
    can straddle midnight.

    Args:
        session_id: Session whose captures to read.
        started_at: The session's first-turn timestamp.
        ended_at: The session's last-turn timestamp.
        limit: Safety bound on captures **matching this session**, not on captures
            examined — the filter is applied inside the scan. Sessions max out
            around 17 turns, so this exists to stop a pathological read, not to
            shape results. Bounding the scan instead would silently drop the
            session's earliest turns whenever the date window happened to hold
            more than ``limit`` captures across all sessions, and the producer
            would then assert an untruncated transcript over an incomplete one —
            precisely the silent truncation ADR-0124 forbids.

    Returns:
        The session's captures sorted by timestamp ascending. Empty if none are
        on disk — which is a real condition (retention may have removed them),
        not an error.
    """
    window = timedelta(days=1)
    matching = read_captures(
        start_date=started_at - window,
        end_date=ended_at + window,
        limit=limit,
        session_id=session_id,
    )
    return sorted(matching, key=lambda c: c.timestamp)
