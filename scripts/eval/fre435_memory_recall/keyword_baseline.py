"""FRE-656 — BM25 keyword baseline over the FRE-489 probe.

The evidence behind the FRE-656 finding (docs/research/2026-06-28-fre-656-
embedder-benchmark.md): on the FRE-489 probe a plain keyword search over the
stored entity text matches or beats the vector embedder, because the queries —
while they never name their target entity — still share ordinary content
vocabulary with the entity *description*. The probe therefore tests lexical
recall with the label hidden, not semantic recall, and cannot distinguish
embedders or justify the vector path.

This is a self-contained, dependency-light reproduction (a small BM25 over the
probe YAML, no substrate, no embedder). FRE-670 will fold a BM25 column into the
main harness as a standing lexical-leakage guard; this script is the FRE-656
proof artifact.

Run:
    uv run python scripts/eval/fre435_memory_recall/keyword_baseline.py
"""

from __future__ import annotations

import math
import re
from collections import Counter
from pathlib import Path

import yaml

_PROBE = Path(__file__).parent / "bespoke_probe.yaml"

# Query-side stopwords: ordinary function words + the conversational scaffolding
# the probe queries use ("pick up where we left off", "what was the core idea").
# Dropping them keeps BM25 honest — it must match on *content*, not filler.
_STOP = set(
    "the a an of to in on and or is are was were be been being it its this that "
    "what left off pick up we core idea i m my you your for with how do does did "
    "lets let s about can could would there here some more most".split()
)


def _content_tokens(text: str | None) -> list[str]:
    """Lowercase content tokens (>2 chars, non-stopword) of ``text``."""
    return [
        t for t in re.findall(r"[a-z0-9]+", (text or "").lower()) if t not in _STOP and len(t) > 2
    ]


def _bm25_scores(
    query: list[str], docs: list[list[str]], k1: float = 1.5, b: float = 0.75
) -> list[float]:
    """Score ``query`` against each document in ``docs`` with Okapi BM25.

    Args:
        query: Tokenised query.
        docs: Tokenised candidate documents (the co-resident entity texts).
        k1: BM25 term-frequency saturation.
        b: BM25 length-normalisation.

    Returns:
        One BM25 score per document, in ``docs`` order.
    """
    n = len(docs)
    df: Counter[str] = Counter()
    for doc in docs:
        df.update(set(doc))
    idf = {w: math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) for w in df}
    avgdl = sum(len(d) for d in docs) / n

    scores: list[float] = []
    for doc in docs:
        tf = Counter(doc)
        score = 0.0
        for w in query:
            if w in tf:
                score += (
                    idf.get(w, 0.0)
                    * tf[w]
                    * (k1 + 1)
                    / (tf[w] + k1 * (1 - b + b * len(doc) / avgdl))
                )
        scores.append(score)
    return scores


def main() -> None:
    """Print BM25 recall@1/@5 for name-only and name+description documents."""
    cases = yaml.safe_load(_PROBE.read_text())["cases"]

    for use_description, label in ((False, "name ONLY"), (True, "name + description")):
        docs: list[list[str]] = []
        names: list[str] = []
        for case in cases:
            for entity in case.get("seed_entities", []):
                text = entity["name"]
                if use_description:
                    text += " " + (entity.get("description") or "")
                docs.append(_content_tokens(text))
                names.append(entity["name"])

        hit1 = hit5 = scored = leak = 0
        for case in cases:
            expected = set(case["expected"]["entity_names"])
            if not expected:
                continue
            scored += 1
            if any(name.lower() in case["query"].lower() for name in expected):
                leak += 1
            query = _content_tokens(case["query"])
            ranked = [
                name
                for _, name in sorted(
                    zip(_bm25_scores(query, docs), names, strict=True), key=lambda x: -x[0]
                )
            ]
            hit1 += ranked[0] in expected
            hit5 += any(r in expected for r in ranked[:5])

        print(
            f"BM25 query <-> {label:18s}: "
            f"recall@1={hit1}/{scored}={hit1 / scored:.3f}  "
            f"recall@5={hit5}/{scored}={hit5 / scored:.3f}  "
            f"(name-in-query leakage={leak}/{scored})"
        )
    print("vector (0.6B or 4B) query <-> name+description: recall@5=0.722  (see writeup)")


if __name__ == "__main__":
    main()
