"""Tests for the FRE-1668 artifact builder side-by-side harness (AC-2, AC-3, AC-4, AC-6)."""

from __future__ import annotations

import json
import pathlib
import shutil
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

import pytest
import structlog
import yaml
from scripts.eval.fre1668_artifact_builder_ab import harness

from personal_agent.config.model_loader import load_model_config
from personal_agent.tools import artifact_tools

GOOD_HTML = "<!DOCTYPE html><html><head><title>t</title></head><body><h1>Hi</h1></body></html>"


class _FakeClient:
    """Stands in for the paid client: logs the same completion event and returns HTML."""

    def __init__(self, model: str, *, html: str = GOOD_HTML, boom: str | None = None) -> None:
        self.model, self.html, self.boom = model, html, boom

    async def respond(self, **_: Any) -> dict[str, Any]:
        if self.boom:
            raise RuntimeError(self.boom)
        structlog.get_logger().info(
            "model_call_completed",
            model=self.model,
            input_tokens=100,
            output_tokens=200,
            cost_usd=0.01,
        )
        return {"content": self.html, "usage": {"completion_tokens": 200}}


@pytest.fixture
def builder_env(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    """Make the real executor run offline: all providers available, run dir in tmp."""
    monkeypatch.setattr(harness, "OUT_DIR", tmp_path)
    monkeypatch.setattr(harness, "HTML_DIR", tmp_path / "html")
    monkeypatch.setattr(
        "personal_agent.orchestrator.constraint_options.build_provider_availability",
        lambda *_a, **_k: lambda _name: True,
    )
    return tmp_path


def _plan() -> harness.Plan:
    return harness.Plan(prompt_id="p1", slug="s", title="T", summary="S", plan="Sections: one.")


def _install(monkeypatch: pytest.MonkeyPatch, client: _FakeClient) -> None:
    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key", lambda *_a, **_k: client
    )


@pytest.mark.asyncio
async def test_build_never_calls_real_artifact_write(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def _boom(**_: Any) -> dict[str, Any]:
        raise AssertionError("the real artifact_write_executor was called")

    monkeypatch.setattr(artifact_tools, "artifact_write_executor", _boom)
    _install(monkeypatch, _FakeClient("anthropic/claude-sonnet-5"))
    row = await harness.build_one(_plan(), "A", skip_render=True)
    assert row["ok"] is True, row
    assert (builder_env / "html" / "p1-A.html").read_text(encoding="utf-8").startswith("<!DOCTYPE")
    assert artifact_tools.artifact_write_executor is _boom, "the patch must be undone after a build"


@pytest.mark.asyncio
async def test_failed_build_row_has_a_reason(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _FakeClient("anthropic/claude-sonnet-5", boom="provider said no"))
    row = await harness.build_one(_plan(), "A", skip_render=True)
    assert row["ok"] is False
    assert "provider said no" in str(row["failure_reason"])


@pytest.mark.asyncio
async def test_served_model_must_match_the_requested_key(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Arm C asked for Sol; the log says Sonnet 5 answered: a silent substitution.
    _install(monkeypatch, _FakeClient("anthropic/claude-sonnet-5"))
    row = await harness.build_one(_plan(), "C", skip_render=True)
    assert row["ok"] is False
    assert "claude-sonnet-5" in str(row["failure_reason"])


@pytest.mark.asyncio
async def test_matching_served_model_is_recorded(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(monkeypatch, _FakeClient("openai/gpt-6.1-sol"))
    row = await harness.build_one(_plan(), "C", skip_render=True)
    assert row["ok"] is True, row
    assert row["requested_key"] == row["resolved_key"] == "gpt-6.1-sol"
    assert row["served_model"] == "openai/gpt-6.1-sol"
    assert row["output_tokens"] == 200 and row["cost_usd"] == pytest.approx(0.01)


def test_missing_provider_key_stops_the_run() -> None:
    catalog = load_model_config()
    with pytest.raises(harness.ArmResolutionError, match="gpt-6.1-sol"):
        harness.resolve_arms(catalog, is_provider_available=lambda p: p != "openai")


def test_all_arms_resolve_when_providers_are_available() -> None:
    catalog = load_model_config()
    assert harness.resolve_arms(catalog, is_provider_available=lambda _p: True) == harness.ARMS


def test_sonnet_arm_resolves_like_the_role_path() -> None:
    from personal_agent.config.model_loader import resolve_role_model_key

    assert resolve_role_model_key("artifact_builder") == harness.ARMS["A"]


@pytest.mark.parametrize(
    "bad",
    [
        {"settings": "postgresql+asyncpg://agent@127.0.0.1:5432/personal_agent"},
        {"cost_tracker": "postgresql://agent@db.example:5434/personal_agent"},
        {"engine": "postgresql+asyncpg://agent@127.0.0.1:5433/personal_agent"},
    ],
)
def test_db_binding_assert_covers_gate_tracker_and_engine(bad: Mapping[str, str]) -> None:
    good = {k: "postgresql://agent@127.0.0.1:5434/personal_agent" for k in harness.DB_BINDINGS}
    harness.assert_eval_database(good)
    with pytest.raises(harness.EvalIsolationError):
        harness.assert_eval_database({**good, **bad})


def test_blind_labels_hide_arm_and_are_reproducible(tmp_path: pathlib.Path) -> None:
    rows = [
        {"arm": a, "prompt_id": f"p{i}", "ok": True, "html_path": str(tmp_path / f"{i}{a}.html")}
        for i in range(1, 4)
        for a in harness.ARMS
    ]
    for r in rows:
        pathlib.Path(str(r["html_path"])).write_text(GOOD_HTML, encoding="utf-8")
    key1 = harness.write_rating_pack(rows, tmp_path / "r1", tmp_path / "key1.json")
    key2 = harness.write_rating_pack(rows, tmp_path / "r2", tmp_path / "key2.json")
    assert key1 == key2
    assert sorted(key1) == [f"P{n:02d}" for n in range(1, 13)]
    assert [p.name for p in (tmp_path / "r1").iterdir()] == ["index.html"]
    index = (tmp_path / "r1" / "index.html").read_text(encoding="utf-8")
    for needle in (*harness.ARMS, *harness.ARMS.values(), "sonnet", "sol"):
        assert f'"{needle}"' not in index and f">{needle}<" not in index
    assert json.loads((tmp_path / "key1.json").read_text(encoding="utf-8")) == key1


def _rows(arm_values: Mapping[str, float]) -> list[dict[str, Any]]:
    return [
        {"arm": a, "prompt_id": f"p{i}", "ok": True, "render_ok": True, "latency_s": v,
         "output_tokens": 100, "cost_usd": 0.1, "score": 3}
        for a, v in arm_values.items()
        for i in range(1, 9)
    ]  # fmt: skip


def test_decision_is_no_decision_inside_the_noise_band() -> None:
    table = harness.decision_table(_rows({"A": 100.0, "A2": 110.0, "B": 105.0, "C": 150.0}))
    lat = {r["arm"]: r for r in table if r["measure"] == "median latency (s)"}
    assert lat["B"]["verdict"] == "no decision"  # |5| <= noise band 10
    assert lat["C"]["verdict"] != "no decision"  # |50| > 10
    assert lat["B"]["noise_band"] == pytest.approx(10.0)


def _complete_rows() -> list[dict[str, Any]]:
    return [
        {"arm": a, "prompt_id": f"p{i}", "ok": True, "render_ok": True, "failure_reason": ""}
        for a in harness.ARMS
        for i in range(1, 9)
    ]


def test_report_accepts_a_complete_set() -> None:
    labels = {f"P{n:02d}": {"arm": "A", "prompt_id": "p1"} for n in range(1, 33)}
    harness.check_complete(_complete_rows(), labels, {k: 3 for k in labels}, n_prompts=8)


@pytest.mark.parametrize("defect", ["missing_row", "duplicate_row", "missing_score", "bad_score"])
def test_report_fails_on_incomplete_rows_or_scores(defect: str) -> None:
    rows = _complete_rows()
    labels = {f"P{n:02d}": {"arm": "A", "prompt_id": "p1"} for n in range(1, 33)}
    scores: dict[str, Any] = {k: 3 for k in labels}
    if defect == "missing_row":
        rows.pop()
    elif defect == "duplicate_row":
        rows[-1] = dict(rows[0])
    elif defect == "missing_score":
        scores.pop("P05")
    else:
        scores["P05"] = 9
    with pytest.raises(harness.IncompleteRunError):
        harness.check_complete(rows, labels, scores, n_prompts=8)


def test_failed_build_without_reason_is_incomplete() -> None:
    rows = _complete_rows()
    rows[0].update(ok=False, failure_reason="")
    with pytest.raises(harness.IncompleteRunError, match="reason"):
        harness.check_complete(rows, {}, {}, n_prompts=8, need_scores=False)


def test_budget_headroom_stops_before_the_first_call() -> None:
    caps = [
        harness.Cap("daily", "artifact_builder", Decimal("1.00")),
        harness.Cap("weekly", "artifact_builder", Decimal("5.00")),
        harness.Cap("weekly", "_total", Decimal("10.00")),
    ]
    spent = {("daily", "artifact_builder"): Decimal("0.20")}
    harness.check_headroom(caps, spent, {"artifact_builder": Decimal("0.70")})
    with pytest.raises(harness.BudgetShortfallError, match="daily artifact_builder"):
        harness.check_headroom(caps, spent, {"artifact_builder": Decimal("0.90")})
    with pytest.raises(harness.BudgetShortfallError, match="_total"):
        harness.check_headroom(
            caps, spent, {"artifact_builder": Decimal("0.50"), "study": Decimal("10")}
        )


def test_verbatim_prompts_match_fre1615() -> None:
    suite = yaml.safe_load(pathlib.Path("scripts/eval/fre1615/suite.yaml").read_text("utf-8"))
    by_id = (
        {t["id"]: t["message"] for t in suite["tasks"]}
        if "tasks" in suite
        else {t["id"]: t["message"] for t in suite}
    )
    prompts = {p.id: p for p in harness.load_prompts()}
    assert len(prompts) == 8
    assert prompts["p1"].request == by_id["ar1"]
    assert prompts["p6"].request == by_id["ar2"]
    assert prompts["p7"].request == by_id["ar3"]
    assert "existing" in prompts["p8"].request.lower()


_NEEDS_BROWSER = pytest.mark.skipif(
    shutil.which("node") is None
    or not pathlib.Path("seshat-pwa/node_modules/playwright").exists()
    or not list(pathlib.Path.home().glob(".cache/ms-playwright/chromium-*")),
    reason="needs node, seshat-pwa/node_modules/playwright and a cached Chromium",
)


@_NEEDS_BROWSER
def test_render_check_passes_a_good_page_and_fails_a_broken_one(tmp_path: pathlib.Path) -> None:
    good = tmp_path / "good.html"
    good.write_text(
        "<!DOCTYPE html><html><body><h1>Hello</h1>"
        "<p>This page has enough visible text to pass the blank test.</p></body></html>",
        encoding="utf-8",
    )
    bad = tmp_path / "bad.html"
    bad.write_text(
        "<!DOCTYPE html><html><body><p>Enough visible text to pass the blank test, but it throws."
        '</p><script>throw new Error("boom")</script>'
        '<script src="https://cdn.example.net/x.js"></script></body></html>',
        encoding="utf-8",
    )
    assert harness.render_html(good)["render_ok"] is True
    broken = harness.render_html(bad)
    assert broken["render_ok"] is False
    assert broken["page_errors"] and broken["blocked_requests"]
