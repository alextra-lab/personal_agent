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
        "read_skill_invoked": [{"skill_name": "model-latency", "path": None}],
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
        if "event_type" in terms:
            sources = events.get(str(terms.get("event_type")), [])
        else:
            sources = events.get("__level_error__", []) if terms.get("level") == "ERROR" else []
        return httpx.Response(200, json={"hits": {"hits": [{"_source": s} for s in sources]}})

    return httpx.AsyncClient(transport=httpx.MockTransport(handle))


def chat_client(posted: list[httpx.Request]) -> httpx.AsyncClient:
    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        arm = "control" if request.url.port == 9002 else "treatment"
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": f"sess-{arm}"})
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


def scenario(sid: str = "model-latency#1") -> part2.Scenario:
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
    reviewer_excluded = {"system-metrics#1", "graph-inspection#3"}
    assert not reviewer_excluded & set(ids)
    assert all(s.prompt and s.expect and s.reason for s in scenarios)


def write_list(tmp_path: Path, ids: list[str]) -> Path:
    path = tmp_path / "list.yaml"
    path.write_text(yaml.safe_dump({"scenarios": [{"id": i, "reason": "r"} for i in ids]}))
    return path


@pytest.mark.parametrize(
    "bad", ["model-latency#99", "no-such-skill#0", "model-latency", "model-latency#x"]
)
def test_an_unknown_or_malformed_id_fails_loudly(tmp_path: Path, bad: str) -> None:
    with pytest.raises(ValueError):
        part2.load_scenarios(write_list(tmp_path, ["model-latency#1", bad]))


def test_a_repeated_id_fails(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="twice"):
        part2.load_scenarios(write_list(tmp_path, ["model-latency#1", "model-latency#1"]))


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
    assert treatment["skills_loaded"] == ["model-latency"]
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
        fields = {next(iter(f["term"])) for f in filters}
        assert fields in ({"trace_id", "event_type"}, {"trace_id", "level"})
    assert any(
        {"term": {"level": "ERROR"}} in q["body"]["query"]["bool"]["filter"] for q in turn_queries
    )  # type: ignore[index]


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
        json.dumps({"id": "model-latency#1", "arm": "control"})
        + "\n"
        + json.dumps({"id": "model-latency#1", "arm": "treatment", "error": "boom"})
        + "\n"
    )
    rows, posted = await run(
        tmp_path, GOOD, [scenario("model-latency#1"), scenario("model-latency#3")]
    )
    assert [(r["id"], r["arm"]) for r in rows] == [
        ("model-latency#1", "treatment"),
        ("model-latency#3", "treatment"),
        ("model-latency#3", "control"),
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


# ── FRE-1598: control sees the tools from call 1 (seeded reveal) ────────────

REVEAL = ("search_logs", "measure_model_latency")
BASE_TOOLS = ["search_memory", "bash", "read_skill"]


class FakeSeeder:
    """Stands in for postgres-eval: a dict of session id → revealed names."""

    def __init__(self, store_back: list[str] | None = None) -> None:
        self.stored: dict[str, list[str]] = {}
        self.store_back = store_back

    async def seed(self, session_id: str, names: tuple[str, ...] | list[str]) -> list[str]:
        self.stored[session_id] = sorted(names)
        return self.store_back if self.store_back is not None else self.stored[session_id]

    async def read(self, session_id: str) -> list[str]:
        return self.stored.get(session_id, [])


def tools_event(at: str, names: list[str], mode: str = "NORMAL") -> dict[str, object]:
    return {"@timestamp": at, "model_role": "primary", "tool_names": names, "mode": mode}


def reveal_events(arm: str, **extra: list[dict[str, object]]) -> dict[str, list[dict[str, object]]]:
    """A good turn of each arm under the seeded-reveal design, with overrides."""
    if arm == "control":
        events = turn_events(catalog_chars=0)
        events["tools_passed_to_llm"] = [
            tools_event("2026-10-13T10:00:01Z", BASE_TOOLS + list(REVEAL)),
            tools_event("2026-10-13T10:00:05Z", BASE_TOOLS + list(REVEAL)),
        ]
    else:
        events = turn_events(catalog_chars=900)
        events["tools_passed_to_llm"] = [
            tools_event("2026-10-13T10:00:01Z", BASE_TOOLS),
            tools_event("2026-10-13T10:00:05Z", BASE_TOOLS + ["search_logs"]),
        ]
        events["skill_tools_revealed"] = [
            {"@timestamp": "2026-10-13T10:00:03Z", "skill_name": "log-search", "tools": ["search_logs"]}
        ]
    events.update(extra)
    return events


def reveal_config(tmp_path: Path) -> part2.RunConfig:
    return part2.RunConfig(
        run_dir=tmp_path,
        model="qwen3.8-flash-next",
        arms=("control", "treatment"),
        settle_s=0.0,
        gap_s=0.0,
        control_reveal=REVEAL,
    )


async def run_reveal(
    tmp_path: Path,
    events_by_arm: Mapping[str, Mapping[str, list[dict[str, object]]]],
    seeder: FakeSeeder | None = None,
    chat: httpx.AsyncClient | None = None,
) -> tuple[list[dict[str, object]], list[httpx.Request]]:
    posted: list[httpx.Request] = []
    async with chat or chat_client(posted) as http, es_client(events_by_arm, []) as es:
        rows = await part2.run_part2(
            reveal_config(tmp_path),
            [scenario()],
            http=http,
            es=es,
            driver=FakeDriver(),
            seeder=seeder if seeder is not None else FakeSeeder(),
        )
    return rows, posted


GOOD_REVEAL = {"control": reveal_events("control"), "treatment": reveal_events("treatment")}


@pytest.mark.asyncio
async def test_seeded_reveal_creates_both_sessions_and_seeds_control_only(tmp_path: Path) -> None:
    seeder = FakeSeeder()
    rows, posted = await run_reveal(tmp_path, GOOD_REVEAL, seeder)
    by_arm = {r["arm"]: r for r in rows}
    assert all("error" not in r for r in rows), rows
    assert [r.url.path for r in posted] == ["/sessions", "/chat", "/sessions", "/chat"]
    chats = [dict(r.url.params) for r in posted if r.url.path == "/chat"]
    assert [c["session_id"] for c in chats] == ["sess-control", "sess-treatment"]
    assert seeder.stored == {"sess-control": sorted(REVEAL)}
    assert by_arm["control"]["revealed_before"] == sorted(REVEAL)
    assert by_arm["treatment"]["revealed_before"] == []
    assert by_arm["control"]["primary_tool_counts"] == [5, 5]
    assert by_arm["treatment"]["prompt_sha256"] == part2.sha256_text("prompt")
    assert by_arm["treatment"]["expect_sha256"] == part2.sha256_text("expect")


@pytest.mark.asyncio
async def test_a_seed_that_does_not_read_back_is_an_error_row(tmp_path: Path) -> None:
    rows, _ = await run_reveal(tmp_path, GOOD_REVEAL, FakeSeeder(store_back=["search_logs"]))
    by_arm = {r["arm"]: r for r in rows}
    assert "holds revealed tools" in str(by_arm["control"]["error"])
    assert "error" not in by_arm["treatment"]


@pytest.mark.asyncio
async def test_a_treatment_session_that_starts_revealed_is_an_error_row(tmp_path: Path) -> None:
    seeder = FakeSeeder()
    seeder.stored["sess-treatment"] = ["search_logs"]
    rows, _ = await run_reveal(tmp_path, GOOD_REVEAL, seeder)
    assert "holds revealed tools" in str({r["arm"]: r for r in rows}["treatment"]["error"])


@pytest.mark.asyncio
async def test_control_whose_first_call_lacks_a_seeded_tool_is_an_error_row(tmp_path: Path) -> None:
    """Seeded negative: the seed read back, but the model never saw the tool."""
    events = dict(GOOD_REVEAL)
    events["control"] = reveal_events(
        "control", tools_passed_to_llm=[tools_event("2026-10-13T10:00:01Z", BASE_TOOLS + ["search_logs"])]
    )
    rows, _ = await run_reveal(tmp_path, events)
    assert "lack a seeded tool" in str({r["arm"]: r for r in rows}["control"]["error"])


@pytest.mark.asyncio
async def test_treatment_whose_first_call_lists_a_tool_is_an_error_row(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["treatment"] = reveal_events(
        "treatment", tools_passed_to_llm=[tools_event("2026-10-13T10:00:01Z", BASE_TOOLS + ["search_logs"])]
    )
    rows, _ = await run_reveal(tmp_path, events)
    assert "first primary call lists" in str({r["arm"]: r for r in rows}["treatment"]["error"])


@pytest.mark.asyncio
async def test_treatment_call_after_a_reveal_without_the_tool_is_an_error_row(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["treatment"] = reveal_events(
        "treatment",
        tools_passed_to_llm=[
            tools_event("2026-10-13T10:00:01Z", BASE_TOOLS),
            tools_event("2026-10-13T10:00:05Z", BASE_TOOLS),
        ],
    )
    rows, _ = await run_reveal(tmp_path, events)
    assert "lacks revealed ['search_logs']" in str({r["arm"]: r for r in rows}["treatment"]["error"])


@pytest.mark.asyncio
async def test_a_call_outside_normal_mode_is_an_error_row(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["control"] = reveal_events(
        "control",
        tools_passed_to_llm=[tools_event("2026-10-13T10:00:01Z", BASE_TOOLS + list(REVEAL), "LOCKDOWN")],
    )
    rows, _ = await run_reveal(tmp_path, events)
    assert "not NORMAL" in str({r["arm"]: r for r in rows}["control"]["error"])


@pytest.mark.asyncio
async def test_a_turn_without_a_primary_tool_list_is_an_error_row(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["control"] = reveal_events("control", tools_passed_to_llm=[])
    rows, _ = await run_reveal(tmp_path, events)
    assert "no tools_passed_to_llm" in str({r["arm"]: r for r in rows}["control"]["error"])


@pytest.mark.asyncio
async def test_an_orchestrator_failure_is_an_error_row(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["treatment"] = reveal_events(
        "treatment", __level_error__=[{"event_type": "orchestrator_call_failed"}]
    )
    rows, _ = await run_reveal(tmp_path, events)
    assert "orchestrator_call_failed" in str({r["arm"]: r for r in rows}["treatment"]["error"])


@pytest.mark.asyncio
async def test_the_generic_failure_reply_is_an_error_row(tmp_path: Path) -> None:
    posted: list[httpx.Request] = []

    def handle(request: httpx.Request) -> httpx.Response:
        posted.append(request)
        if request.url.path == "/sessions":
            return httpx.Response(200, json={"session_id": f"sess-{request.url.port}"})
        reply = f"{part2.GENERIC_FAILURE_REPLY}. Error ID: ab12cd34"
        return httpx.Response(200, json={"session_id": "sess", "trace_id": "t", "response": reply})

    chat = httpx.AsyncClient(transport=httpx.MockTransport(handle))
    rows, _ = await run_reveal(tmp_path, GOOD_REVEAL, chat=chat)
    assert all("generic failure reply" in str(r["error"]) for r in rows)


@pytest.mark.asyncio
async def test_refusals_and_error_events_are_recorded_not_fatal(tmp_path: Path) -> None:
    events = dict(GOOD_REVEAL)
    events["treatment"] = reveal_events(
        "treatment",
        tool_call_completed=[
            {"tool_name": "search_logs", "success": False, "error_code": "tool_not_revealed", "executed": False},
            {"tool_name": "bash", "success": False, "error_code": "tool_execution_failed"},
            {"tool_name": "read_skill", "success": True},
        ],
        __level_error__=[{"event_type": "tool_call_failed"}],
    )
    rows, _ = await run_reveal(tmp_path, events)
    treatment = {r["arm"]: r for r in rows}["treatment"]
    assert "error" not in treatment
    assert treatment["tool_not_revealed"] == 1
    assert treatment["error_events"] == ["tool_call_failed"]


@pytest.mark.asyncio
async def test_control_reveal_without_a_seeder_is_an_error_row(tmp_path: Path) -> None:
    async with chat_client([]) as http, es_client(GOOD_REVEAL, []) as es:
        rows = await part2.run_part2(
            reveal_config(tmp_path), [scenario()], http=http, es=es, driver=FakeDriver()
        )
    assert all("no RevealSeeder" in str(r["error"]) for r in rows)


def test_without_control_reveal_no_session_is_created(tmp_path: Path) -> None:
    """The FRE-1590 mode is unchanged: /chat creates the session itself."""
    assert part2.RunConfig(tmp_path, "m", ("control",)).control_reveal == ()
