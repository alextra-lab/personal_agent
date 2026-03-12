"""Fingerprint-based deduplication for Captain's Log proposals (ADR-0030).

Computes deterministic fingerprints from (category, scope, normalized_what)
so that semantically equivalent proposals collapse into a single entry with
an incrementing seen_count.
"""

import hashlib
import inspect
import re

from personal_agent.captains_log.models import ChangeCategory, ChangeScope

STOPWORDS = frozenset(
    {
        "a",
        "an",
        "the",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "being",
        "to",
        "of",
        "in",
        "for",
        "on",
        "with",
        "at",
        "by",
        "from",
        "and",
        "or",
        "but",
        "not",
        "this",
        "that",
        "it",
        "its",
        "we",
        "our",
        "should",
        "could",
        "would",
        "can",
        "will",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "may",
        "might",
    }
)

_WORD_RE = re.compile(r"[a-z0-9]+")


def _normalize_text(text: str) -> str:
    """Lowercase, strip punctuation, remove stopwords, sort tokens.

    The token sort gives order-independence so "add retry logic" and
    "retry logic add" produce the same normalized form.

    Args:
        text: Free-form proposal text.

    Returns:
        Space-joined, sorted, deduplicated lowercase tokens.
    """
    # Defensive: DSPy or async LM can leave coroutines in result fields (e.g. when
    # reflection runs in asyncio.to_thread). re.findall expects str/bytes.
    if not isinstance(text, str):
        return ""
    if inspect.iscoroutine(text):
        return ""
    tokens = _WORD_RE.findall(text.lower())
    meaningful = [t for t in tokens if t not in STOPWORDS]
    return " ".join(sorted(set(meaningful)))


def compute_proposal_fingerprint(
    category: ChangeCategory,
    scope: ChangeScope,
    what: str,
) -> str:
    """Deterministic fingerprint for dedup.

    Two proposals sharing the same fingerprint are considered duplicates and
    will be merged (seen_count incremented) rather than stored separately.

    Args:
        category: Improvement category enum value.
        scope: Target subsystem enum value.
        what: Free-text description of the proposed change.

    Returns:
        First 16 hex chars of sha256(category:scope:normalized_what).
    """
    # Coerce to str so _normalize_text never receives a coroutine (avoids re error)
    what_str = what if isinstance(what, str) and not inspect.iscoroutine(what) else ""
    normalized = _normalize_text(what_str)
    key = f"{category.value}:{scope.value}:{normalized}"
    return hashlib.sha256(key.encode()).hexdigest()[:16]
