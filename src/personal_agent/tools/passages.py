"""Passage split and ranker arms for ``fetch_url`` focus mode (ADR-0158 D3.2, D4).

FRE-1641 (ADR-0158 T1) scores these functions offline on recorded page fixtures, and T3 wires
the arm and budget that the harness picks into ``fetch_url``. One module serves both, so the
form the harness scores is the form the tool returns (D1.8).

Every passage is a verbatim slice of the extracted page text, with its character offsets: the
tool never rewrites, joins or paraphrases a passage (D3.7).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from typing import Literal

#: A block shorter than this merges with the next block (D3.2).
MIN_PASSAGE_CHARS = 200
#: A block longer than this splits at a sentence end (D3.2).
MAX_PASSAGE_CHARS = 800

#: R2 reranks only the first passages of a page (D4).
R2_MAX_PASSAGES = 100
#: R3 reranks only the BM25 top passages (D4).
R3_BM25_TOP = 20
#: R4 runs R1 up to this many passages, and R3 above it (D4).
R4_LOCAL_MAX_PASSAGES = 50

_BM25_K1 = 1.5
_BM25_B = 0.75

_WORD = re.compile(r"\w+")
_SENTENCE_END = re.compile(r"[.!?][\"')\]]?(?=\s)")

STOP_WORDS = frozenset(
    """
    a about above after again against all am an and any are as at be because been before being
    below between both but by can did do does doing down during each few for from further had
    has have having he her here hers herself him himself his how i if in into is it its itself
    just me more most my myself no nor not now of off on once only or other our ours ourselves
    out over own same she should so some such than that the their theirs them themselves then
    there these they this those through to too under until up very was we were what when where
    which while who whom why will with would you your yours yourself yourselves
    """.split()
)

RankerArm = Literal["R1", "R2", "R3", "R4"]


@dataclass(frozen=True)
class Passage:
    """One verbatim slice of the extracted page text.

    Attributes:
        index: Position of the passage in page order, from 0.
        start: Offset of the first character in the page text.
        end: Offset after the last character, so ``text == page[start:end]``.
        text: The passage text.
    """

    index: int
    start: int
    end: int
    text: str


@dataclass(frozen=True)
class RerankCall:
    """The result of one rerank call.

    Attributes:
        scores: One relevance score per document, in the order the documents were sent.
        tokens: Tokens the vendor billed for the call.
    """

    scores: Sequence[float]
    tokens: int


#: Sends ``(query, documents)`` to a reranker. The caller supplies it, so the module holds no
#: vendor client, no key and no cost recording.
RerankFn = Callable[[str, Sequence[str]], Awaitable[RerankCall]]


@dataclass(frozen=True)
class RankOutcome:
    """The ranked candidates of one page.

    Attributes:
        order: Passage indices, best first. A passage that the arm does not rank is absent.
        rerank_calls: Number of rerank calls made.
        rerank_tokens: Tokens billed over those calls.
        sent_to_vendor: Whether page text left the host.
    """

    order: list[int]
    rerank_calls: int
    rerank_tokens: int
    sent_to_vendor: bool


def _sentence_cuts(text: str, start: int, end: int) -> list[tuple[int, int]]:
    """Split ``text[start:end]`` into spans of at most ``MAX_PASSAGE_CHARS``.

    The cut goes after the last sentence end in the window, else at the last whitespace, else
    hard at the bound. Whitespace at the start of the next span is skipped.
    """
    spans: list[tuple[int, int]] = []
    pos = start
    while end - pos > MAX_PASSAGE_CHARS:
        window = text[pos : pos + MAX_PASSAGE_CHARS]
        cut = 0
        for match in _SENTENCE_END.finditer(window):
            cut = match.end()
        if cut == 0:
            cut = max(window.rfind(" "), window.rfind("\n"))
            cut = cut if cut > 0 else MAX_PASSAGE_CHARS
        spans.append((pos, pos + cut))
        pos += cut
        while pos < end and text[pos].isspace():
            pos += 1
    if pos < end:
        spans.append((pos, end))
    return spans


def split_passages(text: str) -> list[Passage]:
    """Split extracted page text into passages (ADR-0158 D3.2).

    A block is a line of the extractor's output. A block shorter than ``MIN_PASSAGE_CHARS``
    merges with the next block. A block longer than ``MAX_PASSAGE_CHARS`` splits at a sentence
    end.

    Args:
        text: The extracted page text.

    Returns:
        The passages in page order. Each one is ``text[start:end]``.
    """
    blocks: list[tuple[int, int]] = []
    pos = 0
    for line in text.split("\n"):
        stripped = line.strip()
        if stripped:
            first = pos + line.index(stripped)
            blocks.append((first, first + len(stripped)))
        pos += len(line) + 1

    spans: list[tuple[int, int]] = []
    i = 0
    while i < len(blocks):
        start, end = blocks[i]
        while end - start < MIN_PASSAGE_CHARS and i + 1 < len(blocks):
            i += 1
            end = blocks[i][1]
        spans.extend(_sentence_cuts(text, start, end))
        i += 1

    return [Passage(index=n, start=s, end=e, text=text[s:e]) for n, (s, e) in enumerate(spans)]


def tokenize(text: str) -> list[str]:
    """Return the lower-case word tokens of ``text`` that are not stop words.

    Args:
        text: Any text.

    Returns:
        The tokens in order.
    """
    return [t for t in _WORD.findall(text.lower()) if t not in STOP_WORDS]


def bm25_order(focus: str, passages: Sequence[Passage]) -> list[int]:
    """Rank passages against ``focus`` with Okapi BM25 (arm R1).

    Args:
        focus: The question the page must answer.
        passages: The passages of one page.

    Returns:
        Every passage index, best first. Equal scores keep page order.
    """
    docs = [Counter(tokenize(p.text)) for p in passages]
    if not docs:
        return []
    lengths = [sum(d.values()) for d in docs]
    avg_len = (sum(lengths) / len(lengths)) or 1.0
    n = len(docs)
    terms = set(tokenize(focus))
    idf = {
        t: math.log((n - df + 0.5) / (df + 0.5) + 1.0)
        for t in terms
        for df in [sum(1 for d in docs if t in d)]
    }

    def score(i: int) -> float:
        d, length = docs[i], lengths[i]
        total = 0.0
        for t in terms:
            tf = d.get(t, 0)
            if tf:
                norm = tf + _BM25_K1 * (1 - _BM25_B + _BM25_B * length / avg_len)
                total += idf[t] * tf * (_BM25_K1 + 1) / norm
        return total

    scores = [score(i) for i in range(n)]
    return sorted(range(n), key=lambda i: (-scores[i], i))


async def _rerank_order(
    focus: str, passages: Sequence[Passage], candidates: Sequence[int], rerank: RerankFn
) -> tuple[list[int], int]:
    """Order ``candidates`` by the reranker's scores. Equal scores keep candidate order."""
    call = await rerank(focus, [passages[i].text for i in candidates])
    ranked = sorted(range(len(candidates)), key=lambda k: (-call.scores[k], k))
    return [candidates[k] for k in ranked], call.tokens


async def rank_passages(
    arm: RankerArm, focus: str, passages: Sequence[Passage], rerank: RerankFn
) -> RankOutcome:
    """Rank one page's passages with one arm of ADR-0158 D4.

    - R1: BM25 over all passages, local.
    - R2: the reranker over the first ``R2_MAX_PASSAGES`` passages.
    - R3: BM25 picks the top ``R3_BM25_TOP`` passages, then the reranker orders them.
    - R4: R1 for a page with at most ``R4_LOCAL_MAX_PASSAGES`` passages, R3 for a larger page.

    Args:
        arm: The arm.
        focus: The question the page must answer.
        passages: The passages of one page.
        rerank: The reranker call. Only R2, R3 and a large-page R4 use it.

    Returns:
        The ranked candidates and what the ranking cost.
    """
    if not passages:
        return RankOutcome(order=[], rerank_calls=0, rerank_tokens=0, sent_to_vendor=False)
    if arm == "R4":
        arm = "R1" if len(passages) <= R4_LOCAL_MAX_PASSAGES else "R3"
    match arm:
        case "R1":
            return RankOutcome(
                order=bm25_order(focus, passages),
                rerank_calls=0,
                rerank_tokens=0,
                sent_to_vendor=False,
            )
        case "R2":
            candidates = list(range(min(len(passages), R2_MAX_PASSAGES)))
        case "R3":
            candidates = bm25_order(focus, passages)[:R3_BM25_TOP]
    order, tokens = await _rerank_order(focus, passages, candidates, rerank)
    return RankOutcome(order=order, rerank_calls=1, rerank_tokens=tokens, sent_to_vendor=True)


def focus_matched(focus: str, passages: Sequence[Passage]) -> bool:
    """Whether a returned passage holds a term of the focus (ADR-0158 D3.2).

    The definition is lexical and the same for every arm: at least one passage holds a token of
    ``focus`` that is not a stop word. It does not depend on a ranker's score.

    Args:
        focus: The question the page must answer.
        passages: The returned passages.

    Returns:
        True if a passage holds a focus term.
    """
    terms = set(tokenize(focus))
    return any(terms.intersection(tokenize(p.text)) for p in passages)


def select_within_budget(
    order: Sequence[int], passages: Sequence[Passage], budget: int
) -> list[Passage]:
    """Take the best passages that fit a character budget, in page order (D3.2).

    Passages are taken in rank order. A passage that does not fit the remaining budget is
    skipped, and a later, shorter one can still fit.

    Args:
        order: Passage indices, best first.
        passages: The passages of the page.
        budget: The character budget for the passage texts.

    Returns:
        The chosen passages, sorted by page order.
    """
    chosen: list[Passage] = []
    used = 0
    for i in order:
        size = len(passages[i].text)
        if used + size <= budget:
            chosen.append(passages[i])
            used += size
    return sorted(chosen, key=lambda p: p.index)
