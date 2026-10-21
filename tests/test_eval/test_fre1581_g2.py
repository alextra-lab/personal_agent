"""FRE-1581 / ADR-0156 AC-2: the G2 items and the G2 scorer, offline."""

from __future__ import annotations

import pytest
import yaml
from scripts.eval.fre1581 import g2_skill_choice as g2


def row(id_: str, kind: str, expected: str | None, loaded: list[str], **extra: object) -> dict:
    return {"id": id_, "kind": kind, "expected_skill": expected, "skills_loaded": loaded, **extra}


def passing_rows(scenarios: int = 33) -> list[dict]:
    rows = [row(f"s#{i}", "scenario", "model-latency", ["model-latency"]) for i in range(scenarios)]
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
    assert g2.is_right("scenario", "model-latency", ["model-latency"])
    assert g2.is_right(
        "scenario", "model-latency", ["model-latency", "model-latency"]
    )  # a reference read
    assert not g2.is_right("scenario", "model-latency", ["model-latency", "spend-and-tokens"])
    assert not g2.is_right("scenario", "model-latency", [])
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
    rows[4]["skills_loaded"] = ["spend-and-tokens"]
    assert not g2.score(rows).passed  # 35/40


def test_three_no_skill_loads_fail() -> None:
    rows = passing_rows()
    no_skill = [r for r in rows if r["kind"] == "no_skill"]
    for r in no_skill[:2]:
        r["skills_loaded"] = ["spend-and-tokens"]
    assert g2.score(rows).passed
    no_skill[2]["skills_loaded"] = ["model-latency"]
    result = g2.score(rows)
    assert not result.passed and result.no_skill_loaded == 3


def test_an_error_row_is_not_scored_and_a_later_row_replaces_it() -> None:
    rows = passing_rows()
    rows.append({"id": "s#0", "kind": "scenario", "error": "ReadTimeout"})
    assert g2.score(rows).scenarios_run == 33
    assert g2.score(rows).errored == []
    only_error = [*passing_rows(), {"id": "x#9", "kind": "scenario", "error": "ReadTimeout"}]
    result = g2.score(only_error)
    assert result.errored == ["x#9"] and "errored, not scored: x#9" in result.render()
    short = [r for r in passing_rows() if r["id"] != "ns01"]
    assert not g2.score(short).passed  # 19 no-skill prompts ran


def test_the_report_names_each_wrong_turn() -> None:
    rows = passing_rows()
    rows[0]["skills_loaded"] = ["mermaid-diagrams"]
    text = g2.score(rows).render()
    assert "s#0 loaded ['mermaid-diagrams']" in text and "32/33" in text


class _FakeES:
    """Answer ``agent-logs-*`` searches the way production-mapped ES does.

    A ``term`` on a ``.keyword`` sub-field matches nothing, because the fields are ``keyword``.
    """

    def __init__(self, events: list[dict]) -> None:
        self.events = events

    def post(self, url: str, json: dict, timeout: float) -> object:
        hits = []
        if "agent-logs-" in url:
            terms = [f["term"] for f in json["query"]["bool"]["filter"]]
            hits = [
                e for e in self.events if all(e.get(k) == v for t in terms for k, v in t.items())
            ]
        body = {"hits": {"hits": [{"_source": h} for h in hits]}}
        return type("R", (), {"raise_for_status": lambda self: None, "json": lambda self: body})()


def test_the_turn_query_uses_the_production_field_shape() -> None:
    """FRE-1581 review: eval ES has the production templates, so no `.keyword` sub-field."""
    terms = g2.turn_events_query("t1", "read_skill_invoked")["query"]["bool"]["filter"]
    assert terms == [{"term": {"trace_id": "t1"}}, {"term": {"event_type": "read_skill_invoked"}}]


def test_read_turn_reads_a_skill_load_on_a_production_mapped_index() -> None:
    es = _FakeES(
        [
            {
                "trace_id": "t1",
                "event_type": "model_call_completed",
                "role": "primary",
                "model": "m",
            },
            {"trace_id": "t1", "event_type": "read_skill_invoked", "skill_name": "model-latency"},
            {
                "trace_id": "t2",
                "event_type": "read_skill_invoked",
                "skill_name": "graph-inspection",
            },
        ]
    )
    turn = g2.read_turn(es, "t1", settle_s=0)  # type: ignore[arg-type]
    assert turn["skills_loaded"] == ["model-latency"] and turn["primary_models"] == ["m"]


def test_a_turn_with_no_model_call_is_an_error_not_a_no_skill_row() -> None:
    """Seeded negative: a query that matches nothing must not score as "no skill loaded"."""
    es = _FakeES([{"trace_id": "t1", "event_type": "read_skill_invoked", "skill_name": "x"}])
    with pytest.raises(ValueError, match="no model_call_completed"):
        g2.read_turn(es, "t1", settle_s=0)  # type: ignore[arg-type]
