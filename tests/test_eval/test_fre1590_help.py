"""FRE-1590 Part 2 harness (scripts/eval/fre1590/help.py), offline: fakes only, no network."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

import httpx
import pytest
import yaml
from scripts.eval.fre1337_intent_probe.behavioral import EVAL_ES_INDEX, EVAL_ES_URL
from scripts.eval.fre1590 import help as part2

OVERRIDE = part2.HERE / "docker-compose.fre1590.yml"


# ── fakes ────────────────────────────────────────────────────────────────────


class FakeResult:
    def __init__(self, nodes: int) -> None:
        self._nodes = nodes

    async def single(self) -> dict[str, int]:
        return {"n": self._nodes}


class FakeSession:
    def __init__(self, driver: FakeDriver) -> None:
        self._driver = driver

    async def __aenter__(self) -> FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def run(self, cypher: str) -> FakeResult:
        self._driver.cyphers.append(cypher)
        return FakeResult(self._driver.nodes)


class FakeDriver:
    def __init__(self, nodes: int = 0) -> None:
        self.nodes = nodes
        self.cyphers: list[str] = []

    def session(self) -> FakeSession:
        return FakeSession(self)


def turn_events(catalog_chars: int = 0, completed: int = 1) -> dict[str, list[dict[str, object]]]:
    return {
        "model_call_completed": [{"model": "m", "role": "primary"}] * completed,
        "skill_index_assembled": [{"index_chars": catalog_chars}],
        "read_skill_invoked": [{"skill_name": "query-tempo", "path": None}],
        "tool_call_started": [{"tool_name": "read_skill"}, {"tool_name": "bash"}],
        "tool_call_failed": [{"tool_name": "bash"}],
        "permission_evaluated": [{"decision": "allow"}, {"decision": "deny"}],
        "api_cost_recorded": [{"cost_usd": 0.25}, {"cost_usd": 0.5}, {"provider": "slm_local"}],
        "constraint_pause_emitted": [],
        "force_synthesis_injected": [{"iteration": 6}],
    }


def es_client(
    events_by_arm: Mapping[str, Mapping[str, list[dict[str, object]]]],
    seen: list[dict[str, object]],
) -> httpx.AsyncClient:
    """An eval-ES fake. The trace id names the arm (``trace-<arm>``)."""

    def handle(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        seen.append({"url": str(request.url), "body": body})
        filters = body.get("query", {}).get("bool", {}).get("filter", [])
        terms = {k: v for f in filters if "term" in f for k, v in f["term"].items()}
        events = events_by_arm.get(str(terms.get("trace_id", "")).removeprefix("trace-"), {})
        sources = events.get(str(terms.get("event_type")), []) if "event_type" in terms else []
        return httpx.Response(200, json={"hits": {"hits": [{"_source": s} for s in sources]}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def chat_client(posted: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        arm = "control" if request.url.port == 9002 else "treatment"
        return httpx.Response(
            200,
            json={
                "session_id": "s-1",
                "trace_id": f"trace-{arm}",
                "response": f"reply from {arm}",
                "primary_selection": {"model": "qwen3.8-flash-next"},
            },
        )

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def config(tmp_path: Path) -> part2.RunConfig:
    return part2.RunConfig(
        run_dir=tmp_path,
        model="qwen3.8-flash-next",
        arms=("control", "treatment"),
        settle_s=0.0,
        gap_s=0.0,
    )


def scenario(sid: str = "query-tempo#1") -> part2.Scenario:
    return part2.Scenario(sid, sid.partition("#")[0], "prompt", "expect", "reason")


async def run(
    tmp_path: Path,
    events_by_arm: Mapping[str, Mapping[str, list[dict[str, object]]]],
    scenarios: list[part2.Scenario],
    seen: list[dict[str, object]] | None = None,
    driver: FakeDriver | None = None,
) -> tuple[list[dict[str, object]], list[httpx.Request]]:
    posted: list[httpx.Request] = []
    async with (
        chat_client(posted) as http,
        es_client(events_by_arm, seen if seen is not None else []) as es,
    ):
        rows = await part2.run_part2(
            config(tmp_path), scenarios, http=http, es=es, driver=driver or FakeDriver()
        )
    return rows, posted


GOOD = {"control": turn_events(catalog_chars=0), "treatment": turn_events(catalog_chars=900)}


# ── scenario selection ───────────────────────────────────────────────────────


def test_the_part2_list_resolves_and_leaves_out_the_excluded_rows() -> None:
    scenarios = part2.load_scenarios()
    ids = [s.id for s in scenarios]
    assert len(ids) == len(set(ids))
    assert not [i for i in ids if i.startswith("sequential-thinking#")]
    reviewer_excluded = {
        "query-elasticsearch#0",
        "query-elasticsearch#1",
        "query-elasticsearch#2",
        "query-tempo#0",
        "system-metrics#1",
        "neo4j-direct#3",
    }
    assert not reviewer_excluded & set(ids)
    assert all(s.prompt and s.expect and s.reason for s in scenarios)


def write_list(tmp_path: Path, ids: list[str]) -> Path:
    path = tmp_path / "list.yaml"
    path.write_text(yaml.safe_dump({"scenarios": [{"id": i, "reason": "r"} for i in ids]}))
    return path


@pytest.mark.parametrize(
    "bad", ["query-tempo#99", "no-such-skill#0", "query-tempo", "query-tempo#x"]
)
def test_an_unknown_or_malformed_id_fails_loudly(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        part2.load_scenarios(write_list(tmp_path, ["query-tempo#1", bad]))


def test_a_repeated_id_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice"):
        part2.load_scenarios(write_list(tmp_path, ["query-tempo#1", "query-tempo#1"]))


def test_select_by_ids_keeps_list_order_and_refuses_unknown_ids() -> None:
    scenarios = [scenario("a#0"), scenario("b#0"), scenario("c#0")]
    assert [s.id for s in part2.select(scenarios, ["c#0", "a#0"], 0)] == ["a#0", "c#0"]
    assert [s.id for s in part2.select(scenarios, [], 2)] == ["a#0", "b#0"]
    with pytest.raises(ValueError, match="z#9"):
        part2.select(scenarios, ["a#0", "z#9"], 0)


def test_arms_parse_and_alternate() -> None:
    assert part2.parse_arms("treatment") == ("treatment",)
    for bad in ("", "control,control", "prod"):
        with pytest.raises(ValueError):
            part2.parse_arms(bad)
    assert part2.arm_order(("control", "treatment"), 0) == ("control", "treatment")
    assert part2.arm_order(("control", "treatment"), 1) == ("treatment", "control")


# ── the turn ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_turn_row_carries_the_reply_and_the_counts(tmp_path: Path) -> None:
    driver = FakeDriver(nodes=0)
    rows, posted = await run(tmp_path, GOOD, [scenario()], driver=driver)
    assert [r["arm"] for r in rows] == ["control", "treatment"]
    params = dict(posted[0].url.params)
    assert params == {"message": "prompt", "channel": "EVAL", "model": "qwen3.8-flash-next"}
    assert "session_id" not in params
    treatment = rows[1]
    assert "error" not in treatment
    assert treatment["reply"] == "reply from treatment"
    assert treatment["expect"] == "expect"
    assert treatment["skills_loaded"] == ["query-tempo"]
    assert treatment["tool_calls"] == ["read_skill", "bash"]
    assert treatment["tool_failures"] == 1
    assert treatment["permission_denials"] == 1
    assert treatment["constraint_pauses"] == 0
    assert treatment["force_synthesis"] == 1
    assert treatment["model_calls"] == 1
    assert treatment["cost_usd"] == 0.75
    assert treatment["graph_nodes_after"] == 0
    # A wipe before each turn, a node count after each.
    assert driver.cyphers.count("MATCH (n) DETACH DELETE n") == 2
    assert driver.cyphers.count(part2.GRAPH_COUNT_CYPHER) == 2
    lines = part2.rows_path(tmp_path, "qwen3.8-flash-next").read_text().splitlines()
    assert len(lines) == 2


@pytest.mark.asyncio
async def test_the_es_query_uses_the_production_field_shape(tmp_path: Path) -> None:
    seen: list[dict[str, object]] = []
    await run(tmp_path, GOOD, [scenario()], seen=seen)
    turn_queries = [q for q in seen if "trace-" in json.dumps(q["body"])]
    assert turn_queries
    for q in turn_queries:
        assert q["url"] == f"{EVAL_ES_URL}/{EVAL_ES_INDEX}/_search"
        text = json.dumps(q["body"])
        assert ".keyword" not in text
        filters = q["body"]["query"]["bool"]["filter"]  # type: ignore[index]
        assert {next(iter(f["term"])) for f in filters} == {"trace_id", "event_type"}


@pytest.mark.asyncio
async def test_a_turn_without_a_model_call_is_an_error_row(tmp_path: Path) -> None:
    events = {"control": turn_events(completed=0), "treatment": turn_events(900, completed=0)}
    rows, _ = await run(tmp_path, events, [scenario()])
    assert all("no model_call_completed" in str(r["error"]) for r in rows)
    assert all("skills_loaded" not in r for r in rows)


@pytest.mark.asyncio
async def test_a_control_turn_that_saw_a_catalog_is_an_error_row(tmp_path: Path) -> None:
    events = {"control": turn_events(catalog_chars=900), "treatment": turn_events(900)}
    rows, _ = await run(tmp_path, events, [scenario()])
    by_arm = {r["arm"]: r for r in rows}
    assert "saw a skill catalog" in str(by_arm["control"]["error"])
    assert "error" not in by_arm["treatment"]


@pytest.mark.asyncio
async def test_a_treatment_turn_without_a_catalog_is_an_error_row(tmp_path: Path) -> None:
    events = {"control": turn_events(0), "treatment": turn_events(0)}
    rows, _ = await run(tmp_path, events, [scenario()])
    assert "saw no skill catalog" in str({r["arm"]: r for r in rows}["treatment"]["error"])


@pytest.mark.asyncio
async def test_resume_skips_done_pairs_and_reruns_error_rows(tmp_path: Path) -> None:
    out = part2.rows_path(tmp_path, "qwen3.8-flash-next")
    out.parent.mkdir(parents=True)
    out.write_text(
        json.dumps({"id": "query-tempo#1", "arm": "control"})
        + "\n"
        + json.dumps({"id": "query-tempo#1", "arm": "treatment", "error": "boom"})
        + "\n"
    )
    rows, posted = await run(tmp_path, GOOD, [scenario("query-tempo#1"), scenario("query-tempo#3")])
    assert [(r["id"], r["arm"]) for r in rows] == [
        ("query-tempo#1", "treatment"),
        ("query-tempo#3", "treatment"),
        ("query-tempo#3", "control"),
    ]
    assert len(posted) == 3


# ── the report ───────────────────────────────────────────────────────────────


def test_the_report_pairs_the_arms_and_flags_a_one_arm_scenario() -> None:
    rows = [
        {"id": "a#0", "arm": "control", "trace_id": "t1", "skills_loaded": []},
        {"id": "a#0", "arm": "treatment", "trace_id": "t2", "skills_loaded": ["a"]},
        {"id": "b#0", "arm": "control", "trace_id": "t3", "graph_nodes_after": 4},
        {"id": "b#0", "arm": "treatment", "error": "boom"},
    ]
    report = part2.build_report(rows, "m")
    assert report["scenarios_paired"] == 1
    assert report["one_arm"] == ["b#0"]
    assert report["errored"] == ["b#0/treatment"]
    assert report["graph_writes"] == ["b#0/control"]
    assert report["paired"] is False
    first = report["scenarios"][0]  # type: ignore[index]
    assert first["treatment_loaded_skill"] is True
    assert first["arms"]["control"]["trace_id"] == "t1"
    text = part2.render_report(report)
    assert "one arm only (AC-3 fails): b#0" in text
    assert "NOT PAIRED" in text


def test_a_fully_paired_report_passes() -> None:
    rows = [{"id": "a#0", "arm": arm, "trace_id": arm} for arm in ("control", "treatment")]
    assert part2.build_report(rows, "m")["paired"] is True
    assert part2.build_report(rows, "m", ["a#0"])["paired"] is True


def test_a_scenario_with_no_row_is_unrun_and_not_paired() -> None:
    """Seeded negative: a ``--limit 1`` run, or one that stopped early, must not report PAIRED."""
    rows = [{"id": "a#0", "arm": arm, "trace_id": arm} for arm in ("control", "treatment")]
    report = part2.build_report(rows, "m", ["a#0", "b#1"])
    assert report["unrun"] == ["b#1"] and report["paired"] is False
    assert "in the scenario list, not run: b#1" in part2.render_report(report)


# ── the compose override ─────────────────────────────────────────────────────


def test_the_override_masks_every_skill_and_keeps_the_scripts() -> None:
    services = yaml.safe_load(OVERRIDE.read_text())["services"]
    control = services["seshat-gateway-control"]
    targets = {v["target"] for v in control["volumes"]}
    skills = {f"/app/skills/{p.parent.name}/SKILL.md" for p in part2.SKILLS_DIR.glob("*/SKILL.md")}
    assert skills and targets == skills
    assert all(v["source"] == "/dev/null" and v["read_only"] for v in control["volumes"])
    assert "tmpfs" not in control
    assert "volumes" not in services["seshat-gateway-treatment"]


def test_the_override_sets_the_same_run_values_on_both_arms() -> None:
    services = yaml.safe_load(OVERRIDE.read_text())["services"]
    for name in ("seshat-gateway-control", "seshat-gateway-treatment"):
        env = services[name]["environment"]
        assert env["AGENT_ENABLE_SECOND_BRAIN"] == "false"
        assert env["AGENT_CONSTRAINT_PAUSE_TIMEOUT_SECONDS"] == "5"
        assert env["AGENT_TEMPO_URL"] == "http://tempo:3200"
        assert env["AGENT_GROUNDING_VERIFICATION_MODE"] == "off"
        for key in (
            "AGENT_EXPANSION_ENABLED",
            "AGENT_SESSION_SUMMARY_ENABLED",
            "AGENT_SLM_HEALTH_GENERATION_CHECK_ENABLED",
            "AGENT_SLM_TUNNEL_BASE_URL",
            "AGENT_USER_DISPLAY_NAMES_JSON",
        ):
            assert env[key] == f"${{{key}}}", f"{name}: {key} must come from the env file"
        assert "AGENT_AGENT_ID" not in env
    control = services["seshat-gateway-control"]["environment"]
    assert control["AGENT_PRIMITIVE_TOOLS_ENABLED"] == "true"
    assert control["AGENT_APPROVAL_UI_ENABLED"] == "false"
