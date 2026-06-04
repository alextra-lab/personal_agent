"""`expand_tool_result` — exact-replay retrieval of a digested tool result (ADR-0085 §D5).

When the intra-turn digest pass (ADR-0085 §D1) replaces a large tool result with a
compact digest, the full verbatim bytes are persisted to R2 under a canonical key.
This tool fetches those bytes back on demand, **hash-validated** against the
``content_hash`` the digest advertised, with optional line-ranged retrieval and a
token cap so re-expansion cannot re-create the spike it removed.

The contract is deliberately kept separate from the future
``recall_session_history`` (FRE-465): this is exact byte replay from R2
(hash-validated, single-object fetch), not lossy ranked search.
"""

from __future__ import annotations

from typing import Any

from personal_agent.config import settings
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.orchestrator.tool_result_digest import compute_content_hash
from personal_agent.storage.artifact_store import ArtifactStoreError, get_artifact_store
from personal_agent.telemetry import get_logger
from personal_agent.tools.types import ToolDefinition, ToolParameter

log = get_logger(__name__)


expand_tool_result_tool = ToolDefinition(
    name="expand_tool_result",
    description=(
        "Retrieve the full verbatim bytes of a previously digested tool result from "
        "durable storage. Pass the `key` and `content_hash` shown in the digest "
        "placeholder. Use `offset`/`limit` (0-based line range) to page through a "
        "large result; the returned content is capped to a token budget. Call this "
        "before acting on lines a digest omitted."
    ),
    category="memory_read",
    parameters=[
        ToolParameter(
            name="key",
            type="string",
            description="The tool-result key shown in the digest placeholder.",
            required=True,
        ),
        ToolParameter(
            name="content_hash",
            type="string",
            description="The content_hash shown in the digest placeholder (exact-replay check).",
            required=True,
        ),
        ToolParameter(
            name="offset",
            type="number",
            description="0-based start line for ranged retrieval (optional).",
            required=False,
            default=None,
        ),
        ToolParameter(
            name="limit",
            type="number",
            description="Number of lines to return from offset (optional).",
            required=False,
            default=None,
        ),
    ],
    risk_level="low",
    allowed_modes=["NORMAL", "ALERT", "DEGRADED", "RECOVERY"],
    requires_approval=False,
    requires_sandbox=False,
    timeout_seconds=10,
    rate_limit_per_hour=60,
)


async def expand_tool_result_executor(
    key: str,
    content_hash: str,
    offset: int | None = None,
    limit: int | None = None,
    ctx: Any = None,
) -> dict[str, Any]:
    """Fetch, hash-validate, range-slice, and cap a digested tool result (ADR-0085 §D5).

    Args:
        key: Canonical R2 key from the digest placeholder.
        content_hash: Expected full-content SHA-256 hex (integrity guard).
        offset: 0-based start line for ranged retrieval.
        limit: Number of lines to return from ``offset``.
        ctx: Trace context (injected by the tool layer); used for log threading.

    Returns:
        ``{"success": True, "content": str, "truncated": bool, "key": str}`` on
        success, or ``{"success": False, "error": str}`` when the store is
        unwired, the fetch fails, or the hash does not match.
    """
    trace_id = getattr(ctx, "trace_id", None)
    store = get_artifact_store()
    if store is None:
        return {"success": False, "error": "artifact store unavailable (R2 not configured)"}

    try:
        raw = await store.get(key, trace_id=trace_id)
    except ArtifactStoreError as exc:
        log.warning("expand_tool_result_fetch_failed", key=key, trace_id=trace_id, error=str(exc))
        return {"success": False, "error": f"fetch failed for {key}: {exc}"}

    full = raw.decode("utf-8", errors="replace")
    actual_hash = compute_content_hash(full)
    if actual_hash != content_hash:
        log.warning(
            "expand_tool_result_hash_mismatch",
            key=key,
            trace_id=trace_id,
            expected=content_hash,
            actual=actual_hash,
        )
        return {
            "success": False,
            "error": f"content_hash mismatch for {key}: stored bytes do not match the digest.",
        }

    content = full
    if offset is not None or limit is not None:
        lines = full.split("\n")
        start = int(offset) if offset is not None else 0
        end = start + int(limit) if limit is not None else len(lines)
        content = "\n".join(lines[start:end])

    truncated = False
    cap_tokens = settings.tool_result_digest_max_expand_tokens
    if estimate_tokens(content) > cap_tokens:
        content = content[: cap_tokens * 4]  # ~4 chars/token heuristic
        truncated = True

    log.info(
        "tool_result_digest_reexpanded",
        key=key,
        trace_id=trace_id,
        returned_tokens=estimate_tokens(content),
        truncated=truncated,
        ranged=offset is not None or limit is not None,
    )
    return {"success": True, "content": content, "truncated": truncated, "key": key}
