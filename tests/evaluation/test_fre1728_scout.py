"""FRE-1728: the Scout stage's budget refusal, its conversion to defects and the merged ranking."""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest
from scripts.eval.fre1728 import stub_model

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "fre1728"
sys.path.insert(0, str(_DIR))

import scout_scan  # type: ignore[import-not-found]  # noqa: E402


def _write_run(tmp_path: Path) -> None:
    for run in ("A", "B"):
        path = tmp_path / "transcripts" / run / "user_x" / "episode_1.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "task": "plan my week",
                    "turns": [
                        {"agent_response": "a plan", "user_simulator": {"text": "too basic"}},
                        {"agent_response": "better", "user_simulator": {"text": None}},
                    ],
                }
            )
        )
    (tmp_path / "manifest.json").write_text(json.dumps({"status": "complete", "runs": ["A", "B"]}))


def test_transcript_turns_keep_order_and_drop_an_empty_message(tmp_path: Path) -> None:
    _write_run(tmp_path)

    episode = scout_scan.load_episodes(tmp_path, ["A"])[0]

    assert episode["id"] == "A|user_x|1"
    assert scout_scan.transcript_turns(episode["transcript"]) == [
        ("user", "plan my week"),
        ("assistant", "a plan"),
        ("user", "too basic"),
        ("assistant", "better"),
    ]


def test_estimate_grows_with_questions_and_a_cheaper_model_costs_less(tmp_path: Path) -> None:
    _write_run(tmp_path)
    episodes = scout_scan.load_episodes(tmp_path, ["A", "B"])

    calls, mini = scout_scan.estimate_usd(episodes, 5, "openai/gpt-5.4-mini")
    _, more = scout_scan.estimate_usd(episodes, 10, "openai/gpt-5.4-mini")
    _, luna = scout_scan.estimate_usd(episodes, 5, "openai/gpt-6-luna")

    assert calls == 10
    assert more > mini > luna > 0


def test_scan_over_the_budget_is_refused_before_it_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _write_run(tmp_path)

    def fail(*_args: object) -> None:
        raise AssertionError("the scan must not start")

    monkeypatch.setattr(scout_scan, "run_scan", fail)
    code = scout_scan.main(["--run-dir", str(tmp_path), "--runs", "A", "B", "--remaining-usd", "0"])

    assert code == scout_scan.EXIT_OVER_BUDGET
    assert not (tmp_path / "scout").exists()


def test_only_a_positive_answer_becomes_a_fact(tmp_path: Path) -> None:
    _write_run(tmp_path)
    episodes = scout_scan.load_episodes(tmp_path, ["A"])
    findings = [
        {
            "transcript_id": "A|user_x|1",
            "question": "wrong_level",
            "value": True,
            "explanation": "e",
        },
        {
            "transcript_id": "A|user_x|1",
            "question": "repeated_request",
            "value": False,
            "explanation": "",
        },
    ]

    facts = scout_scan.findings_to_facts(findings, episodes)

    assert [f["label"] for f in facts["A"]] == ["wrong_level"]
    assert facts["A"][0]["outcome"] == scout_scan.OUTCOME
    assert facts["A"][0]["transcript"] == "transcripts/A/user_x/episode_1.json"


def test_merged_ranking_holds_both_kinds_and_keeps_the_both_runs_rule(tmp_path: Path) -> None:
    _write_run(tmp_path)
    memory = {
        "run": "A",
        "user_id": "user_x",
        "category": "skill_memory",
        "dimension": "d",
        "outcome": "write_failure",
        "label": "memory_failure",
        "graph_score": 0.0,
        "answer_score": 0.0,
        "invalid_reasons": [],
        "transcript": "t",
    }
    for run in ("A", "B"):
        path = tmp_path / "results" / run / "user_x.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps([{**memory, "run": run}]))
        (tmp_path / "results" / run / "user_x.wipe.json").write_text('{"waited_s": 0}')
    episodes = scout_scan.load_episodes(tmp_path, ["A", "B"])
    findings = [
        {
            "transcript_id": "A|user_x|1",
            "question": "wrong_level",
            "value": True,
            "explanation": "",
        },
        {
            "transcript_id": "B|user_x|1",
            "question": "wrong_level",
            "value": True,
            "explanation": "",
        },
        {
            "transcript_id": "A|user_x|1",
            "question": "repeated_request",
            "value": True,
            "explanation": "",
        },
    ]

    scout_scan.rank_everything(
        tmp_path,
        ["A", "B"],
        scout_scan.findings_to_facts(findings, episodes),
        {"memory_failure": 2.0, "wrong_level": 1.0, "repeated_request": 5.0},
    )

    defects = json.loads((tmp_path / "defects.json").read_text())
    assert [d["label"] for d in defects] == ["memory_failure", "wrong_level"]
    assert "wrong_level" in (tmp_path / "report.md").read_text()


def test_every_question_has_a_severity_and_every_name_is_unique() -> None:
    questions = json.loads((_DIR / "scout_questions.json").read_text())
    severity = json.loads((_DIR / "severity.json").read_text())

    names = [q["name"] for q in questions]
    assert len(names) == len(set(names))
    assert set(names) <= set(severity)


def test_stub_answers_a_scout_prompt_in_scout_format() -> None:
    def ask(question: str) -> str:
        prompt = f"[BEGIN TRANSCRIPT]\nx\n[END TRANSCRIPT]\n{question}\n'ANSWER: $VALUE'"
        return stub_model.reply_for({"messages": [{"role": "user", "content": prompt}]})

    assert ask("Did the assistant report an internal failure?").endswith("ANSWER: yes")
    assert ask("Did the user say that it was too basic?").endswith("ANSWER: no")
