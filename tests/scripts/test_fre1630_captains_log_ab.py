"""Tests for the FRE-1630 candidate arm of the Captain's Log side-by-side (AC-3)."""

from __future__ import annotations

import json
import pathlib

import pytest
from scripts.eval.fre1628_captains_log_ab import harness

RIDS = [f"c{i:02d}" for i in range(1, 11)]


def _write(path: pathlib.Path, rows: list[dict[str, object]]) -> None:
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _labels(candidate: str, winners: dict[str, list[str]]) -> list[dict[str, object]]:
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
    _write(run_dir / "labels-haiku.jsonl", _labels("haiku", _wins("haiku", 6, 1)))

    assert harness.score("haiku") == 1  # 7 of 10 < bar 8
    report = json.loads((run_dir / "report-haiku.json").read_text(encoding="utf-8"))

    assert report["haiku_not_worse"] == 7
    assert report["bar_not_worse"] == 8
    assert report["haiku_off_dspy_path"] == []
    assert report["passed"] is False
    assert set(report["cost_usd"]) == {"sonnet_generate", "haiku_generate", "judges"}
    assert set(report["per_judge"]["j0"]) == {"haiku", "sonnet", "tie", "error"}


def test_luna_passes_at_eight_not_worse(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels("luna", _wins("luna", 6, 2)))

    assert harness.score("luna") == 0
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 8
    assert report["passed"] is True
    assert report["per_pair"]["c01"] == "luna"
    assert report["per_pair"]["c07"] == "tie"
    assert report["per_pair"]["c09"] == "sonnet"


def test_luna_fails_at_seven_not_worse(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels("luna", _wins("luna", 6, 1)))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 7
    assert report["passed"] is False


def test_luna_off_dspy_path_fails_the_bar(run_dir: pathlib.Path) -> None:
    _write(run_dir / "labels-luna.jsonl", _labels("luna", _wins("luna", 10)))
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
    _write(run_dir / "labels-haiku.jsonl", _labels("haiku", _wins("haiku", 10)))
    _write(run_dir / "labels-luna.jsonl", _labels("luna", _wins("luna", 0)))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["luna_not_worse"] == 0


def test_an_unparsed_pair_is_not_evidence(run_dir: pathlib.Path) -> None:
    winners = _wins("luna", 8)  # exactly the bar
    winners["c01"] = ["luna", "luna", "error"]
    _write(run_dir / "labels-luna.jsonl", _labels("luna", winners))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["per_pair"]["c01"] == "error"
    assert report["luna_not_worse"] == 7
    assert report["passed"] is False


def test_a_missing_pair_is_incomplete(run_dir: pathlib.Path) -> None:
    winners = _wins("luna", 10)
    del winners["c10"]
    _write(run_dir / "labels-luna.jsonl", _labels("luna", winners))

    assert harness.score("luna") == 1
    report = json.loads((run_dir / "report-luna.json").read_text(encoding="utf-8"))

    assert report["complete"] is False
    assert report["passed"] is False
