"""Literal entity-mention verification (FRE-1041).

The precision half of the graph-anchored entity resolver. The ``turn_entity_fulltext``
index (ADR-0104) supplies *recall* — it will happily return ``Ice cream maker`` for a
message that never says it — and these functions supply *precision* by keeping only the
names the message literally contains.

Matching is contiguous-token-run containment, never substring containment: ``Ice`` must
not match inside ``nice``, and a multi-word name must appear as consecutive words.
Tokenisation splits on every non-alphanumeric character, which is what lets a lowercase
``melon/canteloupe`` reach the graph's ``Melon`` — the FRE-1041 decisive case, where a
whitespace-only split leaves one token that matches nothing.

There is deliberately **no stemming and no fuzzy matching**. Morphological folding would
reintroduce exactly the false-positive class this design removes, so ``melons`` does not
match ``Melon`` and the misspelling ``canteloupe`` does not reach ``Cantaloupe``. That is
a stated limitation of the design, not an oversight.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable, Sequence

_TOKEN_RE = re.compile(r"[^\W_]+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    """Split text into casefolded alphanumeric tokens.

    Args:
        text: Arbitrary user text, or an entity name.

    Returns:
        NFKC-normalised, casefolded tokens with every non-alphanumeric character
        (including underscores) treated as a separator.
    """
    return [t.casefold() for t in _TOKEN_RE.findall(unicodedata.normalize("NFKC", text))]


def mentions(name: str, message_tokens: Sequence[str]) -> bool:
    """Report whether *name* occurs as a contiguous token run in *message_tokens*.

    Args:
        name: Candidate entity name, as stored in the graph.
        message_tokens: The message tokenised by :func:`tokenize`.

    Returns:
        True when the name's tokens appear consecutively in the message. A name that
        tokenises to nothing (blank or punctuation-only) never matches.
    """
    name_tokens = tokenize(name)
    if not name_tokens or len(name_tokens) > len(message_tokens):
        return False
    span = len(name_tokens)
    return any(
        list(message_tokens[start : start + span]) == name_tokens
        for start in range(len(message_tokens) - span + 1)
    )


def verify_mentions(message: str, candidate_names: Iterable[str]) -> list[str]:
    """Keep the candidate names the message literally mentions.

    Args:
        message: The verbatim user message.
        candidate_names: Entity names from the full-text index, best-first.

    Returns:
        The mentioned names in the caller's order, de-duplicated, and in the graph's
        own casing — ``memory.proactive._overlap_subscore`` intersects case-sensitively,
        so a re-cased name would silently contribute nothing.
    """
    message_tokens = tokenize(message)
    if not message_tokens:
        return []
    verified: list[str] = []
    seen: set[str] = set()
    for name in candidate_names:
        if name not in seen and mentions(name, message_tokens):
            seen.add(name)
            verified.append(name)
    return verified
