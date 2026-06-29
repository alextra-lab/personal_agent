"""FRE-670 — vocabulary-divergent (semantic) recall probe — gate-set validation (pure).

Encodes the acceptance for the committed semantic probe split (`semantic_probe.yaml`):
it must load through the FRE-488 harness schema, hit the owner-authored size (54
cases: ≥44 positives across 12 themes, ≥10 controls, ≥7 natural/imagery
register-pairs), and — because the repo is public — carry no PII.

The instrument-defining tests are the two disciplines that make the split genuinely
semantic rather than lexical-masked-as-semantic (the FRE-489 failure mode, FRE-656):

* **referential** — a query never names its own expected entity (shared with the
  FRE-489 gate; a substring cheat would let keyword-only recall pass);
* **vocabulary-divergent** — for every *imagery*-register positive, the query shares
  near-zero content vocabulary with the stored note text (description + history),
  measured with the keyword baseline's own tokenizer plus light suffix-stemming so
  morphology (plurals / tenses) cannot smuggle overlap past the guard.

The guard is a *floor, not a proof*: it cannot detect a dishonestly-rewritten note
that preserves semantic answerability while dropping the query's words. Note
faithfulness is protected out-of-band by the gitignored working file
(`telemetry/archive/fre670_probe_queries.md`) being the auditable provenance record
(codex plan-review, 2026-06-29).
"""

from __future__ import annotations

import re
from pathlib import Path

from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set

GATE_PATH = Path("scripts/eval/fre435_memory_recall/semantic_probe.yaml")

#: Owner-authored size (Linear handoff 2026-06-29): 44 positives + 10 controls.
EXPECTED_CASES = 54
MIN_POSITIVES = 44
MIN_CONTROLS = 10
MIN_REGISTER_PAIRS = 7
EXPECTED_THEMES = 12

#: Max content-token Jaccard overlap allowed between an imagery query and its stored
#: note text. Imagery queries are deliberately oblique, so honest authorship lands
#: well below this; the bound is the structural guarantee that BM25 cannot ride
#: surface overlap to a hit (the AC2 instrument check).
MAX_IMAGERY_JACCARD = 0.15

#: PII / injected-identifier tokens that must never leak into the public gate set.
#: Extends the FRE-489 denylist with the real personal names/places the working
#: file paraphrases away (matched case-insensitively against every authored string).
PII_DENYLIST = {
    "alex",
    "kookier",
    "icloud.com",
    "@",
    "cf-access",
    "starry-plaza",
    # FRE-670 working-file PII (locations / names paraphrased out of the committed set).
    "mane",
    "forcalquier",
    "manosque",
    "marseille",
    "florian",
    "theo",
    "susan",
    "lyon",
}

#: Query-side stopwords — mirror the keyword baseline so the divergence test measures
#: the same content tokens BM25 ranks on.
_STOP = set(
    "the a an of to in on and or is are was were be been being it its this that "
    "what left off pick up we core idea i m my you your for with how do does did "
    "lets let s about can could would there here some more most".split()
)


def _stem(token: str) -> str:
    """Strip a few common English suffixes so plurals/tenses don't smuggle overlap."""
    for suffix in ("ing", "edly", "ed", "es", "ly", "s"):
        if len(token) > len(suffix) + 2 and token.endswith(suffix):
            return token[: -len(suffix)]
    return token


def _content_tokens(text: str | None) -> set[str]:
    """Stemmed lowercase content tokens (>2 chars, non-stopword) of ``text``."""
    return {
        _stem(t)
        for t in re.findall(r"[a-z0-9]+", (text or "").lower())
        if t not in _STOP and len(t) > 2
    }


def _note_text(case: ProbeCase) -> str:
    """All stored note text a keyword search would rank on (description + history)."""
    parts = [e.description for e in case.seed_entities]
    parts += [e.name for e in case.seed_entities]
    for turn in case.history:
        parts += [turn.user, turn.assistant]
    return " ".join(p for p in parts if p)


def _jaccard(a: set[str], b: set[str]) -> float:
    """Jaccard overlap of two token sets (0.0 when either is empty)."""
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _all_strings(case: ProbeCase) -> list[str]:
    """Collect every authored free-text string from a parsed case."""
    out = [case.case_id, case.query, case.note, *case.tags]
    out += [e.name for e in case.seed_entities]
    out += [e.description for e in case.seed_entities]
    out += [e.entity_type for e in case.seed_entities]
    for turn in case.history:
        out += [turn.user, turn.assistant]
    out += list(case.expected.entity_names)
    return out


def _tag_value(case: ProbeCase, prefix: str) -> str | None:
    """Return the value of a ``prefix:value`` tag (first match), or ``None``."""
    for tag in case.tags:
        if tag.startswith(prefix):
            return tag[len(prefix) :]
    return None


def _positives(cases: list[ProbeCase]) -> list[ProbeCase]:
    return [c for c in cases if "type:positive" in c.tags]


def _controls(cases: list[ProbeCase]) -> list[ProbeCase]:
    return [c for c in cases if "type:control" in c.tags]


def test_gate_set_loads_and_meets_size() -> None:
    """Loads through the harness schema, hits 54 cases with unique ids."""
    cases = load_probe_set(GATE_PATH)
    assert len(cases) == EXPECTED_CASES
    ids = [c.case_id for c in cases]
    assert len(set(ids)) == len(ids)


def test_positive_and_control_counts() -> None:
    """≥44 positives and ≥10 controls; every case is exactly one of the two."""
    cases = load_probe_set(GATE_PATH)
    positives, controls = _positives(cases), _controls(cases)
    assert len(positives) >= MIN_POSITIVES
    assert len(controls) >= MIN_CONTROLS
    # Partition: no case is both / neither.
    for c in cases:
        assert ("type:positive" in c.tags) ^ ("type:control" in c.tags), (
            f"{c.case_id} must be tagged exactly one of type:positive / type:control"
        )


def test_twelve_themes_present() -> None:
    """All 12 real corpus themes are represented (theme:<n> tags)."""
    cases = load_probe_set(GATE_PATH)
    themes = {_tag_value(c, "theme:") for c in cases} - {None}
    assert len(themes) >= EXPECTED_THEMES, f"only {len(themes)} themes: {sorted(themes)}"


def test_register_pairs_present() -> None:
    """≥7 register-pairs: a pair:<id> shared by a natural and an imagery case."""
    cases = load_probe_set(GATE_PATH)
    pairs: dict[str, set[str]] = {}
    for c in cases:
        pid = _tag_value(c, "pair:")
        reg = _tag_value(c, "register:")
        if pid and reg:
            pairs.setdefault(pid, set()).add(reg)
    complete = [pid for pid, regs in pairs.items() if {"natural", "imagery"} <= regs]
    assert len(complete) >= MIN_REGISTER_PAIRS, (
        f"only {len(complete)} complete natural/imagery pairs: {sorted(complete)}"
    )


def test_every_positive_has_register_tag() -> None:
    """Every positive carries a register tag (the register-delta measurement keys on it)."""
    for c in _positives(load_probe_set(GATE_PATH)):
        assert _tag_value(c, "register:") in {"natural", "imagery"}, (
            f"{c.case_id} positive missing register:natural|imagery"
        )


def test_positives_are_seeded_and_labelled() -> None:
    """Every positive seeds entities and each expected entity is among them."""
    for c in _positives(load_probe_set(GATE_PATH)):
        assert c.relevant_ids, f"{c.case_id} positive has no expected recall label"
        assert c.seed_entities, f"{c.case_id} positive has no seed_entities (not replay-loadable)"
        seeded = {e.name.strip().lower() for e in c.seed_entities}
        for name in c.expected.entity_names:
            assert name.strip().lower() in seeded, f"{c.case_id}: expected '{name}' not seeded"


def test_controls_abstain() -> None:
    """Controls carry no expected recall and must_not_deny is false (abstention is correct)."""
    for c in _controls(load_probe_set(GATE_PATH)):
        assert not c.relevant_ids, f"{c.case_id} control must have no expected recall"
        assert c.expected.must_not_deny is False, (
            f"{c.case_id} control must set must_not_deny: false"
        )


def test_compound_cases_have_multiple_targets() -> None:
    """A `compound` case carries ≥2 expected entity names (primary + supporting)."""
    cases = load_probe_set(GATE_PATH)
    compound = [c for c in cases if "compound" in c.tags]
    assert compound, "expected at least one compound case"
    for c in compound:
        assert len(c.expected.entity_names) >= 2, (
            f"{c.case_id} tagged compound but has <2 expected entities"
        )


def _pii_hit(token: str, text_low: str) -> bool:
    """Match a denylist token: word-boundary for bare words, substring otherwise.

    Word-boundary matching for alphabetic name/place tokens (``theo``, ``mane``)
    avoids false positives inside ordinary words (``theory``, ``manner``); tokens
    carrying punctuation (``@``, ``icloud.com``, ``cf-access``) match as substrings.
    """
    if token.isalpha():
        return re.search(rf"\b{re.escape(token)}\b", text_low) is not None
    return token in text_low


def test_no_pii_tokens_anywhere() -> None:
    """No PII / injected-identifier tokens leak into the public gate set."""
    offenders: list[str] = []
    for case in load_probe_set(GATE_PATH):
        for text in _all_strings(case):
            low = text.lower()
            for token in PII_DENYLIST:
                if _pii_hit(token, low):
                    offenders.append(f"{case.case_id}: '{token}' in {text!r}")
    assert not offenders, "PII tokens found:\n" + "\n".join(offenders)


def test_queries_are_referential() -> None:
    """A query must not name its own expected entity (gate discrimination, FRE-489)."""
    offenders: list[str] = []
    for case in load_probe_set(GATE_PATH):
        query_low = case.query.lower()
        for name in case.expected.entity_names:
            if name.strip().lower() in query_low:
                offenders.append(f"{case.case_id}: query names expected '{name}'")
    assert not offenders, "Non-referential queries:\n" + "\n".join(offenders)


def test_imagery_positives_are_vocabulary_divergent() -> None:
    """Imagery-register positives share near-zero content vocab with their note text.

    This is the AC2 instrument check: if an imagery query lexically overlaps its
    stored note, a BM25 keyword search could ride that overlap to a hit, and the
    split would be lexical (the FRE-489 / FRE-656 failure) rather than semantic.
    Natural-register positives are deliberately everyday phrasing and are exempt —
    the natural-vs-imagery register delta is what *measures* their easier lexical
    overlap, so gating them would erase the signal.
    """
    offenders: list[str] = []
    for case in _positives(load_probe_set(GATE_PATH)):
        if _tag_value(case, "register:") != "imagery":
            continue
        overlap = _jaccard(_content_tokens(case.query), _content_tokens(_note_text(case)))
        if overlap >= MAX_IMAGERY_JACCARD:
            offenders.append(
                f"{case.case_id}: query↔note Jaccard {overlap:.2f} ≥ {MAX_IMAGERY_JACCARD}"
            )
    assert not offenders, "Imagery queries not vocabulary-divergent:\n" + "\n".join(offenders)
