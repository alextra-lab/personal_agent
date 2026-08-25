"""FRE-1281 — the extractor may not be special-cased to the corpus (ADR-0138 AC-7).

ADR-0138's governance rule is that probes are held out because "An implementation cannot
special-case probes it has not seen." A single session authoring both the corpus and the
extractor cannot make itself blind by assertion, so the *harm* that rule prevents —
memorised documents, surface phrases, or a denylist tuned to the probes — is prevented
mechanically instead.

**The rule, and the one exception that makes it honest.** No long string may appear in
both ``corpus.yaml`` and the extractor's source. The exception is a string that also
appears in **ADR-0138 itself**: those came from the specification, which both the corpus
and the prompt are entitled to quote. ``print("Paris has 9 million residents")`` is the
ADR's own illustration of the string-literal channel, and treating it as leakage would
punish an implementation for following the document it implements.

That exception is checked against the ADR file, not hand-waved by an allowlist — a phrase
earns it by being in the spec, not by being inconvenient.
"""

from __future__ import annotations

import re
from pathlib import Path

ADR_PATH = Path("docs/architecture_decisions/ADR-0138-the-model-may-generate-but-may-not-assert.md")
CORPUS_PATH = Path("scripts/eval/fre1281_span_extraction/corpus.yaml")
EXTRACTOR_DIR = Path("src/personal_agent/grounding")

MIN_LEAK_LENGTH = 20
"""Shortest shared string treated as leakage.

Long enough that ordinary shared vocabulary ("claim_non_exempt", "attributed
restatement") does not trip it, short enough to catch a memorised sentence fragment.
"""

PHRASE_WORDS = 8
"""Words per compared phrase.

Started at 5 and was raised, for a stated reason rather than to make the test pass. At 5
the guard flagged ``from collections.abc import Sequence`` and ``log =
structlog.get_logger(__name__)`` — ubiquitous Python that the corpus happens to quote in
its code-body documents and that any module in this repo contains. Those are shared
idiom, not memorised probes. Eight words requires surrounding context to match too, which
is what distinguishes quoting a probe from writing ordinary Python.
"""

_WORD = re.compile(r"[A-Za-z][A-Za-z0-9'`\-]*")


def probe_text() -> str:
    """The corpus's probe content: document text and user turns only.

    Deliberately excludes ``note`` fields and YAML comments. Those are the labeller's
    commentary *about* the probes — reasoning that legitimately echoes the same design
    vocabulary the implementation's docstrings use, since both were written against
    ADR-0138. Leakage means the extractor encodes the probes, not that two documents
    written by the same author explain the same rule in similar words.

    Returns:
        Every document ``text`` and ``user_message`` in the corpus, concatenated.
    """
    import yaml

    raw = yaml.safe_load(CORPUS_PATH.read_text(encoding="utf-8"))
    parts: list[str] = []
    for document in raw.get("documents", []):
        parts.append(str(document.get("text", "")))
        if document.get("user_message"):
            parts.append(str(document["user_message"]))
    return "\n".join(parts)


def _phrases(text: str, length: int = PHRASE_WORDS) -> set[str]:
    """Every ``length``-word phrase in ``text``, normalised for whitespace and case.

    Args:
        text: Source text.
        length: Words per phrase.

    Returns:
        Normalised phrases at least :data:`MIN_LEAK_LENGTH` characters long.
    """
    words = [w.lower() for w in _WORD.findall(text)]
    out: set[str] = set()
    for i in range(len(words) - length + 1):
        phrase = " ".join(words[i : i + length])
        if len(phrase) >= MIN_LEAK_LENGTH:
            out.add(phrase)
    return out


def test_extractor_source_does_not_quote_the_corpus() -> None:
    """No corpus phrase appears in the extractor unless the ADR says it first."""
    corpus_phrases = _phrases(probe_text())
    adr_phrases = _phrases(ADR_PATH.read_text(encoding="utf-8"))

    leaked: dict[str, list[str]] = {}
    for source in sorted(EXTRACTOR_DIR.rglob("*.py")):
        shared = _phrases(source.read_text(encoding="utf-8")) & corpus_phrases
        from_spec = shared & adr_phrases
        genuine = shared - from_spec
        if genuine:
            leaked[str(source)] = sorted(genuine)[:5]

    assert not leaked, (
        "extractor source shares phrasing with the corpus that ADR-0138 does not itself "
        f"contain — this is the special-casing AC-7's held-out rule exists to prevent: {leaked}"
    )


def test_the_leakage_guard_would_catch_a_seeded_leak() -> None:
    """Seeded negative — a guard never shown to reject anything has not been shown to work."""
    corpus_phrases = _phrases(probe_text())
    adr_phrases = _phrases(ADR_PATH.read_text(encoding="utf-8"))

    # A distinctive corpus sentence that ADR-0138 does not contain.
    seeded = "Ortiz is a Spanish brand sold in most French supermarkets"
    shared = _phrases(seeded) & corpus_phrases
    assert shared, "the seeded phrase is not actually in the corpus — the test is vacuous"
    assert shared - adr_phrases, "the seeded phrase is in the ADR, so it would be exempted"


def test_the_spec_exception_is_real_and_not_a_blanket() -> None:
    """The ADR exception must apply to something, and must not swallow everything.

    If it applied to nothing the exception would be dead code; if it applied to
    everything the guard would be decoration.
    """
    corpus_phrases = _phrases(probe_text())
    adr_phrases = _phrases(ADR_PATH.read_text(encoding="utf-8"))
    overlap = corpus_phrases & adr_phrases
    assert overlap, "no corpus phrasing comes from the ADR — the exception is dead"
    assert len(overlap) < len(corpus_phrases) / 2, (
        "most of the corpus is ADR text, so the exception would exempt nearly anything"
    )
