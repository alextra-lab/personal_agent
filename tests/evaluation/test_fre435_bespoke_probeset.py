"""FRE-489 — bespoke live-corpus gate set validation (pure).

Encodes the ADR-0087 §D2 acceptance for the committed gate set: it must load
through the FRE-488 harness schema, hit the agreed size (N≈20), carry the three
pedagogical-shaped sub-types (§D6), include real false-negative failures and
true-negative abstention controls, and — because the repo is public — contain no
PII / injected-email tokens.
"""

from __future__ import annotations

from pathlib import Path

from scripts.eval.fre435_memory_recall.probes import load_probe_set

GATE_PATH = Path("scripts/eval/fre435_memory_recall/bespoke_probe.yaml")

#: N agreed with the owner (2026-06-26).
MIN_CASES = 20

#: The three pedagogical sub-types the bar must reflect (ADR-0087 §D6 / ADR-0084).
PEDAGOGICAL_SUBTYPES = {"active-recall-due", "thread-branch", "cross-domain"}

#: Tokens that would indicate leaked private content in a public repo. Kept lower-case;
#: matched case-insensitively against every authored string in the set.
PII_DENYLIST = {
    "alex",
    "kookier",
    "icloud.com",
    "@",
    "cf-access",
    "starry-plaza",
}


def _all_strings(case: object) -> list[str]:
    """Collect every authored free-text string from a parsed case."""
    from scripts.eval.fre435_memory_recall.probes import ProbeCase

    assert isinstance(case, ProbeCase)
    out = [case.case_id, case.query, case.note, *case.tags]
    out += [e.name for e in case.seed_entities]
    out += [e.description for e in case.seed_entities]
    out += [e.entity_type for e in case.seed_entities]
    for turn in case.history:
        out += [turn.user, turn.assistant]
    out += list(case.expected.entity_names)
    return out


def test_gate_set_loads_and_meets_size() -> None:
    """Gate set loads through the harness schema and meets N."""
    cases = load_probe_set(GATE_PATH)
    assert len(cases) >= MIN_CASES
    # Unique case ids.
    ids = [c.case_id for c in cases]
    assert len(set(ids)) == len(ids)


def test_pedagogical_subtypes_present() -> None:
    """All three §D6 pedagogical sub-types are represented (>=3 cases)."""
    cases = load_probe_set(GATE_PATH)
    pedagogical = [c for c in cases if "pedagogical" in c.tags]
    assert len(pedagogical) >= 3
    seen_subtypes = {t for c in pedagogical for t in c.tags} & PEDAGOGICAL_SUBTYPES
    assert seen_subtypes == PEDAGOGICAL_SUBTYPES


def test_includes_false_negative_failures() -> None:
    """Includes real 'no prior discussions' false-negative cases (must_not_deny)."""
    cases = load_probe_set(GATE_PATH)
    fn = [
        c
        for c in cases
        if "false-negative" in c.tags and c.relevant_ids and c.expected.must_not_deny
    ]
    assert len(fn) >= 3


def test_includes_abstention_controls() -> None:
    """Includes true-negative controls (nothing relevant; denial acceptable)."""
    cases = load_probe_set(GATE_PATH)
    controls = [
        c
        for c in cases
        if "control" in c.tags and not c.relevant_ids and not c.expected.must_not_deny
    ]
    assert len(controls) >= 2


def test_every_relevant_case_is_replay_loadable() -> None:
    """Every case with an expected recall seeds entities (replay-mode runnable)."""
    cases = load_probe_set(GATE_PATH)
    for case in cases:
        if case.relevant_ids:
            assert case.seed_entities, f"{case.case_id} has expected recall but no seed_entities"


def test_expected_entities_are_seeded() -> None:
    """Each expected entity name is actually seeded in its case (replay can hit it)."""
    cases = load_probe_set(GATE_PATH)
    for case in cases:
        seeded = {e.name.strip().lower() for e in case.seed_entities}
        for name in case.expected.entity_names:
            assert name.strip().lower() in seeded, (
                f"{case.case_id}: expected '{name}' is not seeded"
            )


def test_no_pii_tokens_anywhere() -> None:
    """No PII / injected-email tokens leak into the public gate set."""
    cases = load_probe_set(GATE_PATH)
    offenders: list[str] = []
    for case in cases:
        for text in _all_strings(case):
            low = text.lower()
            for token in PII_DENYLIST:
                if token in low:
                    offenders.append(f"{case.case_id}: '{token}' in {text!r}")
    assert not offenders, "PII tokens found:\n" + "\n".join(offenders)


def test_queries_are_referential() -> None:
    """A query must not name its expected entity (codex review — gate discrimination).

    Naming the target makes the case pass on a substring match (and trivially under
    keyword-only recall), so it cannot discriminate a real semantic-recall failure.
    """
    cases = load_probe_set(GATE_PATH)
    offenders: list[str] = []
    for case in cases:
        query_low = case.query.lower()
        for name in case.expected.entity_names:
            if name.strip().lower() in query_low:
                offenders.append(f"{case.case_id}: query names expected '{name}'")
    assert not offenders, "Non-referential queries:\n" + "\n".join(offenders)


def test_domain_diversity() -> None:
    """The set spans multiple knowledge domains (cross-domain bar)."""
    cases = load_probe_set(GATE_PATH)
    domains = {
        t
        for c in cases
        for t in c.tags
        if t
        in {
            "physics",
            "game-theory",
            "cosmology",
            "cooking",
            "history",
            "agent-arch",
            "neuroscience",
        }
    }
    assert len(domains) >= 4
