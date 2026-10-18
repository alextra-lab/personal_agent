"""FRE-1615 Block 1: offline tests of the runner, the arm patches and the memory seed data."""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest
from scripts.eval.fre1615 import run
from scripts.eval.fre1615.capture import load_suite

HERE = Path(run.__file__).resolve().parent
ARMS = HERE / "arms"


def test_parse_arms_maps_names_to_gateways() -> None:
    """Each arm name maps to one gateway, in the given order."""
    assert run.parse_arms("S=control,PL=treatment") == (("S", "control"), ("PL", "treatment"))
    assert run.parse_arms("P2=treatment") == (("P2", "treatment"),)


@pytest.mark.parametrize(
    "text", ["", "S", "S=nowhere", "S=control,S=treatment", "S=control,PL=control"]
)
def test_parse_arms_refuses_bad_input(text: str) -> None:
    """A malformed pair, an unknown gateway, or a repeat is refused."""
    with pytest.raises(ValueError):
        run.parse_arms(text)


def test_select_tasks_keeps_suite_order_and_refuses_unknown_ids() -> None:
    """``--ids`` keeps suite order; an unknown id is refused."""
    tasks = load_suite()
    assert [t["id"] for t in run.select_tasks(tasks, ["rs1", "dq1"], 0)] == ["dq1", "rs1"]
    assert len(run.select_tasks(tasks, [], 2)) == 2
    with pytest.raises(ValueError):
        run.select_tasks(tasks, ["zz9"], 0)


def test_tempo_hex_drops_the_dashes() -> None:
    """The Tempo trace id is the agent trace UUID without dashes."""
    assert (
        run.tempo_hex("2F36D026-6686-2825-35ff-db3fe2acb172") == "2f36d0266686282535ffdb3fe2acb172"
    )


def _span(start: str, **attrs: str) -> dict[str, object]:
    return {
        "startTimeUnixNano": start,
        "attributes": [{"key": k, "value": {"intValue": v}} for k, v in attrs.items()],
    }


def test_slm_calls_reads_only_slm_server_spans_in_start_order() -> None:
    """Only ``slm-server`` spans with ``slm.prompt_n`` count, sorted by start time."""
    trace = {
        "batches": [
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "seshat-vps"}}]
                },
                "scopeSpans": [{"spans": [_span("1", **{"slm.prompt_n": "999"})]}],
            },
            {
                "resource": {
                    "attributes": [{"key": "service.name", "value": {"stringValue": "slm-server"}}]
                },
                "scopeSpans": [
                    {
                        "spans": [
                            _span("20", **{"slm.prompt_n": "62", "slm.decode_ms": "935.2"}),
                            _span("10", **{"slm.prompt_n": "731", "slm.cache_reuse": "778"}),
                            _span("30"),
                        ]
                    }
                ],
            },
        ]
    }
    calls = run.slm_calls(trace)
    assert [c["slm.prompt_n"] for c in calls] == [731.0, 62.0]
    assert calls[0]["slm.cache_reuse"] == 778.0
    assert calls[1]["slm.decode_ms"] == 935.2


def test_summarize_counts_the_last_good_row_and_lists_expanded_tasks() -> None:
    """Per arm: the last row without an error counts, errors are counted, expanded tasks listed."""
    rows = [
        {"id": "rs1", "arm": "PL", "error": "boom"},
        {
            "id": "rs1",
            "arm": "PL",
            "wall_s": 100.0,
            "model_calls": 6,
            "planner_decision": ["expanded"],
            "stage5": {"reason": "planner_asked"},
            "slm_calls": [{"slm.prompt_n": 1000, "slm.prefill_ms": 2000, "slm.decode_ms": 4000}],
        },
        {
            "id": "dq1",
            "arm": "PL",
            "wall_s": 20.0,
            "model_calls": 2,
            "planner_decision": ["declined"],
            "stage5": {"reason": "planner_asked"},
            "slm_calls": [],
        },
        {
            "id": "rs1",
            "arm": "S",
            "wall_s": 30.0,
            "model_calls": 2,
            "stage5": {"reason": "expansion_disabled"},
        },
    ]
    s = run.summarize(rows)
    pl = s["PL"]
    assert pl["tasks"] == 2 and pl["error_rows"] == 1
    assert pl["expanded"] == ["rs1"]
    assert pl["prompt_n"] == 1000 and pl["prefill_s"] == 2.0 and pl["decode_s"] == 4.0
    assert pl["planner_decisions"] == {"expanded": 1, "declined": 1}
    assert s["S"]["stage5_reasons"] == {"expansion_disabled": 1}


def test_p2_patch_carries_the_owner_text_verbatim() -> None:
    """The P2 patch adds the owner's pasted policy line for line, nothing reworded."""
    owner_lines = (ARMS / "P2_prefer_single.txt").read_text().splitlines()
    patch = (ARMS / "P2.patch").read_text()
    added = [m.group(1) for m in re.finditer(r'^\+    "(.*)\\n"$', patch, flags=re.M)]
    assert added == owner_lines


def test_p1_patch_gates_conversational_simple_and_follow_up() -> None:
    """The P1 patch routes a conversational, simple or follow-up turn to SINGLE before the planner."""
    patch = (ARMS / "P1.patch").read_text()
    assert (
        "task_type == TaskType.CONVERSATIONAL or complexity == Complexity.SIMPLE or follow_up"
        in patch
    )
    assert 'return DecompositionStrategy.SINGLE, "p1_pregate_single"' in patch


def test_memory_seed_writes_six_entities_in_three_turns() -> None:
    """The seed data holds 3 turns and 6 entities, the counts the runner checks."""
    tree = ast.parse((HERE / "memory_seed.py").read_text())
    facts = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "FACTS"
    )
    value = ast.literal_eval(facts)
    assert len(value) == 3
    assert sum(len(f[3]) for f in value) == 6


def test_search_calls_dedupes_spans_across_span_sets() -> None:
    """A span listed in two span sets of a search reply counts once."""
    span = {"spanID": "a", **_span("5", **{"slm.prompt_n": "10"})}
    other = {"spanID": "b", **_span("3", **{"slm.prompt_n": "20"})}
    reply = {"traces": [{"spanSets": [{"spans": [span]}, {"spans": [span, other]}]}]}
    assert [c["slm.prompt_n"] for c in run.search_calls(reply)] == [20.0, 10.0]


def test_every_suite_task_has_an_expect_line() -> None:
    """The judges score each task against its expect line; every task has exactly one."""
    import yaml

    expects = yaml.safe_load((HERE / "expects.yaml").read_text())["expects"]
    assert set(expects) == {str(t["id"]) for t in load_suite()}
    assert all(isinstance(v, str) and v.strip() for v in expects.values())
