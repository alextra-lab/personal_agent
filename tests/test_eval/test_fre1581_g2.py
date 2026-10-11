"""FRE-1581 / ADR-0156 AC-2: the G2 items and the G2 scorer, offline."""

from __future__ import annotations

import yaml
from scripts.eval.fre1581 import g2_skill_choice as g2


def row(id_: str, kind: str, expected: str | None, loaded: list[str], **extra: object) -> dict:
    return {"id": id_, "kind": kind, "expected_skill": expected, "skills_loaded": loaded, **extra}


def passing_rows(scenarios: int = 33) -> list[dict]:
    rows = [row(f"s#{i}", "scenario", "query-tempo", ["query-tempo"]) for i in range(scenarios)]
    return rows + [row(f"ns{i:02d}", "no_skill", None, []) for i in range(1, 21)]


def test_the_items_cover_every_eval_scenario_and_20_no_skill_prompts() -> None:
    items = g2.load_items()
    scenarios = [i for i in items if i.kind == "scenario"]
    assert len(scenarios) >= g2.MIN_SCENARIOS
    assert all(i.expected_skill for i in scenarios)
    assert len({i.id for i in items}) == len(items)
    assert sum(i.kind == "no_skill" for i in items) == 20


def test_at_least_10_no_skill_prompts_hold_a_former_keyword() -> None:
    prompts = yaml.safe_load(g2.NO_SKILL_PROMPTS.read_text())["prompts"]
    marked = [p for p in prompts if p.get("former_keyword")]
    assert len(marked) >= 10
    for p in marked:
        assert p["former_keyword"]["word"] in p["prompt"].lower()


def test_a_scenario_is_right_only_with_its_skill_and_no_other() -> None:
    assert g2.is_right("scenario", "query-tempo", ["query-tempo"])
    assert g2.is_right(
        "scenario", "query-tempo", ["query-tempo", "query-tempo"]
    )  # a reference read
    assert not g2.is_right("scenario", "query-tempo", ["query-tempo", "self-telemetry"])
    assert not g2.is_right("scenario", "query-tempo", [])
    assert g2.is_right("no_skill", None, [])
    assert not g2.is_right("no_skill", None, ["mermaid-diagrams"])


def test_a_clean_run_passes() -> None:
    assert g2.score(passing_rows()).passed


def test_fewer_than_33_scenarios_fail() -> None:
    result = g2.score(passing_rows(32))
    assert not result.passed and result.scenarios_run == 32


def test_the_90_percent_bound() -> None:
    rows = passing_rows(40)
    for r in rows[:4]:
        r["skills_loaded"] = []
    assert g2.score(rows).passed  # 36/40 = 90%
    rows[4]["skills_loaded"] = ["self-telemetry"]
    assert not g2.score(rows).passed  # 35/40


def test_three_no_skill_loads_fail() -> None:
    rows = passing_rows()
    no_skill = [r for r in rows if r["kind"] == "no_skill"]
    for r in no_skill[:2]:
        r["skills_loaded"] = ["self-telemetry"]
    assert g2.score(rows).passed
    no_skill[2]["skills_loaded"] = ["query-tempo"]
    result = g2.score(rows)
    assert not result.passed and result.no_skill_loaded == 3


def test_an_error_row_is_not_scored_and_a_later_row_replaces_it() -> None:
    rows = passing_rows()
    rows.append({"id": "s#0", "kind": "scenario", "error": "ReadTimeout"})
    assert g2.score(rows).scenarios_run == 33
    short = [r for r in passing_rows() if r["id"] != "ns01"]
    assert not g2.score(short).passed  # 19 no-skill prompts ran


def test_the_report_names_each_wrong_turn() -> None:
    rows = passing_rows()
    rows[0]["skills_loaded"] = ["mermaid-diagrams"]
    text = g2.score(rows).render()
    assert "s#0 loaded ['mermaid-diagrams']" in text and "32/33" in text
