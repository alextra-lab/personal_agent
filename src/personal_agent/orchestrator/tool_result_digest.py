"""Intra-turn tool-result digest infrastructure (ADR-0085, FRE-475 PR-A).

Pure, deterministic machinery for compressing large tool results to a
**byte-stable** digest before their verbatim bytes enter the conversation
transcript. PR-A ships this surface *unwired*; the executor insertion hook,
``expand_tool_result`` tool, and dependency-pinning land in PR-B.

Public surface (all pure / side-effect-free except :func:`persist_tool_result`):

- :func:`build_tool_result_key` — canonical R2 key, sibling of
  :func:`personal_agent.storage.artifact_store.build_r2_key` (D1).
- :func:`compute_content_hash` — full SHA-256 hex of the raw bytes (D3; *not*
  the 16-hex loop-gate ``stable_hash``).
- :func:`digest_tool_content` — format-aware deterministic extractor dispatch (D2).
- :func:`build_digest_message` — byte-stable ``role="tool"`` replacement (D3/D6).
- :func:`should_digest` / :func:`digest_saves_enough` — content-intrinsic gates (D7).
- :func:`persist_tool_result` — await R2 ``put`` to durable confirmation (D1).

Byte-stability (D3) is the load-bearing invariant: every digest, once written,
must be byte-identical on every resend within the turn and on every cross-turn
replay. Therefore no field may carry volatile bytes — no timestamps, retry
counts, presigned/expiring URLs, or non-deterministic ordering — and the size
shown to the model is the exact byte count, never an estimated token count.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from collections.abc import Sequence
from typing import TYPE_CHECKING, Any
from uuid import UUID

from personal_agent.config import settings
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.orchestrator.context_compressor import _content_is_error_payload
from personal_agent.storage.artifact_store import (
    ArtifactKeyError,
    ArtifactStoreError,
    R2ArtifactStore,
)
from personal_agent.telemetry import get_logger

if TYPE_CHECKING:
    from personal_agent.events.bus import EventBus
    from personal_agent.orchestrator.types import ExecutionContext
    from personal_agent.telemetry.trace import TraceContext

log = get_logger(__name__)

# Single key prefix for the tool-result tier (D1). Sibling of build_r2_key's
# ``{type}/...`` layout; kept distinct so the two namespaces never collide.
_KEY_PREFIX = "tool-results"

# Strict per-segment grammar (Codex Q4). Must start alphanumeric — so a bare
# ``..`` traversal segment is rejected — then allow alnum / ``.`` / ``_`` / ``-``
# only. Rejects slashes, spaces, ``?``/``#``/``=`` and other URL-unsafe chars,
# control characters, and empty segments, before any R2 call.
_KEY_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")

# Structured-middle frame signals (D2 / Codex Q3). A line matching any of these
# carries a load-bearing fact (diff hunk, stack frame, failure) that head/tail
# masking would discard — so it is preserved with nearby context.
_FRAME_RE = re.compile(
    r'(Traceback|File ".*", line \d+|^@@ |^\+\+\+ |^--- |Error|Exception|FAILED|Failed|assert)'
)


# ---------------------------------------------------------------------------
# D1 — canonical key + D3 — content hash
# ---------------------------------------------------------------------------


def build_tool_result_key(session_id: UUID, trace_id: str, tool_call_id: str) -> str:
    """Produce the canonical R2 key for a digested tool result (ADR-0085 D1).

    Layout: ``tool-results/{session_id}/{trace_id}/{tool_call_id}`` — deterministic
    from three stable IDs, joinable per ADR-0074, minted once and embedded in the
    digest verbatim (never regenerated on replay).

    Args:
        session_id: Producing session UUID (already key-safe).
        trace_id: Request trace identifier.
        tool_call_id: The tool call's identifier.

    Returns:
        The opaque R2 key string.

    Raises:
        ArtifactKeyError: When ``trace_id`` or ``tool_call_id`` escapes
            :data:`_KEY_SEGMENT_RE` (traversal, slash, control, URL-unsafe, empty).
    """
    for segment, name in ((trace_id, "trace_id"), (tool_call_id, "tool_call_id")):
        if not _KEY_SEGMENT_RE.match(segment):
            raise ArtifactKeyError(f"{name} {segment!r} must match {_KEY_SEGMENT_RE.pattern!r}")
    return f"{_KEY_PREFIX}/{session_id}/{trace_id}/{tool_call_id}"


def compute_content_hash(content: str) -> str:
    """Return the full SHA-256 hex digest of *content*'s UTF-8 bytes (D3).

    Used for exact-replay validation in PR-B's ``expand_tool_result``. Deliberately
    *not* ``loop_gate.stable_hash`` (16 hex, ``default=str``) — that is for loop
    dedup, not content identity.
    """
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# D2 — deterministic, format-aware extractors
# ---------------------------------------------------------------------------


def _try_parse_json(content: str) -> Any:
    """Parse *content* as JSON, returning ``None`` on failure."""
    try:
        return json.loads(content)
    except (TypeError, ValueError):
        return None


def _looks_structured(text: str) -> bool:
    """True when *text* contains at least one structured-middle frame line."""
    return any(_FRAME_RE.search(line) for line in text.split("\n"))


def _headtail(text: str, head: int, tail: int) -> str:
    """Keep the first *head* and last *tail* lines with a deterministic elision marker.

    The elided-line count is derived from the input, so the output is a pure
    function of the input (byte-stable on regeneration).
    """
    lines = text.split("\n")
    if len(lines) <= head + tail:
        return text
    elided = len(lines) - head - tail
    kept = lines[:head] + [f"... [{elided} lines elided] ..."] + lines[len(lines) - tail :]
    return "\n".join(kept)


def _structured_digest(text: str, *, max_lines: int) -> str:
    """Keep frame lines (+/- 1 line of context) in order, eliding the gaps (D2).

    Falls back to :func:`_headtail` when no frame lines are found.
    """
    lines = text.split("\n")
    keep: set[int] = set()
    for i, line in enumerate(lines):
        if _FRAME_RE.search(line):
            for j in range(max(0, i - 1), min(len(lines), i + 2)):
                keep.add(j)
    if not keep:
        return _headtail(text, max_lines // 2, max_lines - max_lines // 2)

    out: list[str] = []
    prev = -1
    for idx in sorted(keep):
        if prev != -1 and idx > prev + 1:
            out.append(f"... [{idx - prev - 1} lines elided] ...")
        out.append(lines[idx])
        prev = idx
    return "\n".join(out[:max_lines])


def _stream_digest(text: str) -> str:
    """Digest one text stream: structured-middle frames if present, else head/tail.

    Codex Q3: structured-middle detection runs *before* head/tail so a traceback
    or diff buried in the middle of ``stdout`` is not masked into oblivion.
    """
    if not text:
        return ""
    head = settings.tool_result_digest_head_lines
    tail = settings.tool_result_digest_tail_lines
    if _looks_structured(text):
        return _structured_digest(text, max_lines=head + tail)
    return _headtail(text, head, tail)


def _digest_bash(parsed: dict[str, Any]) -> dict[str, Any]:
    """Digest a ``bash`` tool result, retaining diagnostics + head/tail of streams."""
    return {
        "format": "bash",
        "command": parsed.get("command"),
        "exit_code": parsed.get("exit_code"),
        "success": parsed.get("success"),
        "note": parsed.get("note"),
        "truncated_path": parsed.get("truncated_path"),
        "stdout": _stream_digest(str(parsed.get("stdout") or "")),
        "stderr": _stream_digest(str(parsed.get("stderr") or "")),
    }


def _digest_read(parsed: dict[str, Any]) -> dict[str, Any]:
    """Digest a ``read`` tool result: keep the outline + the ranged region (D2)."""
    return {
        "format": "read",
        "path": parsed.get("path"),
        "offset": parsed.get("offset"),
        "limit": parsed.get("limit"),
        "total_lines": parsed.get("total_lines"),
        "truncated": parsed.get("truncated"),
        "marker": parsed.get("marker"),
        "content": _stream_digest(str(parsed.get("content") or "")),
    }


def _digest_json(parsed: dict[str, Any]) -> dict[str, Any]:
    """Digest a generic JSON tool result: key paths, collection counts, error fields."""
    body: dict[str, Any] = {
        "format": "json",
        "keys": sorted(str(k) for k in parsed.keys()),
        "counts": {str(k): len(v) for k, v in parsed.items() if isinstance(v, (list, dict, str))},
    }
    errors = {str(k): v for k, v in parsed.items() if "error" in str(k).lower()}
    if errors:
        body["errors"] = errors
    return body


def _digest_text(content: str) -> dict[str, Any]:
    """Digest unrecognized text (markup/XML/plain) via head/tail fallback (D2)."""
    return {"format": "text", "text": _stream_digest(content)}


def digest_tool_content(tool_name: str, content: str) -> dict[str, Any]:
    """Produce a deterministic, lossy digest body for a tool result (ADR-0085 D2).

    Dispatch order (Codex Q3): parse JSON first, route ``bash``/``read`` to their
    format-aware extractors (which sniff structured-middle frames inside the
    streams), then any other dict to the generic-JSON extractor, and finally fall
    back to head/tail text for unparseable content.

    Args:
        tool_name: The originating tool's name.
        content: The verbatim tool-result content string.

    Returns:
        A JSON-serializable body dict carrying a ``"format"`` discriminator. Never
        an LLM call — pure and deterministic.
    """
    parsed = _try_parse_json(content)
    if isinstance(parsed, dict):
        if tool_name == "bash" and ("stdout" in parsed or "stderr" in parsed):
            return _digest_bash(parsed)
        if tool_name == "read" and "content" in parsed:
            return _digest_read(parsed)
        return _digest_json(parsed)
    return _digest_text(content)


# ---------------------------------------------------------------------------
# D3 — byte-stable digest message
# ---------------------------------------------------------------------------


def build_digest_message(
    *,
    tool_call_id: str,
    tool_name: str,
    r2_key: str,
    content_hash: str,
    full_byte_len: int,
    body: dict[str, Any],
) -> dict[str, Any]:
    """Assemble the byte-stable ``role="tool"`` digest replacement (ADR-0085 D3/D6).

    The ``content`` payload is canonical JSON (sorted keys, compact separators)
    with no volatile bytes. The size advertised to the model is the exact byte
    count (``full_byte_len``), never an estimated token count, so regenerating the
    digest from the same raw content yields byte-identical output. ``tool_call_id``,
    ``role``, and ``name`` are preserved so the message stays a well-formed
    ``tool_result`` adjacent to its ``tool_use`` (D6 adjacency, asserted in PR-B).

    Args:
        tool_call_id: Identifier tying the result to its assistant ``tool_use``.
        tool_name: The originating tool's name.
        r2_key: Canonical key (from :func:`build_tool_result_key`) where the full
            bytes are durably stored; minted once.
        content_hash: Full SHA-256 hex of the verbatim bytes (exact-replay guard).
        full_byte_len: Exact ``len(content.encode("utf-8"))`` — a stable byte count.
        body: The deterministic digest body from :func:`digest_tool_content`.

    Returns:
        An OpenAI-format ``role="tool"`` message dict.
    """
    payload: dict[str, Any] = {
        "_digest": True,
        "body": body,
        "bytes": full_byte_len,
        "content_hash": content_hash,
        "hint": (
            f"Full {tool_name} output hidden ({full_byte_len} bytes). "
            f'Call expand_tool_result("{r2_key}", "{content_hash}") to retrieve '
            "verbatim before editing against omitted lines."
        ),
        "r2_key": r2_key,
        "tool_name": tool_name,
    }
    return {
        "tool_call_id": tool_call_id,
        "role": "tool",
        "name": tool_name,
        "content": json.dumps(payload, sort_keys=True, separators=(",", ":")),
    }


# ---------------------------------------------------------------------------
# D7 — content-intrinsic gates
# ---------------------------------------------------------------------------


def should_digest(tool_name: str, content: str) -> bool:
    """Return True when *content* is eligible for digestion on intrinsic grounds (D7).

    Owns only the content-intrinsic gates — per-tool opt-out, error-payload
    verbatim, and the size threshold. Recency (``keep``) and read→edit dependency
    pinning are turn-stateful and live in PR-B; the master flag
    (``tool_result_compression_enabled``) is checked by the PR-B caller.

    Args:
        tool_name: The originating tool's name.
        content: The verbatim tool-result content string.
    """
    exclude: Sequence[str] = settings.tool_result_digest_exclude_tools
    if tool_name in exclude:
        return False
    if _content_is_error_payload(content):
        return False
    return estimate_tokens(content) >= settings.tool_result_digest_threshold_tokens


def digest_saves_enough(original_content: str, digest_message: dict[str, Any]) -> bool:
    """Return True when the digest clears the ``min_savings`` floor (D7, ``clear_at_least``).

    Skips digestion that would not pay off — the deferred-release case (b) gate in
    ADR-0085 D1.

    Args:
        original_content: The verbatim tool-result content.
        digest_message: The message produced by :func:`build_digest_message`.
    """
    digest_content = str(digest_message.get("content", ""))
    saved = estimate_tokens(original_content) - estimate_tokens(digest_content)
    return saved >= settings.tool_result_digest_min_savings_tokens


# ---------------------------------------------------------------------------
# D1 — durable persistence helper
# ---------------------------------------------------------------------------


async def persist_tool_result(
    store: R2ArtifactStore,
    *,
    r2_key: str,
    content: str,
    trace_id: str,
) -> bool:
    """Persist the verbatim tool-result bytes to R2, awaited to durable confirmation.

    Per ADR-0085 D1 the digest must not be offered against an unreadable key, so the
    caller substitutes the digest only when this returns ``True``. A store failure
    degrades safely — the caller leaves the result verbatim (no digest, no broken
    pointer). The PR-B caller enforces the
    ``tool_result_digest_put_timeout_ms`` ceiling around this call.

    Args:
        store: The R2 artifact store.
        r2_key: Canonical destination key.
        content: Verbatim tool-result content to store.
        trace_id: Originating request trace_id (identity threading, ADR-0074).

    Returns:
        ``True`` on durable confirmation, ``False`` on any store error.
    """
    try:
        await store.put(
            r2_key=r2_key,
            content=content.encode("utf-8"),
            content_type="text/plain; charset=utf-8",
            trace_id=trace_id,
        )
        return True
    except ArtifactStoreError as exc:
        log.warning(
            "tool_result_persist_failed",
            r2_key=r2_key,
            trace_id=trace_id,
            error=str(exc),
        )
        return False


# ---------------------------------------------------------------------------
# D1/D4 — per-round insertion-time digest pass (keep-window deferred)
# ---------------------------------------------------------------------------


def _is_existing_digest(content: str) -> bool:
    """True when *content* is already an ADR-0085 digest payload (idempotency guard)."""
    try:
        parsed = json.loads(content)
    except (TypeError, ValueError):
        return False
    return isinstance(parsed, dict) and parsed.get("_digest") is True


def _safe_session_uuid(session_id: str) -> UUID | None:
    """Parse *session_id* as a UUID, returning ``None`` when it is not one."""
    try:
        return UUID(session_id)
    except (TypeError, ValueError):
        return None


def _update_pins(ctx: "ExecutionContext", sidecar: dict[str, dict[str, Any]]) -> None:
    """Maintain read→write dependency pins from the current batch (ADR-0085 §D4).

    Records a pin for each ``read`` with a ``path``; releases prior-round pins
    whose path saw a successful ``write`` this round (except a same-batch
    read+write to that path — Codex Q2, deferred); and releases pins past the
    ``pin_ttl_turns`` abandonment bound. A failed ``write`` never releases.
    """
    round_now = ctx.tool_iteration_count
    from personal_agent.orchestrator.types import ToolResultPin

    reads_this_batch: set[str] = set()
    writes_ok_this_batch: set[str] = set()
    for tool_call_id, info in sidecar.items():
        tool_name = str(info.get("tool_name") or "")
        args = info.get("arguments") or {}
        path = args.get("path") if isinstance(args, dict) else None
        if tool_name == "read" and isinstance(path, str) and path:
            ctx.tool_result_pins[tool_call_id] = ToolResultPin(path=path, round_pinned=round_now)
            reads_this_batch.add(path)
        elif tool_name == "write" and info.get("success") and isinstance(path, str) and path:
            writes_ok_this_batch.add(path)

    # Release prior-round pins on a successful write — unless the same path was
    # also read this batch (concurrent dispatch; defer to next round / TTL).
    for path in writes_ok_this_batch - reads_this_batch:
        for pinned_id in [tid for tid, pin in ctx.tool_result_pins.items() if pin.path == path]:
            del ctx.tool_result_pins[pinned_id]

    ttl = settings.tool_result_digest_pin_ttl_turns
    for stale_id in [
        tid for tid, pin in ctx.tool_result_pins.items() if round_now - pin.round_pinned >= ttl
    ]:
        del ctx.tool_result_pins[stale_id]


def _digest_candidate_indices(ctx: "ExecutionContext") -> list[int]:
    """Return indices of tool messages eligible for digestion (keep-window deferred).

    Protects the most-recent ``tool_result_digest_keep`` tool results (the recency
    floor, which doubles as the conservative D6 reasoning floor), already-digested
    messages, pinned reads, and content that fails :func:`should_digest`.
    """
    keep = settings.tool_result_digest_keep
    tool_indices = [i for i, m in enumerate(ctx.messages) if m.get("role") == "tool"]
    protected = set(tool_indices[-keep:]) if keep > 0 else set()

    candidates: list[int] = []
    for i in tool_indices:
        if i in protected:
            continue
        msg = ctx.messages[i]
        if msg.get("tool_call_id") in ctx.tool_result_pins:
            continue
        content = msg.get("content")
        if not isinstance(content, str) or not content or _is_existing_digest(content):
            continue
        if not should_digest(str(msg.get("name") or ""), content):
            continue
        candidates.append(i)
    return candidates


async def apply_intra_turn_digest(
    ctx: "ExecutionContext",
    sidecar: dict[str, dict[str, Any]],
    *,
    trace_ctx: "TraceContext | None" = None,
    store: R2ArtifactStore,
    bus: "EventBus | None" = None,
) -> None:
    """Digest aged tool results in ``ctx.messages`` in place (ADR-0085 §D1/§D4).

    Keep-window-deferred semantics (owner decision): run after the current batch is
    appended; digest oversized, eligible, unpinned tool messages that are *older*
    than the most-recent ``tool_result_digest_keep`` results, so the model keeps its
    latest output verbatim. Each digest persists the full bytes to R2 (awaited to
    durable confirmation) and replaces the message content in place — preserving
    ``tool_call_id``/``role``/``name`` (D6 adjacency). R2 puts run concurrently,
    bounded by ``tool_result_digest_put_timeout_ms``; on timeout/failure the result
    is left verbatim.

    Args:
        ctx: The live execution context (``messages``/``tool_result_pins`` mutated).
        sidecar: Current-batch metadata keyed by ``tool_call_id`` →
            ``{"tool_name", "success", "arguments"}`` (the ``success``/``path`` the
            transcript message does not carry).
        trace_ctx: Optional trace context (reserved for span threading).
        store: The R2 artifact store.
        bus: Optional event bus for digest telemetry.
    """
    _update_pins(ctx, sidecar)

    candidates = _digest_candidate_indices(ctx)
    if not candidates:
        return

    session_uuid = _safe_session_uuid(ctx.session_id)
    if session_uuid is None:
        log.warning(
            "tool_result_digest_skipped_non_uuid_session",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
        )
        return

    async def _persist_one(index: int) -> tuple[int, dict[str, Any], str, str, str, str] | None:
        msg = ctx.messages[index]
        content = str(msg["content"])
        tool_call_id = str(msg["tool_call_id"])
        tool_name = str(msg.get("name") or "")
        try:
            key = build_tool_result_key(session_uuid, ctx.trace_id, tool_call_id)
        except ArtifactKeyError as exc:
            log.warning(
                "tool_result_digest_key_error",
                trace_id=ctx.trace_id,
                tool_call_id=tool_call_id,
                error=str(exc),
            )
            return None
        if not await persist_tool_result(store, r2_key=key, content=content, trace_id=ctx.trace_id):
            return None
        return index, {}, content, tool_call_id, tool_name, key

    try:
        persisted = await asyncio.wait_for(
            asyncio.gather(*[_persist_one(i) for i in candidates]),
            timeout=settings.tool_result_digest_put_timeout_ms / 1000,
        )
    except asyncio.TimeoutError:
        log.warning(
            "tool_result_digest_put_timeout",
            trace_id=ctx.trace_id,
            session_id=ctx.session_id,
            candidate_count=len(candidates),
        )
        return

    from personal_agent.telemetry.tool_result_digest import (
        ToolResultDigestRecord,
        record_digest,
    )

    for item in persisted:
        if item is None:
            continue
        index, _unused, content, tool_call_id, tool_name, key = item
        content_hash = compute_content_hash(content)
        body = digest_tool_content(tool_name, content)
        full_byte_len = len(content.encode("utf-8"))
        digest_msg = build_digest_message(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            r2_key=key,
            content_hash=content_hash,
            full_byte_len=full_byte_len,
            body=body,
        )
        if not digest_saves_enough(content, digest_msg):
            continue
        ctx.messages[index]["content"] = digest_msg["content"]
        await record_digest(
            ToolResultDigestRecord(
                trace_id=ctx.trace_id,
                session_id=ctx.session_id,
                tool_name=tool_name,
                tool_call_id=tool_call_id,
                bytes_in=full_byte_len,
                tokens_in=estimate_tokens(content),
                tokens_out=estimate_tokens(str(digest_msg["content"])),
                format=str(body.get("format", "")),
                persisted=True,
                r2_key=key,
                content_hash=content_hash,
            ),
            bus,
        )
