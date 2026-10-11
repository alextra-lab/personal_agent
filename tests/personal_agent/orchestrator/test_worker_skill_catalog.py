"""Each worker type sees its own skill catalog, and every type holds read_skill (FRE-1580).

ADR-0156 D9.1, D9.2, AC-10. FRE-1580 AC-1 and AC-2.

AC-1 — a type's catalog lists exactly the skills whose ``metadata.requires-tools`` it holds,
and every type holds ``read_skill``. *Fails if* a type sees a skill whose tools it lacks,
misses one whose tools it holds, or any type lacks ``read_skill``.

AC-2 — the researcher's ``read_skill`` returns a skill file, and refuses any path outside the
skill folders. *Fails if* it can read anything else.

The prompt and the tools are read from the first model call of a real worker run: the worker
loop, the registry, governance and the permission rules are real (``tests/_helpers``).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.governance.sub_agent_tools import evaluate_sub_agent_tool_grant
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from tests._helpers.worker_run import WorkerRun, run_worker, shared_layer, worker_layer

# Each seeded skill and the tools it requires. The subset rule decides who sees it.
_SEEDED: dict[str, str | None] = {
    "needs-nothing": None,
    "needs-bash": "bash",
    "needs-search": "web_search",
    "needs-telemetry": "query_telemetry",
    "needs-search-and-telemetry": "web_search, query_telemetry",
}
_SEEDED_VISIBLE = {
    WorkerType.RESEARCHER: {"needs-nothing", "needs-search"},
    WorkerType.GENERAL: {"needs-nothing", "needs-telemetry"},
}


def _write_skill(root: Path, name: str, requires: str | None) -> None:
    folder = root / name
    (folder / "references").mkdir(parents=True)
    metadata = f"metadata:\n  requires-tools: {requires}\n" if requires else ""
    (folder / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: Covers the {name} case.\n{metadata}---\n\n"
        f"Body of {name}.\n",
        encoding="utf-8",
    )
    (folder / "references" / "guide.md").write_text(f"Guide of {name}.\n", encoding="utf-8")


@pytest.fixture
def seeded_skills(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "skills"
    for name, requires in _SEEDED.items():
        _write_skill(root, name, requires)
    (tmp_path / "secret.txt").write_text("outside the skill folders\n", encoding="utf-8")
    monkeypatch.setattr(skills_module, "SKILLS_DIR", root)
    monkeypatch.setattr(skills_module, "_cache", None)
    return root


def _granted_tools(worker_type: WorkerType) -> list[str]:
    """The tools a worker of this type holds on a task that names no file (NORMAL mode)."""
    grant = evaluate_sub_agent_tool_grant(
        WORKER_TYPES[worker_type].tools, Mode.NORMAL, load_governance_config()
    )
    return [t for t in grant.granted if t != "read"]


def _spec(worker_type: WorkerType) -> SubAgentSpec:
    return SubAgentSpec(
        task="answer the question",
        context=[],
        timeout_seconds=30.0,
        tools=_granted_tools(worker_type),
        worker_type=worker_type,
    )


def _catalog_names(system: str) -> set[str]:
    """Return the skill names that the catalog block of a system prompt lists."""
    if "## Skills" not in system:
        return set()
    block = system.split("## Skills", 1)[1]
    return {
        line[2:].split(":", 1)[0]
        for line in block.splitlines()
        if line.startswith("- ") and ":" in line
    }


def _read_skill_def(first_call: dict[str, Any]) -> dict[str, Any]:
    tools = first_call["tools"] or []
    matches = [t for t in tools if t["function"]["name"] == "read_skill"]
    assert len(matches) == 1, [t["function"]["name"] for t in tools]
    function: dict[str, Any] = matches[0]["function"]
    return function


async def _first_call(worker_type: WorkerType) -> dict[str, Any]:
    with shared_layer(worker_layer()):
        run = await run_worker(_spec(worker_type), [])
    return run.calls[0]


@pytest.mark.usefixtures("seeded_skills")
class TestEachTypeSeesItsOwnCatalog:
    """AC-1 (ADR-0156 AC-10) over seeded skills, one per side of the subset rule."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worker_type", list(WorkerType))
    async def test_the_catalog_lists_exactly_the_skills_whose_tools_the_type_holds(
        self, worker_type: WorkerType
    ) -> None:
        first = await _first_call(worker_type)
        system = first["messages"][0]["content"]
        assert _catalog_names(system) == _SEEDED_VISIBLE[worker_type]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worker_type", list(WorkerType))
    async def test_every_type_holds_read_skill_and_its_enum_is_the_catalog(
        self, worker_type: WorkerType
    ) -> None:
        first = await _first_call(worker_type)
        name_schema = _read_skill_def(first)["parameters"]["properties"]["name"]
        assert set(name_schema["enum"]) == _SEEDED_VISIBLE[worker_type]

    @pytest.mark.asyncio
    async def test_a_hidden_skill_is_not_in_the_prompt_text_at_all(self) -> None:
        """S2: a filtered skill is hidden entirely, not listed as unavailable."""
        first = await _first_call(WorkerType.RESEARCHER)
        system = first["messages"][0]["content"]
        for hidden in set(_SEEDED) - _SEEDED_VISIBLE[WorkerType.RESEARCHER]:
            assert hidden not in system

    def test_the_seeded_negative_a_type_that_held_bash_would_see_the_bash_skill(self) -> None:
        """The rule is the subset rule, not a fixed list: holding bash shows needs-bash."""
        visible = skills_module.skills_for_tools(["bash", "read_skill"])
        assert set(visible) == {"needs-nothing", "needs-bash"}

    @pytest.mark.asyncio
    async def test_a_type_with_no_tools_gets_no_catalog(self) -> None:
        """ALERT/DEGRADED revoke every worker tool; with no read_skill, no catalog is shown."""
        spec = SubAgentSpec(
            task="t", context=[], timeout_seconds=30.0, tools=[], worker_type=WorkerType.GENERAL
        )
        with shared_layer(worker_layer()):
            run = await run_worker(spec, [])
        assert "## Skills" not in run.calls[0]["messages"][0]["content"]


class TestTheRealSkills:
    """The shipped skills: every one except two requires bash, which no worker holds."""

    @pytest.mark.asyncio
    @pytest.mark.parametrize("worker_type", list(WorkerType))
    async def test_each_type_sees_the_real_skills_its_tools_allow(
        self, worker_type: WorkerType
    ) -> None:
        held = set(_granted_tools(worker_type))
        expected = {
            name
            for name, doc in skills_module.get_all_skills().items()
            if set(doc.requires_tools) <= held
        }
        first = await _first_call(worker_type)
        assert _catalog_names(first["messages"][0]["content"]) == expected
        assert expected, "a worker sees at least the skills that need no tool"
        assert set(_read_skill_def(first)["parameters"]["properties"]["name"]["enum"]) == expected

    def test_every_registry_type_lists_read_skill(self) -> None:
        for worker_type, spec in WORKER_TYPES.items():
            assert "read_skill" in spec.tools, worker_type


@pytest.mark.usefixtures("seeded_skills")
class TestTheResearchersReadSkill:
    """AC-2: the researcher reads skill files, and nothing else."""

    @staticmethod
    async def _run(*calls: tuple[str, dict[str, Any]]) -> WorkerRun:
        with shared_layer(worker_layer()):
            return await run_worker(_spec(WorkerType.RESEARCHER), list(calls))

    @pytest.mark.asyncio
    async def test_it_returns_a_visible_skill_body_and_a_listed_file(self) -> None:
        run = await self._run(
            ("read_skill", {"name": "needs-search"}),
            ("read_skill", {"name": "needs-search", "path": "references/guide.md"}),
        )
        body, guide = run.payload("c0"), run.payload("c1")
        assert body["ok"] is True
        assert body["body"] == "Body of needs-search."
        assert body["files"] == ["references/guide.md"]
        assert guide["ok"] is True
        assert guide["content"] == "Guide of needs-search.\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "path",
        [
            "../../secret.txt",
            "../needs-telemetry/references/guide.md",
            "SKILL.md/../../secret.txt",
            "/etc/hostname",
        ],
    )
    async def test_a_path_outside_the_skill_folder_is_refused(self, path: str) -> None:
        run = await self._run(("read_skill", {"name": "needs-search", "path": path}))
        payload = run.payload("c0")
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_path"
        assert "outside the skill folders" not in run.tool_messages["c0"]["content"]

    @pytest.mark.asyncio
    @pytest.mark.parametrize("hidden", ["needs-telemetry", "needs-bash"])
    async def test_a_hidden_skill_cannot_be_read_by_name_or_by_path(self, hidden: str) -> None:
        run = await self._run(
            ("read_skill", {"name": hidden}),
            ("read_skill", {"name": hidden, "path": "references/guide.md"}),
        )
        for call_id in ("c0", "c1"):
            payload = run.payload(call_id)
            assert payload["ok"] is False
            assert payload["error"]["code"] == "unknown_skill"
            assert f"Guide of {hidden}" not in run.tool_messages[call_id]["content"]
            assert f"Body of {hidden}" not in run.tool_messages[call_id]["content"]

    @pytest.mark.asyncio
    async def test_it_holds_no_file_read(self, seeded_skills: Path) -> None:
        secret = seeded_skills.parent / "secret.txt"
        run = await self._run(("read", {"path": str(secret)}))
        payload = run.payload("c0")
        assert payload["ok"] is False
        assert payload["error"]["code"] == "not_available_to_worker"
        assert "outside the skill folders" not in run.tool_messages["c0"]["content"]


class TestASymlinkedSkillIsNoSkill:
    """ADR-0156 AC-10: read_skill never returns text from outside the skill folders."""

    @pytest.mark.asyncio
    async def test_a_symlinked_folder_or_skill_file_is_not_loaded(
        self, seeded_skills: Path
    ) -> None:
        outside = seeded_skills.parent / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text(
            "---\nname: linked-folder\ndescription: Comes from outside.\n---\n\nOutside body.\n",
            encoding="utf-8",
        )
        (seeded_skills / "linked-folder").symlink_to(outside, target_is_directory=True)
        (seeded_skills / "linked-file").mkdir()
        (seeded_skills / "linked-file" / "SKILL.md").symlink_to(outside / "SKILL.md")

        assert "linked-folder" not in skills_module.get_all_skills()
        assert "linked-file" not in skills_module.get_all_skills()
        with shared_layer(worker_layer()):
            run = await run_worker(
                _spec(WorkerType.RESEARCHER), [("read_skill", {"name": "linked-folder"})]
            )
        assert run.payload("c0")["ok"] is False
        assert "Outside body" not in run.tool_messages["c0"]["content"]
