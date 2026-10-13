"""FRE-1598 benefit run (scripts/eval/fre1598), offline: fakes only, no network, no database."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import pytest
from scripts.eval.fre1590 import help as part2
from scripts.eval.fre1598 import judge, run, seed

# ── the scenario list ────────────────────────────────────────────────────────

AREA_SKILLS = {
    "graph-inspection",
    "interaction-outcomes",
    "log-search",
    "model-latency",
    "service-health",
    "spend-and-tokens",
    "turn-explanation",
}


def test_the_list_resolves_and_covers_each_area_skill_once_or_twice() -> None:
    scenarios = part2.load_scenarios(run.SCENARIOS_FILE)
    per_skill: dict[str, int] = {}
    for s in scenarios:
        per_skill[s.skill] = per_skill.get(s.skill, 0) + 1
        assert s.prompt and s.expect and s.reason
    assert set(per_skill) == AREA_SKILLS
    assert all(1 <= n <= 2 for n in per_skill.values())


def test_the_adr_named_regression_case_is_in_the_list() -> None:
    """ADR-0156 AC-F4: turn-forensics #1 (now turn-explanation#1) scored lower with the skill."""
    assert "turn-explanation#1" in {s.id for s in part2.load_scenarios(run.SCENARIOS_FILE)}


# ── the seed ─────────────────────────────────────────────────────────────────


def test_the_seed_dsn_is_postgres_eval_only() -> None:
    dsn = seed.eval_dsn("p@ss/word")
    assert dsn == "postgresql://agent:p%40ss%2Fword@localhost:5434/personal_agent"
    with pytest.raises(ValueError, match="not postgres-eval"):
        seed.eval_dsn("pw", port=5432)


class FakeConn:
    def __init__(self, status: str, stored: list[str] | None) -> None:
        self.status = status
        self.stored = stored
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    async def execute(self, sql: str, *args: object) -> str:
        self.calls.append((sql, args))
        return self.status

    async def fetchrow(self, sql: str, *args: object) -> dict[str, object] | None:
        self.calls.append((sql, args))
        return None if self.stored is None else {"revealed_tools": self.stored}


@pytest.mark.asyncio
async def test_seed_writes_one_row_and_reads_it_back() -> None:
    conn = FakeConn("UPDATE 1", ["search_logs", "measure_model_latency"])
    seeder = seed.EvalRevealSeeder(conn)
    assert await seeder.seed("s1", ["search_logs", "measure_model_latency"]) == [
        "measure_model_latency",
        "search_logs",
    ]
    assert conn.calls[0] == (seed.ADD_SQL, ("s1", ["search_logs", "measure_model_latency"]))


@pytest.mark.asyncio
async def test_seed_that_matches_no_row_fails() -> None:
    seeder = seed.EvalRevealSeeder(FakeConn("UPDATE 0", []))
    with pytest.raises(ValueError, match="UPDATE 0"):
        await seeder.seed("s1", ["search_logs"])


@pytest.mark.asyncio
async def test_read_of_a_missing_session_fails() -> None:
    seeder = seed.EvalRevealSeeder(FakeConn("UPDATE 1", None))
    with pytest.raises(ValueError, match="no session"):
        await seeder.read("s1")


# ── the pins ─────────────────────────────────────────────────────────────────


def fingerprint(build: str = "b1") -> dict[str, object]:
    return run.build_fingerprint(
        scenarios=part2.load_scenarios(run.SCENARIOS_FILE),
        builds={"control": build, "treatment": build},
        head="abc",
        props={"chat_template_sha256": "t"},
        server_args="--parallel 3",
    )


def test_the_fingerprint_pins_model_sampling_build_and_scenario_hashes() -> None:
    fp = fingerprint()
    assert fp["model_key"] == "qwen3.8-flash-next"
    assert (
        isinstance(fp["catalog_entry"], dict)
        and fp["catalog_entry"]["id"] == "unsloth/qwen3.8-flash-next"
    )
    pins = fp["scenarios"]
    assert isinstance(pins, list) and len(pins) == 11
    first = part2.load_scenarios(run.SCENARIOS_FILE)[0]
    assert pins[0] == {
        "id": first.id,
        "prompt_sha256": part2.sha256_text(first.prompt),
        "expect_sha256": part2.sha256_text(first.expect),
    }


def test_arms_on_different_builds_or_no_props_stop() -> None:
    with pytest.raises(SystemExit, match="different builds"):
        run.build_fingerprint(
            scenarios=[],
            builds={"control": "a", "treatment": "b"},
            head="h",
            props={},
            server_args="",
        )
    with pytest.raises(SystemExit, match="props"):
        run.build_fingerprint(
            scenarios=[],
            builds={"control": "a", "treatment": "a"},
            head="h",
            props={"error": "403"},
            server_args="",
        )


def test_a_rerun_with_other_pins_stops(tmp_path: Path) -> None:
    run.pin_run(tmp_path, fingerprint("b1"))
    run.pin_run(tmp_path, fingerprint("b1"))  # same pins: resumes
    with pytest.raises(SystemExit, match="gateway_builds"):
        run.pin_run(tmp_path, fingerprint("b2"))


# ── blinding ─────────────────────────────────────────────────────────────────


def row(sid: str, arm: str, reply: str, **extra: object) -> dict[str, object]:
    return {
        "id": sid,
        "arm": arm,
        "prompt": f"p {sid}",
        "expect": f"e {sid}",
        "reply": reply,
        "trace_id": f"t-{sid}-{arm}",
        **extra,
    }


ROWS = [
    row("a#0", "control", "ca"),
    row("a#0", "treatment", "ta"),
    row("b#1", "treatment", "old", error="boom"),
    row("b#1", "control", "cb"),
    row("b#1", "treatment", "tb"),
]


def test_blinding_hides_the_arm_and_keeps_only_valid_rows() -> None:
    items, key = judge.blind(ROWS)
    assert len(items) == 4
    for item in items:
        assert set(item) == {"rid", "prompt", "expect", "reply"}
        assert "control" not in json.dumps(item) and "treatment" not in json.dumps(item)
    assert {item["reply"] for item in items} == {"ca", "ta", "cb", "tb"}
    by_reply = {i["reply"]: key[i["rid"]] for i in items}
    assert by_reply["tb"] == {"id": "b#1", "arm": "treatment", "trace_id": "t-b#1-treatment"}


def test_blinding_is_seeded() -> None:
    assert judge.blind(ROWS) == judge.blind(ROWS)


# ── labels ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "label"),
    [
        ('{"label": "PASS", "reason": "ok"}', "PASS"),
        ('Here: {"label": "partial", "reason": "half"} done', "PARTIAL"),
        ('```json\n{"label": "FAIL", "reason": "no"}\n```', "FAIL"),
    ],
)
def test_parse_label_reads_the_json(text: str, label: str) -> None:
    assert judge.parse_label(text)[0] == label


@pytest.mark.parametrize("text", ["PASS", '{"label": "GOOD"}', "{}"])
def test_parse_label_refuses_a_bad_answer(text: str) -> None:
    with pytest.raises(ValueError):
        judge.parse_label(text)


def scripted(*texts: str) -> judge.JudgeFn:
    replies = list(texts)
    seen: list[str] = []

    async def call(system: str, user: str) -> judge.JudgeReply:
        assert system == judge.JUDGE_SYSTEM
        seen.append(user)
        return judge.JudgeReply(replies.pop(0), 1000, 100, 0.0045)

    call.seen = seen  # type: ignore[attr-defined]
    return call


ITEMS = [{"rid": "r01", "prompt": "P", "expect": "E", "reply": "R"}]


@pytest.mark.asyncio
async def test_a_judge_sees_prompt_expect_and_reply_only(tmp_path: Path) -> None:
    call = scripted('{"label": "PASS", "reason": "ok"}')
    rows = await judge.label_items(ITEMS, "1", call, tmp_path / "l.jsonl")
    assert rows[0]["label"] == "PASS" and float(str(rows[0]["cost_usd"])) > 0
    assert call.seen == ["REQUEST:\nP\n\nEXPECTED:\nE\n\nREPLY:\nR\n"]  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_a_bad_answer_is_asked_again_then_recorded_as_an_error(tmp_path: Path) -> None:
    out = tmp_path / "l.jsonl"
    rows = await judge.label_items(ITEMS, "2", scripted("nope", '{"label": "FAIL"}'), out)
    assert rows[0]["label"] == "FAIL" and rows[0]["attempts"] == 2 and "error" not in rows[0]
    out2 = tmp_path / "l2.jsonl"
    rows = await judge.label_items(ITEMS, "2", scripted("nope", "still no"), out2)
    assert "label" not in rows[0] and "error" in rows[0]
    # A rerun asks again only for the item without a label.
    rows = await judge.label_items(ITEMS, "2", scripted('{"label": "PASS"}'), out2)
    assert rows[0]["label"] == "PASS"
    assert await judge.label_items(ITEMS, "2", scripted(), out2) == []


# ── the score ────────────────────────────────────────────────────────────────

KEY = {
    "r01": {"id": "a#0", "arm": "control", "trace_id": "t"},
    "r02": {"id": "a#0", "arm": "treatment", "trace_id": "t"},
    "r03": {"id": "b#1", "arm": "control", "trace_id": "t"},
    "r04": {"id": "b#1", "arm": "treatment", "trace_id": "t"},
}


def labels(**by_judge: Mapping[str, str]) -> dict[str, dict[str, dict[str, object]]]:
    return {
        j.removeprefix("j"): {rid: {"rid": rid, "label": lab} for rid, lab in v.items()}
        for j, v in by_judge.items()
    }


def test_scores_sum_both_judges_and_ac1_passes_when_skills_are_not_lower() -> None:
    report = judge.score(
        KEY,
        labels(
            j1={"r01": "PARTIAL", "r02": "PASS", "r03": "PASS", "r04": "PASS"},
            j2={"r01": "PARTIAL", "r02": "PARTIAL", "r03": "PASS", "r04": "PASS"},
        ),
    )
    assert report["scenarios"] == [
        {"id": "a#0", "control": 2, "treatment": 3},
        {"id": "b#1", "control": 4, "treatment": 4},
    ]
    assert report["ac2"] == "PASS" and report["ac1"] == "PASS"
    assert report["totals"] == {"control": 6, "treatment": 7}


def test_a_lower_treatment_scenario_fails_ac1() -> None:
    """Seeded negative: one scenario lower with skills."""
    report = judge.score(
        KEY,
        labels(
            j1={"r01": "PASS", "r02": "PARTIAL", "r03": "PASS", "r04": "PASS"},
            j2={"r01": "PASS", "r02": "PASS", "r03": "PASS", "r04": "PASS"},
        ),
    )
    assert report["ac1"] == "FAIL" and report["lower_with_skills"] == ["a#0"]


def test_a_pass_fail_split_takes_the_third_label_in_place_of_the_fail() -> None:
    both = {"r01": "PARTIAL", "r03": "PASS", "r04": "PASS"}
    report = judge.score(
        KEY,
        labels(j1={**both, "r02": "PASS"}, j2={**both, "r02": "FAIL"}, j3={"r02": "PARTIAL"}),
    )
    replies = report["replies"]
    assert isinstance(replies, list)
    r02 = next(r for r in replies if r["rid"] == "r02")
    assert r02["judge_1"] == "PASS" and r02["judge_2"] == "FAIL" and r02["judge_3"] == "PARTIAL"
    assert r02["final"] == ["PASS", "PARTIAL"] and r02["points"] == 3
    assert report["splits"] == ["r02"] and report["ac2"] == "PASS"


def test_a_split_without_a_third_label_fails_ac2_and_leaves_ac1_incomplete() -> None:
    both = {"r01": "PARTIAL", "r03": "PASS", "r04": "PASS"}
    report = judge.score(KEY, labels(j1={**both, "r02": "FAIL"}, j2={**both, "r02": "PASS"}))
    assert report["missing_third"] == ["r02"]
    assert report["ac2"] == "FAIL" and report["ac1"] == "incomplete"


def test_a_third_label_on_a_non_split_fails_ac2() -> None:
    same = {"r01": "PASS", "r02": "PASS", "r03": "PASS", "r04": "PASS"}
    report = judge.score(KEY, labels(j1=same, j2=same, j3={"r01": "FAIL"}))
    assert report["unexpected_third"] == ["r01"] and report["ac2"] == "FAIL"


def test_a_reply_without_both_labels_fails_ac2() -> None:
    report = judge.score(
        KEY,
        labels(
            j1={"r01": "PASS", "r02": "PASS", "r03": "PASS", "r04": "PASS"},
            j2={"r01": "PASS", "r02": "PASS", "r03": "PASS"},
        ),
    )
    assert (
        report["missing_labels"] == ["r04"]
        and report["ac2"] == "FAIL"
        and report["ac1"] == "incomplete"
    )


def test_a_scenario_with_one_arm_fails_ac2() -> None:
    key = {rid: v for rid, v in KEY.items() if rid != "r04"}
    same = {"r01": "PASS", "r02": "PASS", "r03": "PASS"}
    report = judge.score(key, labels(j1=same, j2=same))
    assert report["one_arm"] == ["b#1"] and report["ac2"] == "FAIL"


def test_the_judges_resolve_from_the_catalog() -> None:
    pins = judge.judge_pins()
    judges = pins["judges"]
    assert isinstance(judges, dict)
    assert judges["1"] == {
        "key": "claude_sonnet",
        "provider": "anthropic",
        "model_id": "claude-sonnet-5",
    }
    assert judges["2"]["provider"] == "openai" and judges["3"]["key"] == "claude_haiku"
    assert pins["system_sha256"] == part2.sha256_text(judge.JUDGE_SYSTEM)
