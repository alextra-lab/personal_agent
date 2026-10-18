"""Prompt debug capture: the full request of every model call, one JSON file each (FRE-1614).

ES keeps only the size of a model call and a 100-character preview per message, so
nobody can read what a model received. With ``prompt_debug_capture_enabled`` on, the
LiteLLM client hands each completed call to :func:`capture_model_call`, which writes
the messages, the tools, the allow-listed request parameters and the usage to
``telemetry/prompt_debug/<session>/<trace>/<seq>-<role>.json``.

The files hold the owner's private content, so they stay on the gateway volume
(never ES), are readable by their owner only, and are deleted after 7 days.
Credentials cannot reach a file: only the keys in :data:`ALLOWED_PARAMS` are
written, so ``api_key``, ``api_base``, ``client`` and ``extra_headers`` are dropped
by construction rather than by a deny-list that a new key could slip past.

Read the files with ``python -m personal_agent.telemetry.prompt_debug_viewer``.
"""

from __future__ import annotations

import asyncio
import json
import os
import pathlib
import re
import time
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Literal

import httpx

from personal_agent.config import settings
from personal_agent.telemetry.logger import get_logger
from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)

#: The request keys written to a capture file. Every other key is dropped.
ALLOWED_PARAMS: frozenset[str] = frozenset(
    {
        "model",
        "max_tokens",
        "temperature",
        "top_p",
        "top_k",
        "min_p",
        "presence_penalty",
        "frequency_penalty",
        "repetition_penalty",
        "seed",
        "stop",
        "tool_choice",
        "response_format",
        "parallel_tool_calls",
        "reasoning_effort",
        "thinking",
        "stream",
        "stream_options",
        "num_retries",
        "timeout",
        "extra_body",
        "allowed_openai_params",
    }
)

RETENTION_DAYS = 7
_PRUNE_INTERVAL_S = 3600.0
_NO_SESSION = "no-session"
_UNSAFE_PATH_CHARS = re.compile(r"[^A-Za-z0-9_-]")
_SEQ_PREFIX = re.compile(r"^(?:\.seq-)?(\d+)")
_MAX_SEQ_ATTEMPTS = 1000

_last_prune_monotonic: float | None = None

Placement = Literal["cloud", "local"]


def capture_enabled() -> bool:
    """Return whether prompt debug capture is on.

    Reads the settings singleton, not ``get_settings()``: client tests patch
    ``get_settings`` with a ``MagicMock``, whose every attribute is truthy.

    Returns:
        The value of ``settings.prompt_debug_capture_enabled``.
    """
    return settings.prompt_debug_capture_enabled


def prompt_debug_root() -> pathlib.Path:
    """Directory that holds the capture files (``/app/telemetry/prompt_debug`` in the gateway).

    Returns:
        The capture root. It is not created here: a read must not write.
    """
    return (
        pathlib.Path(__file__).resolve().parent.parent.parent.parent
        / "telemetry"
        / ("prompt_debug")
    )


def build_record(
    *,
    trace_id: str,
    session_id: str | None,
    role: str,
    model: str,
    provider: str,
    placement: Placement,
    started_at: datetime,
    duration_ms: int,
    request: Mapping[str, object],
    usage: Mapping[str, object],
) -> dict[str, object]:
    """Build the capture record for one model call.

    Args:
        trace_id: Trace the call belongs to.
        session_id: Session the call belongs to, or ``None`` for a system trace.
        role: Model role value (``primary``, ``sub_agent``, ...).
        model: Model string the request named.
        provider: Catalog provider key.
        placement: ``cloud`` or ``local`` — which client path sent the call.
        started_at: Wall-clock time the request left the client.
        duration_ms: Time from dispatch to the parsed response.
        request: The keyword arguments handed to ``litellm.acompletion``.
        usage: Token usage the response reported.

    Returns:
        A JSON-ready record. ``messages`` and ``tools`` are copied in full; of the
        other request keys, only those in :data:`ALLOWED_PARAMS` are kept.
    """
    return {
        "trace_id": trace_id,
        "session_id": session_id,
        "role": role,
        "model": model,
        "provider": provider,
        "placement": placement,
        "started_at": started_at.isoformat(),
        "duration_ms": duration_ms,
        "usage": dict(usage),
        "params": {key: value for key, value in request.items() if key in ALLOWED_PARAMS},
        "messages": request.get("messages"),
        "tools": request.get("tools"),
    }


def safe_path_component(value: object, fallback: str) -> str:
    """Reduce a value to one path component of ``[A-Za-z0-9_-]`` (no traversal).

    Args:
        value: Session id, trace id or role.
        fallback: Component to use when ``value`` is empty.

    Returns:
        The cleaned component.
    """
    cleaned = _UNSAFE_PATH_CHARS.sub("_", str(value)) if value else ""
    return cleaned or fallback


def _next_seq(directory: pathlib.Path) -> int:
    """Return one more than the highest sequence number already used in ``directory``.

    Both the capture files and the ``.seq-NNN`` reservation markers count.
    """
    highest = 0
    for entry in directory.iterdir():
        match = _SEQ_PREFIX.match(entry.name)
        if match:
            highest = max(highest, int(match.group(1)))
    return highest + 1


def _json_fallback(value: object) -> object:
    """Serialise the one non-JSON request value the client sends, and name any other.

    An unknown object is written as its type name only, never its ``repr``, which
    could carry state nobody meant to write.
    """
    if isinstance(value, httpx.Timeout):
        return value.as_dict()
    return f"<{type(value).__name__}>"


def _reserve_seq(directory: pathlib.Path) -> int:
    """Reserve the next sequence number of a trace with an ``O_EXCL`` marker file.

    The marker name has no role in it, so two concurrent writers with different
    roles cannot both take the same number. Markers stay until the retention prune:
    deleting one would let a later writer take its number again.
    """
    seq = _next_seq(directory)
    for _ in range(_MAX_SEQ_ATTEMPTS):
        try:
            os.close(os.open(directory / f".seq-{seq:03d}", os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            seq += 1
            continue
        return seq
    raise OSError(f"no free sequence number under {directory}")


def _publish(directory: pathlib.Path, role: str, record: Mapping[str, object]) -> pathlib.Path:
    """Write the record to a hidden temporary file, then link it to its final name.

    ``os.link`` fails if the name exists, so publishing is atomic: a reader never
    sees a partial file, and no file is overwritten.
    """
    seq = _reserve_seq(directory)
    payload = json.dumps(
        {"seq": seq, **record}, default=_json_fallback, ensure_ascii=False, indent=1
    )
    tmp = directory / f".tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
        final = directory / f"{seq:03d}-{role}.json"
        os.link(tmp, final)
        return final
    finally:
        tmp.unlink(missing_ok=True)


def write_record(record: Mapping[str, object], *, root: pathlib.Path) -> pathlib.Path:
    """Write one record to ``<root>/<session>/<trace>/<seq>-<role>.json``.

    The file has mode ``0o600``. The root, session and trace directories are each
    created with mode ``0o700`` (``mkdir(parents=True)`` would give the parents the
    default mode). Publishing is atomic, and a sequence number is never reused.

    Args:
        record: A record from :func:`build_record`.
        root: Capture root directory.

    Returns:
        The path written.

    Raises:
        OSError: When the directory or the file cannot be created.
    """
    directory = (
        root
        / safe_path_component(record.get("session_id"), _NO_SESSION)
        / safe_path_component(record.get("trace_id"), "no-trace")
    )
    role = safe_path_component(record.get("role"), "unknown")
    for _ in range(2):
        try:
            root.mkdir(mode=0o700, parents=True, exist_ok=True)
            directory.parent.mkdir(mode=0o700, exist_ok=True)
            directory.mkdir(mode=0o700, exist_ok=True)
            return _publish(directory, role, record)
        except FileNotFoundError:
            continue  # The prune removed the directory after mkdir: create it again.
    raise OSError(f"could not create a capture file under {directory}")


def prune_old_captures(
    root: pathlib.Path, *, now: float, max_age_days: int = RETENTION_DAYS
) -> int:
    """Delete capture files older than ``max_age_days``, then their emptied directories.

    Every directory's mtime is read before any file is deleted, because deleting a
    file updates its directory's mtime. A directory is removed only when it is empty
    and its own mtime was already older than the limit, so a trace directory a writer
    created a moment ago is never removed under it.

    Args:
        root: Capture root directory.
        now: Current time, as ``time.time()``.
        max_age_days: Age limit in days.

    Returns:
        The number of files deleted.
    """
    if not root.is_dir():
        return 0
    cutoff = now - max_age_days * 86400
    old_dirs: list[pathlib.Path] = []
    files: list[pathlib.Path] = []
    for dirpath, _dirnames, filenames in os.walk(root):
        directory = pathlib.Path(dirpath)
        if directory != root and directory.stat().st_mtime < cutoff:
            old_dirs.append(directory)
        files.extend(directory / name for name in filenames)
    deleted = 0
    for path in files:
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except FileNotFoundError:
            continue
    # Deepest first, so a session directory empties before it is tried.
    for directory in sorted(old_dirs, key=lambda d: len(d.parts), reverse=True):
        try:
            directory.rmdir()
        except OSError:
            continue  # Not empty, or already gone.
    return deleted


def _write_and_maybe_prune(record: Mapping[str, object], root: pathlib.Path) -> pathlib.Path:
    """Write the record, and prune at most once per hour per process."""
    global _last_prune_monotonic
    path = write_record(record, root=root)
    now_monotonic = time.monotonic()
    if _last_prune_monotonic is None or now_monotonic - _last_prune_monotonic >= _PRUNE_INTERVAL_S:
        _last_prune_monotonic = now_monotonic
        prune_old_captures(root, now=time.time())
    return path


async def capture_model_call(
    *,
    trace_ctx: TraceContext,
    role: str,
    model: str,
    provider: str,
    placement: Placement,
    started_at: datetime,
    duration_ms: int,
    request: Mapping[str, object],
    usage: Mapping[str, object],
    root: pathlib.Path | None = None,
) -> None:
    """Write the capture file for one completed model call.

    The caller checks :func:`capture_enabled` first, so a disabled capture adds no
    work. The file write runs in a worker thread. A failure is logged and swallowed:
    a debug file must never fail the model call.

    Args:
        trace_ctx: Trace context of the call.
        role: Model role value.
        model: Model string the request named.
        provider: Catalog provider key.
        placement: ``cloud`` or ``local``.
        started_at: Wall-clock time the request left the client.
        duration_ms: Time from dispatch to the parsed response.
        request: The keyword arguments handed to ``litellm.acompletion``.
        usage: Token usage the response reported.
        root: Capture root; defaults to :func:`prompt_debug_root`.
    """
    trace_id = str(trace_ctx.trace_id)
    try:
        record = build_record(
            trace_id=trace_id,
            session_id=trace_ctx.session_id,
            role=role,
            model=model,
            provider=provider,
            placement=placement,
            started_at=started_at,
            duration_ms=duration_ms,
            request=request,
            usage=usage,
        )
        await asyncio.to_thread(
            _write_and_maybe_prune, record, root if root is not None else prompt_debug_root()
        )
    except Exception as exc:  # noqa: BLE001 — a debug write never fails the call
        log.warning(
            "prompt_debug_capture_failed",
            trace_id=trace_id,
            session_id=trace_ctx.session_id,
            role=role,
            error=str(exc),
        )


async def prune_captures_at_startup() -> None:
    """Apply the 7-day retention once at service start, whether capture is on or off.

    Capture-time pruning alone would keep old files forever once capture is off or
    the gateway is idle. A failure is logged and swallowed: startup must not fail
    over a debug directory.
    """
    root = prompt_debug_root()
    try:
        deleted = await asyncio.to_thread(prune_old_captures, root, now=time.time())
    except Exception as exc:  # noqa: BLE001 — retention never blocks startup
        log.warning("prompt_debug_prune_failed", trace_id=None, root=str(root), error=str(exc))
        return
    if deleted:
        log.info("prompt_debug_pruned", trace_id=None, root=str(root), deleted=deleted)
