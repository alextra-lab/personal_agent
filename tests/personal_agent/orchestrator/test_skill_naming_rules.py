"""ADR-0156 A3 (FRE-1596 AC-1, AC-2): names state an objective, and "Not for…" lines name real things.

A3.1: a skill name or a tool name never holds a backend word (``elasticsearch``, ``es``, ``tempo``,
``neo4j``, ``postgres``, ``redis``) or the word ``telemetry``. The validator splits a name on ``-``
and ``_``.

A3.2: each description has a "Not for…" line that names the neighbouring skill or tool by its exact
name, in backticks. The validator checks that the line exists and that each name it holds is a
skill or a tool that exists.

The seeded cases below fail on purpose. The real-inventory cases live in
``tests/personal_agent/orchestrator/test_telemetry_tools_visibility.py`` and in
``tests/docs/test_skill_validator.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from personal_agent.orchestrator.skills import (
    BACKEND_WORDS,
    SkillRule,
    name_violations,
    not_for_violations,
    validate_skill,
)

# ── AC-1: no backend word in a name ──────────────────────────────────────────────


# The two seeded names of AC-1. The old tool name is built from two parts, so the AC-5 guard
# (test_old_tool_name_guard.py) does not read this file as a live use of it.
_OLD_SKILL = "query-elasticsearch"
_OLD_TOOL = "query_" + "telemetry"


def test_the_seeded_old_names_fail() -> None:
    found = name_violations([_OLD_SKILL, _OLD_TOOL])
    assert [m.split(":")[0] for m in found] == [_OLD_SKILL, _OLD_TOOL]


@pytest.mark.parametrize("word", sorted(BACKEND_WORDS))
def test_each_backend_word_fails_in_either_separator(word: str) -> None:
    assert name_violations([f"read-{word}"]) and name_violations([f"read_{word}"])
    assert name_violations([f"{word}-direct"]) and name_violations([f"{word}_read"])


def test_the_word_list_is_the_adr_list() -> None:
    assert BACKEND_WORDS == frozenset(
        {"elasticsearch", "es", "tempo", "neo4j", "postgres", "redis", "telemetry"}
    )


@pytest.mark.parametrize(
    "name",
    [
        "log-search",
        "model-latency",
        "turn-explanation",
        "spend-and-tokens",
        "interaction-outcomes",
        "service-health",
        "graph-inspection",
        "mermaid-diagrams",
        "search_logs",
        "measure_model_latency",
        "recall_personal_history",
        # a part must EQUAL a backend word: a longer word that holds one is a different word
        "tempos-guide",
        "redistribute",
        "yes-or-no",
    ],
)
def test_a_real_name_passes(name: str) -> None:
    assert name_violations([name]) == ()


# ── AC-2: each "Not for…" line names something real ──────────────────────────────

_KNOWN = {"log-search", "model-latency", "search_logs", "measure_model_latency"}


def test_a_line_that_names_real_things_passes() -> None:
    descriptions = {
        "log-search": "Counts events. Not for timing. The `model-latency` skill gives that.",
        "search_logs": "Reads logs.\nNot for timing. `measure_model_latency` gives that.",
    }
    assert not_for_violations(descriptions, _KNOWN) == ()


def test_a_line_that_names_a_missing_skill_fails() -> None:
    descriptions = {"log-search": "Counts events. Not for timing. The `latency-wizard` skill does."}
    found = not_for_violations(descriptions, _KNOWN)
    assert found == ("log-search: its 'Not for' line names 'latency-wizard', which does not exist",)


def test_a_description_with_no_line_fails() -> None:
    found = not_for_violations({"log-search": "Counts events."}, _KNOWN)
    assert found == ("log-search: description has no 'Not for' line",)


def test_a_line_that_names_nothing_fails() -> None:
    found = not_for_violations({"log-search": "Counts events. Not for timing."}, _KNOWN)
    assert found == ("log-search: its 'Not for' line names no skill or tool in backticks",)


def test_a_name_without_backticks_does_not_count() -> None:
    found = not_for_violations({"log-search": "Not for timing. model-latency does that."}, _KNOWN)
    assert found == ("log-search: its 'Not for' line names no skill or tool in backticks",)


def test_a_hyphenated_word_is_not_read_as_a_name() -> None:
    descriptions = {"log-search": "Not for model-call timing. The `model-latency` skill gives it."}
    assert not_for_violations(descriptions, _KNOWN) == ()


# ── the per-folder rules of the validator ────────────────────────────────────────


def _skill(tmp_path: Path, name: str, description: str) -> Path:
    folder = tmp_path / name
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\nDo the task.\n", encoding="utf-8"
    )
    return folder


def _rules(folder: Path) -> set[SkillRule]:
    return {v.rule for v in validate_skill(folder, "agent")}


def test_a_folder_with_a_backend_word_fails_the_name_words_rule(tmp_path: Path) -> None:
    folder = _skill(tmp_path, "query-tempo", "Reads spans. Not for logs. `log-search` does that.")
    assert "name_words" in _rules(folder)


def test_a_folder_with_no_not_for_line_fails_the_not_for_rule(tmp_path: Path) -> None:
    folder = _skill(tmp_path, "model-latency", "Reads latency.")
    assert "not_for" in _rules(folder)


def test_a_folder_with_a_good_name_and_line_passes_both_rules(tmp_path: Path) -> None:
    folder = _skill(
        tmp_path, "model-latency", "Reads latency. Not for logs. The `log-search` skill does."
    )
    assert not _rules(folder) & {"name_words", "not_for"}
