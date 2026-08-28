"""Linear, ReDoS-safe extraction of ``<tool_code>...</tool_code>`` blocks.

Both :mod:`personal_agent.llm_client.tool_call_parser` (extracting call bodies) and
:mod:`personal_agent.llm_client.history_sanitiser` (stripping whole blocks) need to find
Gemini-style ``<tool_code>`` blocks in model output. A lazy ``.*?`` regex under ``re.DOTALL``
backtracks from every unmatched opening tag, which is O(k*n) whenever many ``<tool_code>``
opens have no closer.

A containment pre-check ("does the string contain a closer at all?", FRE-1308's original
fix) does not close that gap: a closer positioned earlier in the string with many unmatched
openers after it still passes the pre-check, and the regex still scans the whole unmatched
suffix (FRE-1309 measured ~28s on this shape). This module walks the string once, left to
right, using ``str.find``, so an unmatched trailing run of openers costs nothing.
"""

from __future__ import annotations

from collections.abc import Iterator

TOOL_CODE_OPEN_TAG = "<tool_code>"
TOOL_CODE_CLOSE_TAG = "</tool_code>"


def iter_tool_code_blocks(content: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, body)`` for each ``<tool_code>...</tool_code>`` block.

    Case-insensitive. Matches the leftmost / non-overlapping / lazy semantics of
    ``re.compile(r"<tool_code>(.*?)</tool_code>", re.DOTALL | re.IGNORECASE)`` — each
    block closes at the nearest closing tag after its opener, and scanning for the next
    block resumes right after that closer — but never rescans an unmatched opener: every
    position in ``content`` is visited at most once via ``str.find``.

    Args:
        content: Text to scan. Not mutated.

    Yields:
        ``(start, end, body)`` tuples where ``start``/``end`` bound the full match
        (both tags included) and ``body`` is the original-case text between them.
    """
    lower = content.lower()
    pos = 0
    open_len = len(TOOL_CODE_OPEN_TAG)
    close_len = len(TOOL_CODE_CLOSE_TAG)
    while True:
        start = lower.find(TOOL_CODE_OPEN_TAG, pos)
        if start == -1:
            return
        body_start = start + open_len
        close_start = lower.find(TOOL_CODE_CLOSE_TAG, body_start)
        if close_start == -1:
            return
        end = close_start + close_len
        yield start, end, content[body_start:close_start]
        pos = end
