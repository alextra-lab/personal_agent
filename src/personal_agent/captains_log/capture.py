"""Fast capture system for Captain's Log (Phase 2.2).

This module provides structured capture of task execution data without LLM processing.
Captures are written immediately during request processing, then processed later by
the second brain for deep reflection.
"""

import asyncio
import pathlib
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import TYPE_CHECKING, Any
from uuid import UUID

import orjson
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, ValidationError, field_validator

from personal_agent.captains_log.es_indexer import schedule_es_index
from personal_agent.captains_log.turn_evidence import (
    AssembledContextRecord,
    EvidenceState,
    RecallAdmissionRecord,
)
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
    # ADR-0125 D3 (FRE-1004) — the turn evidence contract.
    # item 5: which memory items the turn actually relied on, by identity and score,
    # with the ones trimming or rendering dropped named rather than vanished.
    recall_admission: RecallAdmissionRecord | None = None
    # item 6: what the assembled context contained, at item-identity granularity.
    assembled_context: AssembledContextRecord | None = None
    # The state of all eight D3 records: an implicitly missing field is
    # indistinguishable from a capture gap, which is the failure the contract
    # exists to prevent. All three default so legacy on-disk captures still read.
    evidence_presence: dict[str, EvidenceState] = Field(default_factory=dict)

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
    return _scan_captures(
        start_date=start_date, end_date=end_date, limit=limit, session_id=session_id
    )[0]


def _scan_captures(
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    limit: int = 100,
    session_id: str | None = None,
) -> tuple[list[TaskCapture], int, list[str]]:
    """Scan the on-disk captures, reporting what could not be read.

    Splitting the unreadable count out of :func:`read_captures` is load-bearing for the
    session-digest producer (FRE-992): a file that fails to parse used to be logged and
    dropped, so a caller received a *shortened* transcript indistinguishable from a
    genuinely shorter session. The digest producer is explicitly instructed to read a
    gap in its input as evidence of absence, so it must be told when its input has one.

    Args:
        start_date: Optional start date filter.
        end_date: Optional end date filter.
        limit: Maximum number of matching captures to return.
        session_id: Optional session filter, applied inside the scan.

    Returns:
        The captures read; the number of files attributable to ``session_id`` that
        could not be read; and the filename stems (capture ``trace_id``s) of files
        whose own session could not be determined because they are not valid JSON.
    """
    captures_dir = _get_captures_dir()
    captures: list[TaskCapture] = []
    unreadable = 0
    truncated_stems: list[str] = []

    if not captures_dir.exists():
        return captures, unreadable, truncated_stems

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
            # Held outside the try so the failure path can attribute the file without
            # re-reading it. A corrupt file stays corrupt, so a second read would
            # repeat for every session, on every sweep, forever.
            parsed: dict[str, Any] | None = None
            try:
                content = json_file.read_text(encoding="utf-8")
                data = orjson.loads(content)
                if isinstance(data, dict):
                    parsed = data
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
                    return captures, unreadable, truncated_stems
            except Exception as e:
                # Attribute the failure before counting it. A date directory holds
                # every session's captures for that day, and the filename is a
                # trace_id, so an unattributed count would let one corrupt file mark
                # the read of every *other* session in the window non-authoritative —
                # condemning healthy sessions for a hole that is not theirs.
                owner = parsed.get("session_id") if parsed else None
                owner = owner if isinstance(owner, str) else None
                if session_id is None or owner == session_id:
                    unreadable += 1
                elif owner is None:
                    # Not even JSON, so its session is unknowable from content. The
                    # filename is the capture's trace_id, which lets the caller
                    # discharge it if the durable store already holds that capture.
                    truncated_stems.append(json_file.stem)
                log.warning(
                    "capture_read_failed",
                    file_path=str(json_file),
                    # Never str(e): a pydantic ValidationError embeds a repr of the
                    # offending value, which for a capture is the user's own message.
                    # Logs and captures are separate indices with separate access.
                    error=_safe_error_summary(e),
                    # ADR-0074 §I3: threaded now that the scan is session-scoped.
                    session_id=session_id,
                    file_session_id=owner,
                )

    return captures, unreadable, truncated_stems


def _safe_error_summary(exc: Exception) -> str:
    """Render an exception without echoing the value that caused it.

    ``str(ValidationError)`` embeds a truncated repr of the offending input. For a
    capture that input is conversation text, so the default rendering would ship user
    content into the log index — which the project forbids and which crosses an access
    boundary, since logs and captures are separate indices.

    Args:
        exc: The exception to describe.

    Returns:
        The field paths and error types, or just the exception class name.
    """
    if isinstance(exc, ValidationError):
        return "; ".join(
            f"{'.'.join(str(p) for p in err['loc'])}: {err['type']}"
            for err in exc.errors(include_input=False, include_url=False)
        )
    return f"{type(exc).__name__}: {exc}"
    return None


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
    captures, _, _ = _read_session_captures_from_disk(
        session_id, started_at=started_at, ended_at=ended_at, limit=limit
    )
    return captures


#: Widening applied to a session's own span when scanning either store. Captures are
#: filed under a UTC date directory on disk, and a session can straddle midnight.
_SESSION_WINDOW = timedelta(days=1)


class CaptureSource(StrEnum):
    """Which store(s) a session's captures actually came from."""

    ELASTICSEARCH = "elasticsearch"
    DISK = "disk"
    BOTH = "both"
    NONE = "none"


@dataclass(frozen=True)
class SessionCaptureRead:
    """One session's captures, plus whether the read can be trusted as whole.

    ``complete`` is a claim about the *read*, not about the session: it says every store
    this reader consulted answered, and everything they returned parsed. It deliberately
    says nothing about whether the session had more turns than the stores hold — that is
    decided against the graph by the caller, which owns the only independent count.

    Attributes:
        captures: The union of both stores, deduplicated by ``trace_id`` and ordered
            oldest first.
        source: Which store(s) contributed.
        unreadable: Documents and files attributable to **this session** that could not
            be parsed. Never silently dropped: the digest producer reads a gap in its
            input as evidence of absence, so a shortened transcript must be reported as
            shortened.
        unattributable: Corrupt local files in the window whose own session could not
            be determined and which the durable store does not already account for.
            Reported rather than charged: one such file cannot be pinned on any
            session, and treating it as fatal would condemn every session in the
            window. A non-zero value here is an anomaly worth an operator's attention.
        stores_unavailable: The durable store did not answer — it was unreachable, or
            no client existed to ask. Kept separate from ``unreadable`` because the two
            have opposite retry semantics: an outage is transient and the same read may
            succeed in a minute, whereas a corrupt document fails the same way forever.
            Collapsing them would let a brief Elasticsearch restart exhaust a session's
            retry budget.
        complete: True only when **both** stores answered and everything attributable
            parsed. A disk-only read is never complete — that the local directory is
            not durable is the whole premise of FRE-992, so "disk holds two captures"
            can never establish "this session had two turns".
    """

    captures: tuple[TaskCapture, ...]
    source: CaptureSource
    unreadable: int
    unattributable: int
    stores_unavailable: bool
    complete: bool


def _read_session_captures_from_disk(
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    limit: int,
) -> tuple[list[TaskCapture], int, list[str]]:
    """Read one session's captures from the local capture directory.

    Returns:
        The captures, the count of unreadable files attributable to this session, and
        the trace_id stems of unreadable files whose session could not be determined.
    """
    captures, unreadable, orphan_stems = _scan_captures(
        start_date=started_at - _SESSION_WINDOW,
        end_date=ended_at + _SESSION_WINDOW,
        limit=limit,
        session_id=session_id,
    )
    return sorted(captures, key=lambda c: c.timestamp), unreadable, orphan_stems


async def _read_session_captures_from_es(
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    es_client: Any,
    limit: int,
    trace_id: str | None,
) -> tuple[list[TaskCapture], int, bool]:
    """Read one session's captures from the durable captures index.

    Returns:
        The captures read, the number of returned documents that failed validation, and
        whether the response hit ``limit`` (leaving exhaustion unproven).

    Raises:
        Exception: Whatever the client raises. A store that could not be consulted is
            the caller's business — swallowing it here would let an outage masquerade
            as an empty session, which is the FRE-992 defect in a new place.
    """
    # The sub-agent audit index is a sibling under the same wildcard (see
    # SUBAGENT_CAPTURES_INDEX_PREFIX above) and its documents carry `session_id` while
    # holding a SubAgentCapture shape. Excluding it here is what stops N per-turn audit
    # records failing validation and making every read non-authoritative.
    index = f"{CAPTURES_INDEX_PREFIX}-*,-{SUBAGENT_CAPTURES_INDEX_PREFIX}-*"
    response = await es_client.search(
        index=index,
        query={
            "bool": {
                "filter": [
                    {"term": {"session_id": session_id}},
                    {
                        "range": {
                            "timestamp": {
                                "gte": (started_at - _SESSION_WINDOW).isoformat(),
                                "lte": (ended_at + _SESSION_WINDOW).isoformat(),
                            }
                        }
                    },
                ]
            }
        },
        # trace_id breaks ties so two captures sharing a timestamp cannot reorder
        # between reads — a wholesale regeneration must be a function of the captures,
        # not of the order Elasticsearch happened to return them in.
        sort=[{"timestamp": "asc"}, {"trace_id": "asc"}],
        size=limit,
        ignore_unavailable=True,
        allow_no_indices=True,
    )

    hits = response.get("hits", {}).get("hits", []) or []
    captures: list[TaskCapture] = []
    unreadable = 0
    for hit in hits:
        source = hit.get("_source")
        if not isinstance(source, dict):
            unreadable += 1
            continue
        if source.get("session_id") != session_id:
            continue
        try:
            captures.append(TaskCapture(**source))
        except Exception as e:  # noqa: BLE001 — a bad document is counted, never fatal
            unreadable += 1
            log.warning(
                "capture_es_doc_unreadable",
                session_id=session_id,
                trace_id=trace_id,
                capture_trace_id=source.get("trace_id"),
                error=str(e),
            )
    return captures, unreadable, len(hits) >= limit


async def load_session_captures(
    session_id: str,
    *,
    started_at: datetime,
    ended_at: datetime,
    es_client: Any | None,
    limit: int = 1000,
    trace_id: str | None = None,
) -> SessionCaptureRead:
    """Read one session's captures from every available store (FRE-992, ADR-0124 AC-8).

    **Both stores are read and unioned, because they are not replicas.**
    :func:`write_capture` writes the local file synchronously and then *schedules* the
    Elasticsearch index through a fire-and-forget task that is silently dropped when no
    event loop is running. Either store can therefore hold a turn the other lacks, and
    taking whichever answers first is what let 46 sessions be read as empty while their
    captures sat in Elasticsearch — the defect this function exists to close.

    Where a capture is present in both, the Elasticsearch copy wins. The only difference
    is that ``tool_results`` output and arguments have been JSON-serialised on the way
    in (``normalize_capture_doc_for_es``); the conversation-only producer never reads
    those fields, and the user and assistant text are byte-identical in both stores.

    Args:
        session_id: Session whose captures to read.
        started_at: The session's first-turn timestamp.
        ended_at: The session's last-turn timestamp.
        es_client: An open ``AsyncElasticsearch``, or None to read the local store only.
            A client is never constructed here — a background read must not open its own
            connection to whatever URL happens to be configured (FRE-375).
        limit: Safety bound on captures **matching this session**, not on captures
            examined. Sessions max out around 17 turns, so this exists to stop a
            pathological read; reaching it makes the read non-authoritative rather than
            silently truncating, which is the one thing ADR-0124 forbids.
        trace_id: Trace identifier of the enclosing sweep (ADR-0074 §I3).

    Returns:
        The union of both stores, with the provenance and trustworthiness of the read.
    """
    # Directory walk + file reads: off the event loop, since this runs inside the
    # scheduler's sweep alongside live request handling.
    disk_captures, unreadable, orphan_stems = await asyncio.to_thread(
        _read_session_captures_from_disk,
        session_id,
        started_at=started_at,
        ended_at=ended_at,
        limit=limit,
    )

    es_captures: list[TaskCapture] = []
    es_consulted = False
    truncated = False
    if es_client is not None:
        try:
            es_captures, es_unreadable, truncated = await _read_session_captures_from_es(
                session_id,
                started_at=started_at,
                ended_at=ended_at,
                es_client=es_client,
                limit=limit,
                trace_id=trace_id,
            )
            es_consulted = True
            unreadable += es_unreadable
        except Exception as e:  # noqa: BLE001 — an outage is reported, never fatal
            log.warning(
                "capture_es_read_failed",
                session_id=session_id,
                trace_id=trace_id,
                error=str(e),
                error_type=type(e).__name__,
            )

    by_trace: dict[str, TaskCapture] = {c.trace_id: c for c in disk_captures}
    by_trace.update({c.trace_id: c for c in es_captures})
    merged = sorted(by_trace.values(), key=lambda c: (c.timestamp, c.trace_id))

    if es_captures and disk_captures:
        source = CaptureSource.BOTH
    elif es_captures:
        source = CaptureSource.ELASTICSEARCH
    elif disk_captures:
        source = CaptureSource.DISK
    else:
        source = CaptureSource.NONE

    # A corrupt file whose own session is unknowable is discharged when the durable
    # store already holds that capture — the filename is its trace_id, so a local file
    # truncated mid-write is provably not a hole when its Elasticsearch twin is here.
    # What remains is a genuine gap belonging to *some* session in the window; it is
    # surfaced rather than charged, because condemning every session in the window for
    # one file would retire healthy sessions wholesale.
    known = {c.trace_id for c in merged}
    unattributable = len([stem for stem in orphan_stems if stem not in known])

    # "The durable store did not answer" — deliberately NOT `es_client is not None and
    # not es_consulted`. The client is resolved once at startup and is None for the
    # whole process when Elasticsearch was down at boot, so keying off its presence
    # would classify a total outage as a *deterministic* shortfall and spend every
    # session's retry budget on it. Not having asked is not the same as having asked
    # and been told there is nothing.
    stores_unavailable = not es_consulted
    complete = unreadable == 0 and not truncated and es_consulted

    log.info(
        "session_captures_loaded",
        session_id=session_id,
        trace_id=trace_id,
        capture_count=len(merged),
        source=source.value,
        unreadable=unreadable,
        unattributable=unattributable,
        stores_unavailable=stores_unavailable,
        complete=complete,
        es_count=len(es_captures),
        disk_count=len(disk_captures),
    )
    return SessionCaptureRead(
        captures=tuple(merged),
        source=source,
        unreadable=unreadable,
        unattributable=unattributable,
        stores_unavailable=stores_unavailable,
        complete=complete,
    )
