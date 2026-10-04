"""FRE-1514 AC-2: the brief scorer counts goals that introduce an entity absent from the user's message."""

from __future__ import annotations

import json
from pathlib import Path

from scripts.eval.fre1537 import briefs
from scripts.eval.fre1537.common import RunPaths

HEATPUMP = "Which heat pump should I install in a 1930s stone house in Brittany"


def test_stem_folds_plurals_and_final_e() -> None:
    assert briefs.stem("houses") == briefs.stem("house")
    assert briefs.stem("pumps") == briefs.stem("pump")
    assert briefs.stem("aid's") == briefs.stem("aid")
    assert briefs.stem("subsidies") == briefs.stem("subsidy")
    assert briefs.stem("glass") == "glass"


def test_terms_keep_digit_bearing_tokens_and_drop_stop_and_instruction_words() -> None:
    assert briefs.terms("Find the top 3 GPT-5 benchmarks for 1930s houses") == [
        "gpt",
        "benchmarks",
        "1930s",
        "houses",
    ]


def test_the_tickets_heatpump_briefs_each_introduce_an_entity() -> None:
    # FRE-1498 P7: the three briefs the owner read on 2026-09-13. Each one must count.
    for goal in (
        "heat pump types for poorly insulated buildings in maritime climates",
        "the thermal characteristics of 1930s Breton stone houses",
        "French financial aids and heritage constraints",
    ):
        assert briefs.novel_terms(goal, HEATPUMP), goal


def test_a_goal_built_from_the_question_introduces_nothing() -> None:
    goal = "Find 3 heat pumps to install in a 1930s stone house in Brittany and return each one"
    assert briefs.novel_terms(goal, HEATPUMP) == []


def test_prefix_match_needs_five_characters() -> None:
    # install / installation share a 7-character stem prefix: known.
    assert briefs.novel_terms("installation", "install") == []
    # data / database share only 4 characters: not known.
    assert briefs.novel_terms("database", "data") == ["database"]


def test_tasks_from_content_reads_full_goals_and_constraints() -> None:
    goal = "x" * 300
    content = json.dumps(
        {"strategy": "HYBRID", "tasks": [{"goal": goal, "constraints": ["a b", "cd"]}]}
    )
    assert briefs.tasks_from_content(content) == [briefs.Task(goal=goal, constraints=("a b", "cd"))]
    assert briefs.tasks_from_content('{"strategy": "SINGLE", "tasks": []}') == []
    assert briefs.tasks_from_content("not json") == []


def _row(label: str, trial: int, goals: list[str], kind: str = "single") -> dict[str, object]:
    tasks = [{"name": f"t{i}", "goal": g, "constraints": ["c"]} for i, g in enumerate(goals)]
    strategy = "HYBRID" if goals else "SINGLE"
    content = json.dumps({"strategy": strategy, "tasks": tasks})
    return {
        "label": label,
        "trial": trial,
        "kind": kind,
        "content": content,
        "plan": {"parse_error": False, "strategy": strategy, "task_count": len(goals)},
    }


def _write_arm(run_dir: Path, rows: list[dict[str, object]], system: str) -> RunPaths:
    paths = RunPaths(run_dir, "planner")
    paths.rows.mkdir(parents=True)
    paths.decide.write_text("".join(json.dumps(r) + "\n" for r in rows))
    paths.prompts.write_text(
        json.dumps(
            {
                "system": system,
                "prompt_hash": "h-" + system,
                "fixtures": {"heatpump": {"query": HEATPUMP}, "greeting": {"query": "Hi"}},
            }
        )
    )
    return paths


def test_arm_stats_count_goals_rate_tasks_and_length() -> None:
    rows = [
        _row("heatpump", 0, ["French financial aids", "heat pump for a stone house"]),
        _row("heatpump", 1, ["heat pump in Brittany"]),
        _row("greeting", 0, []),
    ]
    stats = briefs.arm_stats(rows, {"heatpump": HEATPUMP, "greeting": "Hi"})
    assert stats.expansions == 2
    assert stats.goals == 3
    assert stats.goals_with_entity == 1
    assert stats.tasks_per_expansion == 1.5
    assert stats.goal_chars_p50 == len("heat pump in Brittany")


def test_verdict_needs_both_count_and_rate_to_fall(tmp_path: Path) -> None:
    base = _write_arm(
        tmp_path / "base",
        [_row("heatpump", 0, ["French financial aids", "heritage constraints"])],
        "S",
    )
    rule = _write_arm(
        tmp_path / "rule", [_row("heatpump", 0, ["heat pump for a stone house"])], "S\n- rule"
    )
    report = briefs.compare(rule, base)
    assert report.passed
    text = report.render()
    assert "PASS" in text and "+ - rule" in text and briefs.scorer_sha256() in text


def test_a_count_that_does_not_fall_fails(tmp_path: Path) -> None:
    base = _write_arm(
        tmp_path / "base",
        [_row("heatpump", 0, ["French financial aids"]), _row("heatpump", 1, ["heat pump"])],
        "S",
    )
    rule = _write_arm(tmp_path / "rule", [_row("heatpump", 0, ["thermal mass of granite"])], "S2")
    report = briefs.compare(rule, base)
    assert not report.passed  # count 1 -> 1 does not fall


def test_rate_rise_fails_even_when_count_falls(tmp_path: Path) -> None:
    base = _write_arm(
        tmp_path / "base",
        [
            _row("heatpump", 0, ["French financial aids", "heat pump"]),
            _row("heatpump", 1, ["heritage constraints", "stone house"]),
        ],
        "S",
    )
    rule = _write_arm(tmp_path / "rule", [_row("heatpump", 0, ["thermal mass of granite"])], "S2")
    report = briefs.compare(rule, base)
    assert not report.passed  # count 2 -> 1 falls, rate 50% -> 100% rises
