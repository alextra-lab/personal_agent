"""AC-3's guard: the wipe/drive targets are hardcoded, never configurable to prod.

`wipe_eval_graph` and `assert_eval_chat_url` must refuse anything but the isolated eval
Neo4j/gateway (a literal string check, not the `Environment` enum — see substrate.py's
module docstring for why). `find_cross_session_sources` is the pure logic behind AC-3's
"prove it" requirement.
"""

from __future__ import annotations

from typing import Any

import pytest
from scripts.eval.fre1337_intent_probe.substrate import (
    EVAL_CHAT_BASE_URL,
    EVAL_NEO4J_URI,
    WIPE_CYPHER,
    SubstrateGuardError,
    assert_eval_chat_url,
    find_cross_session_sources,
    wipe_eval_graph,
)


class _FakeSession:
    def __init__(self) -> None:
        self.queries: list[str] = []

    async def run(self, query: str) -> None:
        self.queries.append(query)

    async def __aenter__(self) -> "_FakeSession":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None


class _FakeDriver:
    def __init__(self) -> None:
        self.fake_session = _FakeSession()

    def session(self) -> _FakeSession:
        return self.fake_session


def test_wipe_cypher_is_unscoped_full_wipe() -> None:
    """No WHERE clause — a scoped delete is exactly the hazard the plan-review flagged
    (misses :Session-only nodes, risks orphaning cross-session-adopted entities).
    """
    assert WIPE_CYPHER == "MATCH (n) DETACH DELETE n"
    assert "WHERE" not in WIPE_CYPHER


@pytest.mark.asyncio
async def test_wipe_eval_graph_runs_against_the_allowed_uri() -> None:
    driver = _FakeDriver()
    await wipe_eval_graph(driver, uri=EVAL_NEO4J_URI)
    assert driver.fake_session.queries == [WIPE_CYPHER]


@pytest.mark.parametrize(
    "bad_uri",
    [
        "bolt://localhost:7687",  # production  # fre-375-allow: negative case — asserts the guard REFUSES this URI, never connects
        "bolt://localhost:7688",  # FRE-375 test stack, different substrate
        "bolt://neo4j-eval:7687",  # in-container form — host callers must use :7689
        "",
    ],
)
@pytest.mark.asyncio
async def test_wipe_eval_graph_refuses_any_other_uri(bad_uri: str) -> None:
    driver = _FakeDriver()
    with pytest.raises(SubstrateGuardError):
        await wipe_eval_graph(driver, uri=bad_uri)
    assert driver.fake_session.queries == []


def test_assert_eval_chat_url_accepts_the_eval_gateway() -> None:
    assert_eval_chat_url(EVAL_CHAT_BASE_URL)  # must not raise


@pytest.mark.parametrize(
    "bad_url",
    ["http://localhost:9001", "http://localhost:9000", "http://localhost:9004", ""],
)
def test_assert_eval_chat_url_refuses_any_other_url(bad_url: str) -> None:
    """Anything outside `EVAL_ARMS` is refused — production above all.

    FRE-1350 removed `http://localhost:9003` from this list because the treatment
    gateway became a legitimate arm, NOT because the guard was loosened. 9001 is
    production's gateway and stays here: that is the case this test exists for, and it
    must keep failing loudly. 9004 replaces 9003 to keep an unknown-port case.
    """
    with pytest.raises(SubstrateGuardError):
        assert_eval_chat_url(bad_url)


@pytest.mark.parametrize("arm_url", ["http://localhost:9002", "http://localhost:9003"])
def test_assert_eval_chat_url_admits_both_eval_arms(arm_url: str) -> None:
    """FRE-1350: control AND treatment are both drivable.

    Arm 3 originally drove only control, whose primitives are disabled — the
    `tool_use_request` fixture scored 0 tool calls for want of `bash` rather than for
    anything about intent routing. Measured: control 10 tools, treatment 15,
    production 22.
    """
    assert_eval_chat_url(arm_url)


def test_eval_arms_maps_names_to_the_guarded_urls() -> None:
    """The arm map and the guard cannot drift apart — the guard reads the same mapping."""
    from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS

    assert EVAL_ARMS == {
        "control": "http://localhost:9002",
        "treatment": "http://localhost:9003",
    }


def test_find_cross_session_sources_empty_when_control_held() -> None:
    session_2_sources: list[dict[str, Any]] = [
        {"name": "X", "originating_session_id": "session-2"},
        {"name": "Y", "originating_session_id": "session-2"},
    ]
    assert find_cross_session_sources(session_2_sources, "session-1") == []


def test_find_cross_session_sources_flags_contamination() -> None:
    session_2_sources: list[dict[str, Any]] = [
        {"name": "SafeCart", "originating_session_id": "session-1"},
        {"name": "EaseCert", "originating_session_id": "session-1"},
        {"name": "Z", "originating_session_id": "session-2"},
    ]
    leaked = find_cross_session_sources(session_2_sources, "session-1")
    assert {r["name"] for r in leaked} == {"SafeCart", "EaseCert"}
