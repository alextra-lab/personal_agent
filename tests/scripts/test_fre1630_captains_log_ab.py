"""Tests for the FRE-1630 candidate arm of the Captain's Log side-by-side (AC-3)."""

from __future__ import annotations

import asyncio
import json
import pathlib

import pytest
from scripts.eval.fre1628_captains_log_ab import harness

RIDS = [f"c{i:02d}" for i in range(1, 11)]


def _write(path: pathlib.Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _labels(winners: dict[str, list[str]]) -> list[dict[str, object]]:
    """Three judge rows per pair, ``winners[rid]`` in judge order."""
    return [
        {"rid": rid, "judge": f"j{n}", "winner": w, "cost_usd": 0.01}
        for rid, ws in winners.items()
        for n, w in enumerate(ws)
    ]


@pytest.fixture
def run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setattr(harness, "OUT_DIR", tmp_path)
    monkeypatch.setattr(harness, "INPUTS", tmp_path / "inputs.jsonl")
    _write(tmp_path / "inputs.jsonl", [{"rid": rid} for rid in RIDS])
    for arm in ("sonnet", "haiku", "luna"):
        _write(
            tmp_path / f"outputs-{arm}.jsonl",
            [{"rid": rid, "arm": arm, "path": "dspy", "cost_usd": 0.1} for rid in RIDS],
        )
    return tmp_path


def _wins(cand: str, n_cand: int, n_tie: int = 0) -> dict[str, list[str]]:
    """``n_cand`` pairs the candidate wins, ``n_tie`` split pairs, the rest Sonnet wins."""
    out: dict[str, list[str]] = {}
    for i, rid in enumerate(RIDS):
        if i < n_cand:
            out[rid] = [cand, cand, "sonnet"]
        elif i < n_cand + n_tie:
            out[rid] = [cand, "sonnet", "tie"]
        else:
            out[rid] = ["sonnet", "sonnet", cand]
    return out


def test_arms_bind_luna_and_keep_haiku() -> None:
    assert harness.ARMS == {
        "sonnet": "claude_sonnet",
        "haiku": "claude_haiku_5_5",
        "luna": "gpt-6-luna",
    }


def test_haiku_rescore_keeps_the_fre1628_report_shape(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-haiku.jsonl", _labels(_wins("haiku", 6, 1)))

    assert harness.score("haiku") == 1  # 7 of 10 < bar 8
    report = json.loads((run_dir / "report-haiku.json").read_text(encoding="utf-8"))

    assert report["haiku_not_worse"] == 7
    assert report["bar_not_worse"] == 8
    assert report["haiku_off_dspy_path"] == []
    assert report["passed"] is False
    assert set(report["cost_usd"]) == {"sonnet_generate", "haiku_generate", "judges"}
    assert set(report["per_judge"]["j0"]) == {"haiku", "sonnet", "tie", "error"}


def test_luna_passes_at_eight_not_worse(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels(_wins("luna", 6, 2)))

    assert harness.score("luna") == 0
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 8
    assert report["passed"] is True
    assert report["per_pair"]["c01"] == "luna"
    assert report["per_pair"]["c07"] == "tie"
    assert report["per_pair"]["c09"] == "sonnet"


def test_luna_fails_at_seven_not_worse(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels(_wins("luna", 6, 1)))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 7
    assert report["passed"] is False


def test_luna_off_dspy_path_fails_the_bar(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels(_wins("luna", 10)))
    outputs: list[dict[str, object]] = [
        {"rid": rid, "arm": "luna", "path": "manual" if rid == "c03" else "dspy", "cost_usd": 0.1}
        for rid in RIDS
    ]
    _write(run_dir / "outputs-luna.jsonl", outputs)

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 10
    assert report["luna_off_dspy_path"] == ["c03"]
    assert report["passed"] is False


def test_luna_score_ignores_haiku_labels(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-haiku.jsonl", _labels(_wins("haiku", 10)))
    _write(run_dir / "labels-luna.jsonl", _labels(_wins("luna", 0)))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 0


def test_an_unparsed_pair_is_not_evidence(run_dir: pathlib.Path) -> None:
    winners = _wins("luna", 8)  # exactly the bar
    winners["c01"] = ["luna", "luna", "error"]
    _write(run_dir / "labels-luna.jsonl", _labels(winners))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["per_pair"]["c01"] == "error"
    assert report["luna_not_worse"] == 7
    assert report["passed"] is False


def test_a_missing_pair_is_incomplete(run_dir: pathlib.Path) -> None:
    winners = _wins("luna", 10)
    del winners["c10"]
    _write(run_dir / "labels-luna.jsonl", _labels(winners))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["complete"] is False
    assert report["passed"] is False


# ── FRE-1633: swapped order and the combined 6-vote score ────────────────────


class _FakeGate:
    def __init__(self, **_kwargs: object) -> None: ...

    async def connect(self) -> None: ...

    async def disconnect(self) -> None: ...


def _stored_run(run_dir: pathlib.Path, candidate_is_a: dict[str, bool]) -> None:
    """Pairs on disk; the stored labels record which slot the candidate held."""
    _write(
        run_dir / "inputs.jsonl",
        [
            {
                "rid": rid,
                "user_message": "hi",
                "steps_count": 1,
                "reply_length": 2,
                "metrics_summary": None,
                "events": [],
            }
            for rid in candidate_is_a
        ],
    )
    for arm in ("sonnet", "luna"):
        _write(
            run_dir / f"outputs-{arm}.jsonl",
            [
                {"rid": rid, "arm": arm, "path": "dspy", "reflection": {"rationale": f"{arm}-text"}}
                for rid in candidate_is_a
            ],
        )
    _write(
        run_dir / "labels-luna.jsonl",
        [
            {"rid": rid, "judge": "claude_sonnet", "candidate_is_a": is_a, "winner": "luna"}
            for rid, is_a in candidate_is_a.items()
        ],
    )


def _fake_judge_runtime(monkeypatch: pytest.MonkeyPatch, verdict: str) -> list[str]:
    """Stub the gate, telemetry and judge calls. Returns the prompts the judges receive."""
    import personal_agent.captains_log.reflection as reflection
    import personal_agent.cost_gate as cost_gate

    prompts: list[str] = []

    def _call(_key: str) -> object:
        async def call(_system: str, user: str) -> tuple[str, float]:
            prompts.append(user)
            return json.dumps({"verdict": verdict, "reason": "r"}), 0.01

        return call

    monkeypatch.setattr(cost_gate, "CostGate", _FakeGate)
    monkeypatch.setattr(cost_gate, "load_budget_config", lambda: None)
    monkeypatch.setattr(reflection, "_summarize_telemetry", lambda *_a, **_k: "T")
    monkeypatch.setattr(harness, "_translated_events", lambda _row: [])
    monkeypatch.setattr(harness, "_judge_call", _call)
    return prompts


def _slot_a_arm(prompt: str) -> str:
    slot_a = prompt.split("REFLECTION A:\n")[1].split("REFLECTION B:")[0]
    return "sonnet" if "sonnet-text" in slot_a else "luna"


def test_swapped_judge_exchanges_a_and_b_and_maps_each_label_back(
    run_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # c01: the candidate held slot A in the stored order. c02: slot B.
    _stored_run(run_dir, {"c01": True, "c02": False})
    prompts = _fake_judge_runtime(monkeypatch, "A")

    assert asyncio.run(harness.judge("luna", swapped=True)) == 0

    # Three judges per pair, in pair order: slot A now holds the other model.
    assert [_slot_a_arm(p) for p in prompts] == ["sonnet"] * 3 + ["luna"] * 3
    swapped = harness._read_jsonl(run_dir / "labels-luna-swapped.jsonl")
    assert [(r["rid"], r["candidate_is_a"], r["verdict"], r["winner"]) for r in swapped[::3]] == [
        ("c01", False, "A", "sonnet"),
        ("c02", True, "A", "luna"),
    ]
    # The stored labels are untouched and keep their own file.
    assert [r["candidate_is_a"] for r in harness._read_jsonl(run_dir / "labels-luna.jsonl")] == [
        True,
        False,
    ]


def test_swapped_judge_maps_a_b_verdict_to_the_model_in_that_slot(
    run_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_run(run_dir, {"c01": True})
    _fake_judge_runtime(monkeypatch, "B")

    asyncio.run(harness.judge("luna", swapped=True))

    # Swapped: Sonnet is in slot A and the candidate in slot B, so a "B" verdict is the candidate.
    winners = {r["winner"] for r in harness._read_jsonl(run_dir / "labels-luna-swapped.jsonl")}
    assert winners == {"luna"}


def test_swapped_judge_reads_the_fre1628_haiku_slot_key(
    run_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_run(run_dir, {"c01": True})
    for arm in ("haiku",):
        _write(
            run_dir / f"outputs-{arm}.jsonl",
            [{"rid": "c01", "arm": arm, "path": "dspy", "reflection": {"rationale": "haiku-text"}}],
        )
    # FRE-1628 labels wrote ``haiku_is_a``, not ``candidate_is_a``.
    _write(
        run_dir / "labels-haiku.jsonl",
        [{"rid": "c01", "judge": "claude_sonnet", "haiku_is_a": False, "winner": "haiku"}],
    )
    prompts = _fake_judge_runtime(monkeypatch, "A")

    asyncio.run(harness.judge("haiku", swapped=True))

    # Stored: Sonnet in slot A. Swapped: the candidate in slot A.
    assert "haiku-text" in prompts[0].split("REFLECTION B:")[0].split("REFLECTION A:\n")[1]


def test_swapped_judge_needs_the_stored_labels(
    run_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_run(run_dir, {"c01": True})
    (run_dir / "labels-luna.jsonl").unlink()
    prompts = _fake_judge_runtime(monkeypatch, "A")

    with pytest.raises(ValueError, match="stored"):
        asyncio.run(harness.judge("luna", swapped=True))
    assert prompts == []


def test_judge_limit_runs_only_the_first_pairs(
    run_dir: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _stored_run(run_dir, {"c01": True, "c02": False})
    prompts = _fake_judge_runtime(monkeypatch, "A")

    asyncio.run(harness.judge("luna", swapped=True, limit=1))

    assert len(prompts) == 3
    assert {r["rid"] for r in harness._read_jsonl(run_dir / "labels-luna-swapped.jsonl")} == {"c01"}


def _both_orders(
    run_dir: pathlib.Path, stored: dict[str, list[str]], swapped: dict[str, list[str]]
) -> dict[str, object]:
    _write(run_dir / "labels-luna.jsonl", _labels(stored))
    _write(run_dir / "labels-luna-swapped.jsonl", _labels(swapped))
    harness.score("luna", combined=True)
    return json.loads((run_dir / "report-luna-combined.json").read_text(encoding="utf-8"))


def _pairs(stored: list[str], swapped: list[str], rids: int = 10) -> dict[str, list[list[str]]]:
    return {"stored": [stored] * rids, "swapped": [swapped] * rids}


def test_combined_pair_is_worse_only_at_four_sonnet_votes_of_six(run_dir: pathlib.Path) -> None:
    # Sonnet gets 3 of 6: not worse. 4 of 6: worse.
    three = {rid: ["sonnet", "sonnet", "luna"] for rid in RIDS}
    three_s = {rid: ["sonnet", "luna", "luna"] for rid in RIDS}
    four_s = {rid: ["sonnet", "sonnet", "luna"] for rid in RIDS}
    assert _both_orders(run_dir, three, three_s)["per_pair"]["c01"] == "not_worse"
    assert _both_orders(run_dir, three, four_s)["per_pair"]["c01"] == "worse"


def test_combined_tie_votes_count_for_neither_side(run_dir: pathlib.Path) -> None:
    # 3 Sonnet, 3 tie: Sonnet is not at 4 votes.
    report = _both_orders(
        run_dir,
        {rid: ["sonnet", "sonnet", "tie"] for rid in RIDS},
        {rid: ["sonnet", "tie", "tie"] for rid in RIDS},
    )
    assert report["per_pair"]["c01"] == "not_worse"
    assert report["table"]["c01"] == {"luna": 0, "sonnet": 3, "tie": 3, "error": 0}


def test_combined_error_label_is_excluded_and_needs_a_sonnet_majority_of_the_rest(
    run_dir: pathlib.Path,
) -> None:
    # 1 error, 5 valid votes: Sonnet 3 of 5 is a majority, so the pair is worse.
    stored = {rid: ["sonnet", "sonnet", "error"] for rid in RIDS}
    swapped = {rid: ["sonnet", "luna", "luna"] for rid in RIDS}
    assert _both_orders(run_dir, stored, swapped)["per_pair"]["c01"] == "worse"
    # 1 error, 5 valid votes: Sonnet 2 of 5 is no majority.
    stored = {rid: ["sonnet", "luna", "error"] for rid in RIDS}
    swapped = {rid: ["sonnet", "luna", "luna"] for rid in RIDS}
    assert _both_orders(run_dir, stored, swapped)["per_pair"]["c01"] == "not_worse"


def test_combined_bar_is_eight_not_worse_of_ten(run_dir: pathlib.Path) -> None:
    def build(n_worse: int) -> dict[str, list[str]]:
        return {
            rid: (["sonnet"] * 3 if i < n_worse else ["luna"] * 3) for i, rid in enumerate(RIDS)
        }

    report = _both_orders(run_dir, build(2), build(2))
    assert report["luna_not_worse"] == 8
    assert report["passed"] is True
    report = _both_orders(run_dir, build(3), build(3))
    assert report["luna_not_worse"] == 7
    assert report["passed"] is False


def test_combined_reports_the_slot_a_rate_in_each_order(run_dir: pathlib.Path) -> None:
    def rows(verdicts: list[str]) -> list[dict[str, object]]:
        return [
            {"rid": rid, "judge": f"j{n}", "verdict": v, "winner": "luna", "cost_usd": 0.01}
            for rid in RIDS
            for n, v in enumerate(verdicts)
        ]

    _write(run_dir / "labels-luna.jsonl", rows(["A", "A", "A"]))
    _write(run_dir / "labels-luna-swapped.jsonl", rows(["A", "B", "B"]))
    harness.score("luna", combined=True)
    report = json.loads((run_dir / "report-luna-combined.json").read_text(encoding="utf-8"))

    assert report["slot_a_rate"] == {"stored": 1.0, "swapped": pytest.approx(1 / 3)}


def test_combined_needs_both_orders_for_every_pair(run_dir: pathlib.Path) -> None:
    stored = {rid: ["luna"] * 3 for rid in RIDS}
    swapped = {rid: ["luna"] * 3 for rid in RIDS if rid != "c10"}
    report = _both_orders(run_dir, stored, swapped)

    assert report["complete"] is False
    assert report["passed"] is False
