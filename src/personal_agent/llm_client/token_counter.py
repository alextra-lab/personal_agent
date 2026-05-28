"""Unified token counter for the Personal Agent harness.

Replaces the two divergent heuristics previously used:
- ``request_gateway/budget.py``: ``int(len(text.split()) * 1.3)``
- ``orchestrator/context_window.py``: ``len(text) // 4``

Uses tiktoken cl100k_base encoding (adequate approximation for Claude and GPT
model families). Encoding object is cached at module level — cold load ~20ms,
warm calls <1ms.
"""

from __future__ import annotations

import tiktoken

_ENCODING: tiktoken.Encoding | None = None


def _get_encoding() -> tiktoken.Encoding:
    global _ENCODING
    if _ENCODING is None:
        _ENCODING = tiktoken.get_encoding("cl100k_base")
    return _ENCODING


def estimate_tokens(text: str, model_family: str = "claude") -> int:
    """Return token count estimate for text under the given model family.

    Args:
        text: The text to count tokens for.
        model_family: Model family hint. Currently maps to cl100k_base for all
            families (adequate approximation for Claude/GPT).

    Returns:
        Integer token count. Returns 0 for empty or whitespace-only input.
    """
    if not text or not text.strip():
        return 0
    return len(_get_encoding().encode(text))
