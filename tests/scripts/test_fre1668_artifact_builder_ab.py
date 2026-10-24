"""Tests for the FRE-1668 artifact builder side-by-side harness (AC-2, AC-3, AC-4, AC-6)."""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess
from collections.abc import Mapping
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, cast

import pytest
import structlog
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


def test_sol_arm_resolves_like_the_role_path() -> None:
    """FRE-1691 moved the role default from Sonnet 5 (arm A) to Sol (arm C)."""
    from personal_agent.config.model_loader import resolve_role_model_key

    assert resolve_role_model_key("artifact_builder") == harness.ARMS["C"]


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
        for i in range(1, 4)
    ]  # fmt: skip


def test_decision_is_no_decision_inside_the_noise_band() -> None:
    table = harness.decision_table(_rows({"A": 100.0, "A2": 110.0, "B": 105.0, "C": 150.0}))
    lat = {r["arm"]: r for r in table if r["measure"] == "median latency (s), all builds"}
    assert lat["B"]["verdict"] == "no decision"  # |5| <= noise band 10
    assert lat["C"]["verdict"] != "no decision"  # |50| > 10
    assert lat["B"]["noise_band"] == pytest.approx(10.0)


def _complete_rows() -> list[dict[str, Any]]:
    return [
        {"arm": a, "prompt_id": f"p{i}", "ok": True, "render_ok": True, "failure_reason": ""}
        for a in harness.ARMS
        for i in range(1, 4)
    ]


def _five_scores(labels: Mapping[str, Any]) -> dict[str, Any]:
    return {k: {q: 3 for q in harness.QUESTIONS} for k in labels}


def _labels_for(rows: list[dict[str, Any]]) -> dict[str, Any]:
    ok = [r for r in rows if r["ok"]]
    return {
        f"P{n:02d}": {"arm": r["arm"], "prompt_id": r["prompt_id"]} for n, r in enumerate(ok, 1)
    }


def test_report_accepts_a_complete_set() -> None:
    rows = _complete_rows()
    labels = _labels_for(rows)
    harness.check_complete(rows, labels, _five_scores(labels), n_prompts=3)


def test_report_fails_when_labels_do_not_match_the_successful_builds() -> None:
    rows = _complete_rows()
    labels = _labels_for(rows)
    labels["P01"] = {"arm": "A", "prompt_id": "p99"}
    with pytest.raises(harness.IncompleteRunError, match="labels"):
        harness.check_complete(rows, labels, _five_scores(labels), n_prompts=3)


@pytest.mark.parametrize(
    "defect", ["missing_row", "duplicate_row", "missing_score", "bad_score", "missing_question"]
)
def test_report_fails_on_incomplete_rows_or_scores(defect: str) -> None:
    rows = _complete_rows()
    labels = _labels_for(rows)
    scores: dict[str, Any] = _five_scores(labels)
    if defect == "missing_row":
        rows.pop()
    elif defect == "duplicate_row":
        rows[-1] = dict(rows[0])
    elif defect == "missing_score":
        scores.pop("P05")
    elif defect == "missing_question":
        del scores["P05"]["creative"]
    else:
        scores["P05"]["creative"] = 9
    with pytest.raises(harness.IncompleteRunError):
        harness.check_complete(rows, labels, scores, n_prompts=3)


def test_failed_build_without_reason_is_incomplete() -> None:
    rows = _complete_rows()
    rows[0].update(ok=False, failure_reason="")
    with pytest.raises(harness.IncompleteRunError, match="reason"):
        harness.check_complete(rows, {}, {}, n_prompts=3, need_scores=False)


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


def test_pilot_has_exactly_three_rich_prompts_and_twelve_builds() -> None:
    prompts = harness.load_prompts()
    assert [p.id for p in prompts] == ["p3", "p9", "p8"]
    assert len(prompts) * len(harness.ARMS) == 12
    assert all(len(p.request) > 600 for p in prompts), "no prompt may be simplified"
    assert sum(1 for p in prompts if p.fixture) == 1, "one prompt edits an existing page"


def test_edit_fixture_is_a_rich_interactive_page_that_fits_the_plan_limit() -> None:
    fixture = harness.HERE / "fixtures" / "edit_source.html"
    page = fixture.read_text(encoding="utf-8")
    controls = len(re.findall(r"<(?:input|select|button)\b", page))
    assert controls >= 8
    assert "<canvas" in page and "addEventListener" in page
    assert len(page) < 9000, "the plan carries the page and must stay under 16,000 characters"


def test_the_five_questions_are_the_owners() -> None:
    assert list(harness.QUESTIONS.values()) == [
        "The interactive widgets work",
        "It is creative",
        "It is easy to learn from",
        "It is attractive and enticing",
        "It encourages learning or continued use",
    ]


def _table(**per_arm: Mapping[str, str]) -> list[dict[str, Any]]:
    """A decision table with one verdict per (arm, measure)."""
    return [
        {"measure": m, "arm": arm, "verdict": v, "diff": 0.5}
        for arm, verdicts in per_arm.items()
        for m, v in verdicts.items()
    ]


def test_pilot_is_go_when_an_arm_beats_a_on_the_rating() -> None:
    table = _table(B={"owner: mean of five": "higher"}, C={"owner: mean of five": "no decision"})
    line = harness.pilot_verdict(table)
    assert line.startswith("Pilot verdict: GO") and "Arm B" in line


def test_pilot_is_go_on_cost_at_an_equal_rating() -> None:
    table = _table(C={"owner: mean of five": "no decision", "mean cost (usd), all builds": "lower"})
    assert harness.pilot_verdict(table).startswith("Pilot verdict: GO")


def test_pilot_is_no_go_when_nothing_clears_the_noise_band() -> None:
    table = _table(
        B={"owner: mean of five": "no decision", "mean cost (usd), all builds": "no decision"},
        C={"owner: mean of five": "lower", "mean cost (usd), all builds": "lower"},
    )
    line = harness.pilot_verdict(table)
    assert line.startswith("Pilot verdict: NO-GO") and "Sonnet 5" in line


def test_pilot_is_no_go_for_an_arm_that_loses_on_success_or_render() -> None:
    table = _table(B={"owner: mean of five": "higher", "success rate": "lower"})
    assert harness.pilot_verdict(table).startswith("Pilot verdict: NO-GO")


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Error code: 402 payment required", True),
        ("Your credit balance is too low to access the Anthropic API", True),
        ("insufficient_quota: You exceeded your current quota", True),
        ("billing_hard_limit_reached", True),
        ("generation_timeout after 600s", False),
        ("output ceiling reached", False),
    ],
)
def test_vendor_credit_errors_are_told_from_other_failures(text: str, expected: bool) -> None:
    assert harness.is_vendor_credit_error(text) is expected


@pytest.mark.asyncio
async def test_a_build_that_stops_at_the_output_ceiling_is_a_failure_with_that_reason(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Capped(_FakeClient):
        async def respond(self, **kw: Any) -> dict[str, Any]:
            out = await super().respond(**kw)
            out["usage"] = {"completion_tokens": 32768}
            return out

    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key",
        lambda *_a, **_k: _Capped("anthropic/claude-sonnet-5"),
    )
    row = await harness.build_one(_plan(), "A", skip_render=True)
    assert row["ok"] is False
    assert "output ceiling reached" in str(row["failure_reason"])


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


def _write_builds(run_dir: pathlib.Path, rows: list[dict[str, Any]]) -> None:
    (run_dir / "builds.jsonl").write_text(
        "".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8"
    )


@pytest.fixture
def run_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path) -> pathlib.Path:
    monkeypatch.setattr(harness, "OUT_DIR", tmp_path)
    return tmp_path


def test_bundle_refuses_an_incomplete_run(run_dir: pathlib.Path) -> None:
    _write_builds(run_dir, _complete_rows()[:-1])
    with pytest.raises(harness.IncompleteRunError):
        harness._cmd_bundle()
    assert not (run_dir / "key.json").exists()


def test_bundle_refuses_to_relabel_after_scores_exist(run_dir: pathlib.Path) -> None:
    rows = [{**r, "html_path": str(run_dir / "x.html")} for r in _complete_rows()]
    (run_dir / "x.html").write_text(GOOD_HTML, encoding="utf-8")
    _write_builds(run_dir, rows)
    (run_dir / "scores.json").write_text("{}", encoding="utf-8")
    with pytest.raises(harness.IncompleteRunError, match="stale"):
        harness._cmd_bundle()
    assert harness._cmd_bundle(force=True) == 0
    assert (run_dir / "key.json").exists()


def test_first_attempt_is_the_scored_one(run_dir: pathlib.Path) -> None:
    first = {"arm": "C", "prompt_id": "p4", "attempt": 1, "ok": False, "failure_reason": "timeout"}
    retry = {"arm": "C", "prompt_id": "p4", "attempt": 2, "ok": True, "failure_reason": ""}
    _write_builds(run_dir, [first, retry])
    scored = harness._scored_builds()
    assert len(scored) == 1 and scored[0]["ok"] is False


def test_render_html_does_not_raise_on_a_hung_check(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    def _hang(*_a: Any, **_k: Any) -> None:
        raise subprocess.TimeoutExpired(cmd="node", timeout=120)

    monkeypatch.setattr(subprocess, "run", _hang)
    result = harness.render_html(tmp_path / "p.html")
    assert result["render_ok"] is False
    assert "TimeoutExpired" in str(result["page_errors"])


def test_trace_check_rejects_a_non_uuid_before_any_sql() -> None:
    with pytest.raises(ValueError):
        harness.trace_hits("no-such-container", ["x'); drop table api_costs; --"])


def test_trace_check_with_no_ids_is_zero_without_a_query() -> None:
    assert harness.trace_hits("no-such-container", []) == {
        "budget_reservations": 0,
        "api_costs": 0,
    }


def test_lib_to_cdn_rewrites_mapped_assets_and_leaves_fonts() -> None:
    entries: list[Mapping[str, object]] = [
        {
            "lib_path": "lib/chartjs@4.4.7/chart.umd.js",
            "public_cdn_url": "https://cdn.test/chart.js",
        },
        {"lib_path": "lib/fonts/f@1/f.woff2", "public_cdn_url": None},
    ]
    page = (
        '<script src="https://art.test/lib/chartjs@4.4.7/chart.umd.js"></script>'
        '<style>@font-face{src:url("https://art.test/lib/fonts/f@1/f.woff2")}</style>'
    )
    out = harness.lib_to_cdn(page, "https://art.test/", entries)
    assert 'src="https://cdn.test/chart.js"' in out
    assert "https://art.test/lib/fonts/f@1/f.woff2" in out


@_NEEDS_BROWSER
def test_widget_check_operates_the_controls_of_a_working_interactive_page(
    tmp_path: pathlib.Path,
) -> None:
    page = tmp_path / "fixture.html"  # a copy: the check saves a screenshot beside the page
    shutil.copy(harness.HERE / "fixtures" / "edit_source.html", page)
    result = harness.render_html(page)
    w = cast(dict[str, Any], result["widgets"])
    assert result["render_ok"] is True
    assert w["found"] >= 10 and w["worked"] >= 7
    assert w["errors"] == 0 and w["with_errors"] == 0
    kinds = {c["kind"] for c in w["controls"]}
    assert {"range", "select", "checkbox", "text", "button"} <= kinds


@_NEEDS_BROWSER
def test_widget_check_flags_a_dead_button_and_a_slider_that_throws(tmp_path: pathlib.Path) -> None:
    page = tmp_path / "broken.html"
    page.write_text(
        "<!DOCTYPE html><html><body><h1>Widgets</h1>"
        "<p>Enough visible text on this page so that the blank test passes.</p>"
        '<output id="o">0</output>'
        "<button id=\"ok\" onclick=\"document.getElementById('o').textContent='clicked'\">Works</button>"
        '<button id="dead">Does nothing</button>'
        '<input id="bad" type="range" min="0" max="10" value="5" oninput="nope.missing()">'
        "</body></html>",
        encoding="utf-8",
    )
    result = harness.render_html(page)
    w = cast(dict[str, Any], result["widgets"])
    labels = {c["label"]: c for c in w["controls"]}
    assert w["found"] == 3
    assert labels["Works"]["reacted"] == "yes"
    assert labels["Does nothing"]["reacted"] == "no"
    assert w["with_errors"] >= 1 and w["errors"] >= 1
    assert result["render_ok"] is True, "errors raised by operating a control are not load errors"


def test_rating_pack_shows_the_five_questions_and_no_arm(tmp_path: pathlib.Path) -> None:
    page = tmp_path / "a.html"
    page.write_text(GOOD_HTML, encoding="utf-8")
    rows = [{"arm": "B", "prompt_id": "p3", "ok": True, "html_path": str(page)}]
    harness.write_rating_pack(rows, tmp_path / "r", tmp_path / "k.json")
    index = (tmp_path / "r" / "index.html").read_text(encoding="utf-8")
    assert all(text in index for text in harness.QUESTIONS.values())
    assert "claude_sonnet_5_5" not in index and ">B<" not in index


@pytest.mark.asyncio
async def test_a_plan_call_without_a_tool_call_keeps_its_evidence(
    builder_env: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _Cut:
        async def respond(self, **_: Any) -> dict[str, Any]:
            return {
                "content": "partial text",
                "tool_calls": [],
                "finish_reason": "length",
                "usage": {"completion_tokens": 24000},
                "cost_usd": 0.24,
            }

    monkeypatch.setattr(
        "personal_agent.llm_client.factory.get_llm_client_for_key", lambda *_a, **_k: _Cut()
    )
    with pytest.raises(RuntimeError, match="finish_reason=length"):
        await harness.make_plan(harness.load_prompts()[0])
    rows = harness._read_jsonl(builder_env / "plan-failures.jsonl")
    assert rows[0]["finish_reason"] == "length" and rows[0]["prompt_id"] == "p3"


def test_a_failed_plan_call_is_not_repeated_without_the_flag() -> None:
    prompts = harness.load_prompts()
    with pytest.raises(harness.IncompleteRunError, match="failed before"):
        harness.plans_to_run(prompts, done=set(), failed={"p9"}, retry_failed=False)
    todo = harness.plans_to_run(prompts, done={"p3"}, failed={"p9"}, retry_failed=True)
    assert [p.id for p in todo] == ["p9", "p8"]


def test_own_spend_counts_a_build_that_the_gate_did_not_record() -> None:
    now = datetime(2026, 10, 24, 18, 0, tzinfo=timezone.utc)
    builds: list[dict[str, Any]] = [
        {"at": "2026-10-24T16:30:00+00:00", "cost_usd": 0.34},
        {"at": "2026-10-24T16:35:00+00:00", "cost_usd": 0.34},
        {"at": "2026-10-23T10:00:00+00:00", "cost_usd": 0.50},  # yesterday: same week, not same day
        {"cost_usd": 9.0},  # no timestamp: ignored
    ]
    plans = [{"at": "2026-10-24T16:20:00+00:00", "cost_usd": 0.10}]
    spent = harness.own_spend(builds, plans, now)
    assert spent[("daily", "artifact_builder")] == Decimal("0.68")
    assert spent[("weekly", "artifact_builder")] == Decimal("1.18")
    assert spent[("daily", "study")] == Decimal("0.10")
    assert spent[("daily", "_total")] == Decimal("0.78")


@pytest.mark.parametrize(
    ("reason", "stop"),
    [
        ("ArtifactDraftError: BudgetDenied: daily cap", True),
        ("Your credit balance is too low to access the Anthropic API", True),
        ("output ceiling reached (32768 of 32768 tokens): HTML was empty", False),
        ("generation_timeout", False),
    ],
)
def test_budget_and_credit_stops_are_not_scored_failures(reason: str, stop: bool) -> None:
    assert harness.is_infrastructure_stop(reason) is stop


def test_a_failed_build_scores_one_on_every_question() -> None:
    rows: list[dict[str, Any]] = [
        {"arm": "A", "prompt_id": "p9", "ok": False, "failure_reason": "output ceiling reached"},
        {"arm": "C", "prompt_id": "p9", "ok": True, "render_ok": True},
    ]
    key: dict[str, dict[str, object]] = {"P01": {"arm": "C", "prompt_id": "p9"}}
    scores = {"P01": {q: 4 for q in harness.QUESTIONS}}
    harness.attach_scores(rows, key, scores)
    assert rows[0]["score"] == 1.0 and rows[0]["score_widgets"] == 1
    assert rows[1]["score"] == 4.0
    rows += [
        {**rows[0], "arm": "A2"},
        {**rows[1], "arm": "B"},
    ]
    assert harness._measure(rows, "A", "owner: mean of five") == 1.0
    assert harness._measure(rows, "C", "owner: mean of five") == 4.0


def test_running_spend_adds_this_commands_builds_to_the_start() -> None:
    start = {
        ("daily", "artifact_builder"): Decimal("0.39")
    }  # the gate's counter missed long builds
    own_start = {("daily", "artifact_builder"): Decimal("1.19")}
    own_now = {("daily", "artifact_builder"): Decimal("1.53"), ("daily", "_total"): Decimal("1.53")}
    now = harness.running_spend(start, own_start, own_now)
    assert now[("daily", "artifact_builder")] == Decimal("0.73")
    assert now[("daily", "_total")] == Decimal("1.53")


def _pilot_rows() -> list[dict[str, Any]]:
    """12 rows shaped like the pilot: p3 and p9 built only by C, p8 built by every arm."""
    rows: list[dict[str, Any]] = []
    for pid in ("p3", "p8", "p9"):
        for arm in harness.ARMS:
            ok = pid == "p8" or arm == "C"
            rows.append(
                {
                    "arm": arm, "prompt_id": pid, "ok": ok, "render_ok": True if ok else None,
                    "failure_reason": "" if ok else "output ceiling reached (32768 of 32768 tokens)",
                    "output_cap_hit": not ok, "latency_s": 100.0 if ok else 280.0,
                    "input_tokens": 5000, "output_tokens": 9000 if ok else 32768,
                    "reasoning_tokens": 3000 if arm != "C" else 150,
                    "cost_usd": 0.10 if ok else 0.34,
                    "controls_found": 16 if ok else None, "controls_worked": 14 if ok else None,
                    "controls_no_reaction": 2 if ok else None, "controls_unverifiable": 0 if ok else None,
                    "widget_errors": 0 if ok else None,
                }
            )  # fmt: skip
    return rows


def test_cost_and_latency_count_failed_builds_and_widgets_use_shared_prompts_only() -> None:
    rows = _pilot_rows()
    # A built one page of three and failed two at $0.34. C built all three at $0.10.
    assert harness._measure(rows, "A", "mean cost (usd), all builds") == pytest.approx(0.78 / 3)
    assert harness._measure(rows, "C", "mean cost (usd), all builds") == pytest.approx(0.10)
    assert harness.paired_prompts(rows) == {"p8"}
    assert harness._measure(rows, "A", "controls found (mean)" + harness.PAIRED) == 16
    assert harness._measure(rows, "C", "controls worked share" + harness.PAIRED) == pytest.approx(
        14 / 16
    )


def test_only_a_prompt_with_two_pages_is_a_blind_comparison() -> None:
    assert harness.blind_prompts(_pilot_rows()) == {"p8"}


def test_the_report_names_the_blind_ratings_shows_thinking_tokens_and_ends_with_the_verdict(
    builder_env: pathlib.Path,
) -> None:
    rows = _pilot_rows()
    ok = [r for r in rows if r["ok"]]
    key: dict[str, dict[str, object]] = {
        f"P{n:02d}": {"arm": r["arm"], "prompt_id": r["prompt_id"]} for n, r in enumerate(ok, 1)
    }
    scores = {k: {q: 4 for q in harness.QUESTIONS} for k in key}
    text = harness._report(rows, key, scores)
    assert "## Which ratings were blind" in text and "no, the only page" in text
    assert "Blind-only comparison (prompts p8" in text
    assert (
        "## Thinking tokens against the output ceiling" in text and "stopped at the ceiling" in text
    )
    assert text.rstrip().splitlines()[-1].startswith("Pilot verdict: ")


def test_a_go_line_names_every_qualifying_arm_strongest_first() -> None:
    table = _table(
        B={"owner: mean of five": "higher"},
        C={"owner: mean of five": "higher"},
    )
    table[1]["diff"] = 3.0  # C is stronger than B (0.5)
    line = harness.pilot_verdict(table)
    assert line.index("Arm C") < line.index("Arm B") and "Arm B (claude_sonnet_5_5) also" in line
