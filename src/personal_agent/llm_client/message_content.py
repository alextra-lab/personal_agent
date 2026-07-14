"""Block-aware helpers for message ``content`` fields (ADR-0101 §2, FRE-664).

``content`` widens from a plain ``str`` to ``str | list[dict[str, Any]]`` once an
attachment resolves to a typed content block (e.g. ``image_url``, ticket 4). Every
site that previously assumed ``str`` must route through these helpers so list
content degrades safely — text preserved, non-text blocks skipped — instead of
being silently stringified, corrupted, or collapsed to an empty string.
"""

from __future__ import annotations

from typing import Any

from personal_agent.llm_client.token_counter import estimate_tokens

MessageContent = str | list[dict[str, Any]]

# Fixed per-image token estimate (ADR-0101 §8b: "≈1600 tokens max after resize").
# Used for context-window budgeting until an image is actually resolved; a future
# ticket may replace this with provider-reported usage once resolution lands.
IMAGE_BLOCK_TOKEN_ESTIMATE = 1600

# Per-page token estimate for the native PDF document block (ADR-0102 §4 cost
# note: "per page you pay both ~1.5-3k text tokens and image tokens" — provider
# extracts text AND rasterizes each page). Upper-bound text (3000) + one image
# tile (IMAGE_BLOCK_TOKEN_ESTIMATE, 1600) = 4600. Deliberately upper-bound, not
# midpoint: this gates a user-facing spend-confirmation threshold, so erring
# toward asking for confirmation is the safe direction (ADR-0102 "the user is
# never surprised by an expensive PDF"). Approximate by construction (ADR-0102
# §"Pre-flight estimate is approximate"); reconciled at commit via real usage.
DOCUMENT_NATIVE_PAGE_TOKEN_ESTIMATE = 4600


def get_text_content(content: Any) -> str:
    """Extract the text portion of a message ``content`` field.

    Args:
        content: Either a plain string or a list of typed content blocks
            (e.g. ``{"type": "text", "text": ...}``, ``{"type": "image_url", ...}``).

    Returns:
        The string unchanged; the blank-line-joined text of every ``text``-type
        block for list content; or ``""`` for anything else (``None``, an
        image-only block list, or an unrecognized/malformed shape).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "text":
                continue
            text = block.get("text", "")
            if isinstance(text, str) and text:
                parts.append(text)
        return "\n\n".join(parts)
    return ""


def merge_content(old: Any, new: Any) -> Any:
    r"""Merge two message ``content`` fields (duplicate-role-merge helper).

    String + string keeps the historical ``"{old}\n\n{new}"`` behavior. Once
    either side is a list of blocks, string-interpolating would corrupt or drop
    the block(s) (an f-string over a list embeds its Python repr) — instead both
    sides are normalized to block lists and concatenated, so every block from
    both sides survives in order.

    Args:
        old: Prior message's content.
        new: Incoming message's content being merged in.

    Returns:
        Merged content: a string when both inputs were strings (or empty), else
        a list of content blocks.
    """
    if not isinstance(old, list) and not isinstance(new, list):
        old_s, new_s = (old or ""), (new or "")
        if old_s and new_s:
            return f"{old_s}\n\n{new_s}"
        return new_s or old_s

    def _as_blocks(c: Any) -> list[dict[str, Any]]:
        if isinstance(c, list):
            return [b for b in c if isinstance(b, dict)]
        if isinstance(c, str) and c:
            return [{"type": "text", "text": c}]
        return []

    return _as_blocks(old) + _as_blocks(new)


def count_content_tokens(content: Any) -> int:
    """Estimate token count for a message ``content`` field, block-aware.

    Args:
        content: Plain string or list of typed content blocks.

    Returns:
        ``estimate_tokens`` over the full string for ``str`` content; for list
        content, the sum of ``estimate_tokens`` over each text block's text plus
        ``IMAGE_BLOCK_TOKEN_ESTIMATE`` for each non-text block. ``0`` for
        anything else (or a malformed text value that isn't a string).
    """
    if isinstance(content, str):
        return estimate_tokens(content)
    if isinstance(content, list):
        total = 0
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text":
                text = block.get("text", "")
                total += estimate_tokens(text) if isinstance(text, str) else 0
            else:
                total += IMAGE_BLOCK_TOKEN_ESTIMATE
        return total
    return 0
