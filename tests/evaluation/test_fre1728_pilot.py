"""FRE-1728 AC-1 and AC-4: the MemAudit adapter and the pilot loop, with fakes for MemAudit."""

from __future__ import annotations

import ast
import json
import sys
import types
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

import httpx
import pytest

_DIR = Path(__file__).resolve().parents[2] / "scripts" / "eval" / "fre1728"
_MEMAUDIT_SIDE = ("memaudit_adapter.py", "memaudit_driver.py", "defect_rank.py", "scout_scan.py")

sys.path.insert(0, str(_DIR))


class _MemoryAgent(ABC):
    """The abstract methods of MemAudit's ``base_agent.MemoryAgent`` at the pinned commit."""

    @abstractmethod
    def __call__(self, task: str, history: list[dict[str, str]]) -> str: ...

    @abstractmethod
    def get_all_memories(self) -> list[dict[str, str]]: ...

    @abstractmethod
    def save_memories(self, path: str) -> None: ...

    @abstractmethod
    def reset(self) -> None: ...


_base_agent = types.ModuleType("base_agent")
_base_agent.MemoryAgent = _MemoryAgent  # type: ignore[attr-defined]
sys.modules.setdefault("base_agent", _base_agent)

import defect_rank  # type: ignore[import-not-found]  # noqa: E402
import memaudit_adapter  # type: ignore[import-not-found]  # noqa: E402
import memaudit_driver  # type: ignore[import-not-found]  # noqa: E402


class _FakeBridge:
    """A transport that answers as the bridge does, with a turn limit that acts as the spend cap."""

    def __init__(self, turns_before_cap: int = 1000) -> None:
        self.turns: list[dict[str, Any]] = []
        self.wipes = 0
        self.turns_before_cap = turns_before_cap
        self.memory_rows = [{"kind": "entity", "name": "n", "content": "fact in graph", "type": ""}]
        self.settle_body: dict[str, Any] = {
            "waited_s": 0.0,
            "missing_turns": [],
            "budget_denied": 0,
        }

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/turn":
            if len(self.turns) >= self.turns_before_cap:
                return httpx.Response(402, json={"stop": True})
            body = json.loads(request.content)
            self.turns.append(body)
            n = len(self.turns)
            return httpx.Response(
                200,
                json={
                    "session_id": body["session_id"] or f"s{n}",
                    "trace_id": f"t{n}",
                    "response": f"reply {n}",
                },
            )
        if path == "/wipe":
            self.wipes += 1
            return httpx.Response(200, json={"waited_s": 0.0, "residual_memories": []})
        if path == "/settle":
            return httpx.Response(200, json=self.settle_body)
        if path == "/memories":
            return httpx.Response(200, json=self.memory_rows)
        if path == "/spend":
            return httpx.Response(200, json={"spent_usd": 1.0, "cap_usd": 45.0, "stop": False})
        return httpx.Response(404)


def _bridge(fake: _FakeBridge) -> Any:
    return memaudit_adapter.BridgeClient("http://bridge", transport=httpx.MockTransport(fake))


def _task(category: str, dimension: str) -> dict[str, str]:
    return {"task": f"task {dimension}", "target_category": category, "target_dimension": dimension}


_TASKS = [
    _task("skill_memory", "skill_a"),
    _task("skill_memory", "skill_b"),
    _task("knowledge_memory", "know_a"),
    _task("episodic_memory", "epi_a"),
    _task("self_model", "self_a"),
    _task("assistance_preference", "pref_a"),
]


class _FakeMemAudit:
    """MemAudit's parts, with scores set per test."""

    def __init__(self, *, graph: float = 1.0, answer: float = 0.0, reason: str = "why") -> None:
        self.graph = graph
        self.answer = answer
        self.reason = reason
        self.attributed: list[dict[str, Any]] = []

    def load_user(self, _user_id: str) -> dict[str, Any]:
        bank: dict[str, list[dict[str, str]]] = {}
        for task in _TASKS:
            bank.setdefault(task["target_category"], []).append(
                {"dimension": task["target_dimension"], "short": "s", "explanation": "e"}
            )
        return {"memory_bank": bank}

    def load_tasks(self, _user_id: str) -> list[dict[str, str]]:
        return list(_TASKS)

    def run_episode(self, _user: str, task: str, agent: Any, max_turns: int) -> dict[str, Any]:
        reply = agent(task, [])
        return {
            "task": task,
            "turns": [
                {"turn": 1, "agent_response": reply, "user_simulator": {"text": "last message"}}
            ],
        }

    def slot_fill(self, _memories: str, _category: str, _dimension: str) -> tuple[str, str]:
        return "label", "reason"

    def judge(self, _d: str, _e: str, predicted: str, _gt: str) -> tuple[float, str]:
        score = self.graph if predicted == "label" else self.answer
        return score, self.reason

    def attribute(
        self, record: dict[str, Any], _run: str, _user: str, _tasks: Any, _oracle: Any
    ) -> dict[str, str]:
        self.attributed.append(record)
        return {"category": "memory_failure"}


def _config(tmp_path: Path, runs: tuple[str, ...] = ("A", "B"), tasks: int = 2) -> Any:
    return memaudit_driver.PilotConfig(
        runs=runs,
        users=("user_x",),
        tasks_per_user=tasks,
        max_turns=6,
        run_dir=tmp_path,
        severity={"memory_failure": 2.0},
    )


# ── AC-1: the adapter ─────────────────────────────────────────────────────────────────────────


def test_ac1_adapter_is_a_memory_agent_and_starts_a_new_session_per_task() -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    assert isinstance(agent, _MemoryAgent)
    assert agent("task one", []) == "reply 1"
    assert agent("task two", []) == "reply 2"
    assert [t["session_id"] for t in fake.turns] == [None, None]


def test_ac1_later_turn_sends_only_the_latest_user_message() -> None:
    """``Episode`` replays the task and the dialogue. The gateway holds the session already."""
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    agent("the task", [])
    agent(
        "the task\n\n[Conversation so far]\nAgent: reply 1\nUser: more detail",
        [{"agent": "reply 1", "user": "more detail"}],
    )

    assert fake.turns[1] == {"message": "more detail", "session_id": "s1"}


def test_ac1_get_all_memories_reads_the_graph_through_the_bridge(tmp_path: Path) -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    assert agent.get_all_memories()[0]["content"] == "fact in graph"
    agent.save_memories(str(tmp_path / "m.json"))
    assert json.loads((tmp_path / "m.json").read_text())["num_memories"] == 1


def test_ac1_empty_simulated_message_stops_the_run() -> None:
    agent = memaudit_adapter.SeshatAgent(_bridge(_FakeBridge()))

    with pytest.raises(memaudit_adapter.HarnessFailure):
        agent("task", [{"agent": "a", "user": "  "}])


def test_ac1_ask_uses_a_fresh_session_and_keeps_the_task_session() -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    agent("task", [])
    agent.ask("question")

    assert fake.turns[1]["session_id"] is None


@pytest.mark.parametrize("name", _MEMAUDIT_SIDE)
def test_memaudit_side_imports_no_project_module(name: str) -> None:
    """MemAudit's environment holds the OpenAI client only, so a project import cannot resolve."""
    tree = ast.parse((_DIR / name).read_text())

    for node in ast.walk(tree):
        modules: list[str] = []
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            modules = [node.module or ""]
        for module in modules:
            assert not module.startswith(("personal_agent", "scripts")), module


# ── The loop ──────────────────────────────────────────────────────────────────────────────────


def test_select_tasks_round_robin_over_categories_and_skips_used_dimensions() -> None:
    used = {"know_a"}
    selected = memaudit_driver.select_tasks(_TASKS, 4, used)

    assert [t["target_dimension"] for t in selected] == ["skill_a", "epi_a", "self_a", "pref_a"]
    assert "skill_a" in used


def test_select_tasks_is_the_same_for_every_run(tmp_path: Path) -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))
    code = memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    assert code == memaudit_driver.EXIT_OK
    messages = [t["message"] for t in fake.turns]
    assert messages[: len(messages) // 2] == messages[len(messages) // 2 :]


@pytest.mark.parametrize(
    ("graph", "answer", "outcome"),
    [
        (0.0, 0.0, "write_failure"),
        (0.75, 0.5, "retrieval_failure"),
        (1.0, 1.0, "ok"),
        (0.5, 0.75, "answer_without_graph"),
    ],
)
def test_classify_maps_the_two_scores(graph: float, answer: float, outcome: str) -> None:
    assert memaudit_driver.classify(graph, answer) == outcome


def test_attribution_copy_removes_only_the_last_simulator_message() -> None:
    transcript = {
        "turns": [{"user_simulator": {"text": "first"}}, {"user_simulator": {"text": "last"}}]
    }

    copied = memaudit_driver.attribution_copy(transcript)

    assert copied["turns"][0]["user_simulator"]["text"] == "first"
    assert copied["turns"][1]["user_simulator"]["text"] is None
    assert transcript["turns"][1]["user_simulator"]["text"] == "last"


def test_pilot_wipes_once_per_user_and_run_and_writes_scores_and_labels(tmp_path: Path) -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    code = memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    assert code == memaudit_driver.EXIT_OK
    assert fake.wipes == 2
    facts = json.loads((tmp_path / "results" / "A" / "user_x.json").read_text())
    assert [f["outcome"] for f in facts] == ["retrieval_failure", "retrieval_failure"]
    assert facts[0]["label"] == "memory_failure"
    history = json.loads((tmp_path / "history" / "A" / "user_x" / "episode_1.json").read_text())
    full = json.loads((tmp_path / "transcripts" / "A" / "user_x" / "episode_1.json").read_text())
    assert history["turns"][-1]["user_simulator"]["text"] is None
    assert full["turns"][-1]["user_simulator"]["text"] == "last message"
    assert json.loads((tmp_path / "defects.json").read_text())[0]["score"] == 2.0


def test_graph_is_read_before_any_question(tmp_path: Path) -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))
    order: list[str] = []
    real_memories, real_ask = agent.get_all_memories, agent.ask

    def memories() -> Any:
        order.append("read")
        return real_memories()

    def ask(question: str) -> str:
        order.append("ask")
        return real_ask(question)

    agent.get_all_memories, agent.ask = memories, ask  # type: ignore[method-assign]
    memaudit_driver.run_pilot(_config(tmp_path, ("A",)), _FakeMemAudit(), _bridge(fake), agent)

    assert order[0] == "read"


def test_budget_denial_marks_the_facts_invalid_and_keeps_them_out_of_the_ranking(
    tmp_path: Path,
) -> None:
    fake = _FakeBridge()
    fake.settle_body["budget_denied"] = 3
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    facts = json.loads((tmp_path / "results" / "A" / "user_x.json").read_text())
    assert facts[0]["invalid_reasons"] == ["budget denials: 3"]
    assert json.loads((tmp_path / "defects.json").read_text()) == []


def test_judge_reply_that_does_not_parse_is_not_a_defect(tmp_path: Path) -> None:
    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(reason=""), _bridge(fake), agent)

    assert json.loads((tmp_path / "defects.json").read_text()) == []


def test_slot_fill_reply_that_does_not_parse_is_not_a_defect(tmp_path: Path) -> None:
    class _NoFillReason(_FakeMemAudit):
        def slot_fill(self, _memories: str, _category: str, _dimension: str) -> tuple[str, str]:
            return "unknown", ""

    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _NoFillReason(), _bridge(fake), agent)

    assert json.loads((tmp_path / "defects.json").read_text()) == []
    assert "slot fill" in (tmp_path / "report.md").read_text()


def test_fact_that_survives_the_wipe_marks_the_user_invalid(tmp_path: Path) -> None:
    class _Leaky(_FakeBridge):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/wipe":
                return httpx.Response(
                    200,
                    json={
                        "waited_s": 0.0,
                        "residual_memories": [
                            {"kind": "entity", "name": "Sam", "content": "", "type": ""},
                            {"kind": "claim", "name": "", "content": "old fact", "type": ""},
                        ],
                    },
                )
            return super().__call__(request)

    fake = _Leaky()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    facts = json.loads((tmp_path / "results" / "A" / "user_x.json").read_text())
    assert facts[0]["invalid_reasons"] == ["facts survived the wipe: 1"]


def test_owner_entity_with_no_content_after_a_wipe_is_not_a_leak(tmp_path: Path) -> None:
    class _Bootstrap(_FakeBridge):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/wipe":
                rows = [{"kind": "entity", "name": "Sam", "content": "", "type": ""}]
                return httpx.Response(200, json={"waited_s": 0.0, "residual_memories": rows})
            return super().__call__(request)

    fake = _Bootstrap()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    facts = json.loads((tmp_path / "results" / "A" / "user_x.json").read_text())
    assert facts[0]["invalid_reasons"] == []


def test_any_other_failure_still_leaves_the_manifest_and_the_report(tmp_path: Path) -> None:
    class _Crashes(_FakeMemAudit):
        def run_episode(self, *_args: Any) -> dict[str, Any]:
            raise AttributeError("'list' object has no attribute 'get'")

    fake = _FakeBridge()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    code = memaudit_driver.run_pilot(_config(tmp_path), _Crashes(), _bridge(fake), agent)

    assert code == memaudit_driver.EXIT_HARNESS_FAILURE
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "stopped: harness_failure"
    assert "AttributeError" in manifest["detail"]
    assert "AttributeError" in (tmp_path / "report.md").read_text()


def test_failed_spend_read_still_leaves_the_report(tmp_path: Path) -> None:
    class _NoSpend(_FakeBridge):
        def __call__(self, request: httpx.Request) -> httpx.Response:
            if request.url.path == "/spend":
                return httpx.Response(500, text="postgres down")
            return super().__call__(request)

    fake = _NoSpend()
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))

    memaudit_driver.run_pilot(_config(tmp_path), _FakeMemAudit(), _bridge(fake), agent)

    assert json.loads((tmp_path / "manifest.json").read_text())["spent_usd"] is None


# ── AC-4: the spend stop ──────────────────────────────────────────────────────────────────────


def test_ac4_the_cap_stops_the_run_and_no_further_turn_is_sent(tmp_path: Path) -> None:
    fake = _FakeBridge(turns_before_cap=3)
    agent = memaudit_adapter.SeshatAgent(_bridge(fake))
    config = _config(tmp_path, tasks=6)

    code = memaudit_driver.run_pilot(config, _FakeMemAudit(), _bridge(fake), agent)

    assert code == memaudit_driver.EXIT_SPEND_CAP
    assert len(fake.turns) == 3
    assert fake.wipes == 1
    manifest = json.loads((tmp_path / "manifest.json").read_text())
    assert manifest["status"] == "stopped: spend_cap"
    assert "report.md" in {p.name for p in tmp_path.iterdir()}


def test_ac4_guarded_completion_checks_the_cap_before_each_attempt() -> None:
    calls: list[str] = []

    def check() -> None:
        calls.append("check")
        if calls.count("check") == 2:
            raise memaudit_adapter.SpendCapReached("cap")

    def complete(_messages: Any, **kwargs: Any) -> None:
        calls.append(f"attempt max_retries={kwargs['max_retries']}")

    guarded = memaudit_driver.make_guarded_completion(complete, check)

    with pytest.raises(memaudit_adapter.SpendCapReached):
        guarded([])
    assert calls == ["check", "attempt max_retries=1", "check"]


def test_failed_memaudit_call_stops_the_run_and_never_scores_zero() -> None:
    guarded = memaudit_driver.make_guarded_completion(lambda _m, **_k: None, lambda: None)

    with pytest.raises(memaudit_adapter.HarnessFailure):
        guarded([])


def test_usage_report_never_raises() -> None:
    def refuse(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down")

    bridge = memaudit_adapter.BridgeClient("http://bridge", transport=httpx.MockTransport(refuse))

    bridge.report_usage(1, 1)


# ── Ranking ───────────────────────────────────────────────────────────────────────────────────


def _fact(run: str, dimension: str, outcome: str = "write_failure", **extra: Any) -> dict[str, Any]:
    return {
        "run": run,
        "user_id": "u",
        "dimension": dimension,
        "category": "skill_memory",
        "outcome": outcome,
        "label": "memory_failure",
        "graph_score": 0.0,
        "answer_score": 0.0,
        "transcript": "t",
        **extra,
    }


def test_defect_in_one_run_only_does_not_count() -> None:
    defects = defect_rank.rank_defects({"A": [_fact("A", "d1")], "B": []}, {"memory_failure": 1.0})

    assert defects == []


def test_defect_count_is_the_lower_run_and_the_gap_is_reported() -> None:
    facts = {
        "A": [_fact("A", "d1"), _fact("A", "d2"), _fact("A", "d3")],
        "B": [_fact("B", "d1")],
    }

    defect = defect_rank.rank_defects(facts, {"memory_failure": 2.0})[0]

    assert (defect["count"], defect["gap"], defect["score"]) == (1, 2, 2.0)


def test_rank_is_count_times_severity_and_a_simulator_label_scores_zero() -> None:
    simulator = {"label": "simulator_too_strict", "category": "self_model"}
    facts = {
        "A": [_fact("A", "d1"), _fact("A", "d2", **simulator), _fact("A", "d3", **simulator)],
        "B": [_fact("B", "d1"), _fact("B", "d2", **simulator), _fact("B", "d3", **simulator)],
    }

    defects = defect_rank.rank_defects(facts, {"memory_failure": 1.0, "simulator_too_strict": 0.0})

    assert [d["label"] for d in defects] == ["memory_failure", "simulator_too_strict"]
    assert defects[1]["score"] == 0.0


def test_report_states_the_stop_rule_and_the_limits() -> None:
    report = defect_rank.render_report([], {"status": "complete"})

    assert defect_rank.STOP_RULE in report
    assert defect_rank.LIMITS in report
