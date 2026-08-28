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
right, so an unmatched trailing run of openers costs nothing.

Matching is done with case-insensitive regex search for the literal tags rather than
comparing against a ``str.lower()`` copy: some code points (e.g. ``"İ".lower() == "i̇"``,
two characters) change length under ``.lower()``, which would desync offsets found in a
lowercased copy from positions in the original string and corrupt the slicing below. A
literal (non-``DOTALL``, no ``.*?``) pattern under ``re.IGNORECASE`` has no backtracking
blowup — it is the same shape as ``str.find``, just length-safe.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

TOOL_CODE_OPEN_TAG = "<tool_code>"
TOOL_CODE_CLOSE_TAG = "</tool_code>"

_OPEN_RE = re.compile(re.escape(TOOL_CODE_OPEN_TAG), re.IGNORECASE)
_CLOSE_RE = re.compile(re.escape(TOOL_CODE_CLOSE_TAG), re.IGNORECASE)


def iter_tool_code_blocks(content: str) -> Iterator[tuple[int, int, str]]:
    """Yield ``(start, end, body)`` for each ``<tool_code>...</tool_code>`` block.

    Case-insensitive. Matches the leftmost / non-overlapping / lazy semantics of
    ``re.compile(r"<tool_code>(.*?)</tool_code>", re.DOTALL | re.IGNORECASE)`` — each
    block closes at the nearest closing tag after its opener, and scanning for the next
    block resumes right after that closer — but never rescans an unmatched opener: every
    position in ``content`` is visited at most once.

    Args:
        content: Text to scan. Not mutated.

    Yields:
        ``(start, end, body)`` tuples where ``start``/``end`` bound the full match
        (both tags included) and ``body`` is the original-case text between them.
    """
    pos = 0
    while True:
        m_open = _OPEN_RE.search(content, pos)
        if m_open is None:
            return
        body_start = m_open.end()
        m_close = _CLOSE_RE.search(content, body_start)
        if m_close is None:
            return
        yield m_open.start(), m_close.end(), content[body_start : m_close.start()]
        pos = m_close.end()
