"""A skill body that a worker without ``bash`` sees holds no command it cannot run (FRE-1616).

ADR-0156 amendment of 2026-10-18 (FRE-1607), B2, V2 and V5.

AC-2 (V2) — no listed command in a worker-visible body. *Fails if* a visible body holds a
shell fence, or a fence line or an inline code span that starts with a listed command word,
or any of the 13 seeded fixtures passes.

AC-5 (V5, second part) — each pointer line names a file that ``read_skill(name, path)``
returns. The first part (the rebuilt body equals the old body outside the listed edits) is a
check at the gate, run against ``origin/main``.

A PR that adds a new command word to any skill adds it to ``COMMAND_WORDS``. A command outside
the list is not detected, by design.
"""

from __future__ import annotations

import re

import pytest

import personal_agent.orchestrator.skills as skills_module
from personal_agent.config.governance_loader import load_governance_config
from personal_agent.governance.models import Mode
from personal_agent.governance.sub_agent_tools import evaluate_sub_agent_tool_grant
from personal_agent.orchestrator.sub_agent_types import SubAgentSpec
from personal_agent.orchestrator.worker_types import WORKER_TYPES, WorkerType
from tests._helpers.worker_run import run_worker, shared_layer, worker_layer

COMMAND_WORDS = ("curl", "date", "docker", "grep", "jq", "ps", "psql", "python3")
SHELL_LABELS = ("", "bash", "sh", "shell", "console", "zsh", "shell-session")

_COMMAND_START = re.compile(rf"^\s*(?:\$\s*)?(?:{'|'.join(COMMAND_WORDS)})(?![\w-])")
# A fence opens with three or more backticks or tildes, maybe indented, with an info string.
# Its label is the first word of the info string: ``bash title=x`` is a ``bash`` fence.
_FENCE = re.compile(r"^\s*(?:`{3,}|~{3,})\s*([\w-]*)")
_INLINE_CODE = re.compile(r"`([^`\n]+)`")


def forbidden_forms(body: str) -> list[str]:
    """Return the forms in ``body`` that a worker without ``bash`` must not see.

    Args:
        body: A ``SKILL.md`` body.

    Returns:
        One message per shell fence, per fence line that starts with a command word, and per
        inline code span that starts with a command word.
    """
    found: list[str] = []
    fence_label: str | None = None
    for number, line in enumerate(body.splitlines(), start=1):
        match = _FENCE.match(line)
        if match and fence_label is None:
            fence_label = match.group(1)
            if fence_label in SHELL_LABELS:
                found.append(f"line {number}: fence labelled {fence_label!r}")
            continue
        if match:
            fence_label = None
            continue
        if fence_label is not None:
            if _COMMAND_START.match(line):
                found.append(f"line {number}: fence line starts with a command word: {line!r}")
            continue
        found.extend(
            f"line {number}: inline code starts with a command word: {span!r}"
            for span in _INLINE_CODE.findall(line)
            if _COMMAND_START.match(span)
        )
    return found


# --- the 13 seeded fixtures: each one must be flagged -------------------------------------

_LABEL_FIXTURES = {
    "bash": "```bash\necho hello\n```\n",
    "sh": "```sh\necho hello\n```\n",
    "shell": "```shell\necho hello\n```\n",
    "console": "```console\necho hello\n```\n",
    "unlabelled": "```\nplain text with no command word\n```\n",
}
_WORD_FIXTURES = {
    "curl": "```python\ncurl -fsS http://x\n```\n",
    "date": "```python\ndate +%s\n```\n",
    "docker": "```python\ndocker ps\n```\n",
    "grep": "```python\ngrep -r x .\n```\n",
    "jq": "Run `jq '.a'` on the reply.\n",
    "ps": "Run `ps aux` first.\n",
    "psql": "Run `psql -U agent` first.\n",
    "python3": "Run `python3 script.py` first.\n",
}
_FIXTURES = {**_LABEL_FIXTURES, **_WORD_FIXTURES}


# Fence shapes beyond the 13: each one must open a shell fence (review of FRE-1616).
_SHAPE_FIXTURES = {
    "indented": "  ```bash\n  echo hello\n  ```\n",
    "tilde": "~~~bash\necho hello\n~~~\n",
    "info-string": "```bash title=x\necho hello\n```\n",
    "zsh": "```zsh\necho hello\n```\n",
    "four-backtick": "````bash\necho hello\n````\n",
}


class TestTheDetector:
    def test_there_are_thirteen_fixtures_one_per_label_and_one_per_word(self) -> None:
        assert len(_FIXTURES) == 13
        assert set(_WORD_FIXTURES) == set(COMMAND_WORDS)

    @pytest.mark.parametrize("name", sorted(_SHAPE_FIXTURES))
    def test_a_shell_fence_in_another_shape_is_flagged(self, name: str) -> None:
        assert forbidden_forms(_SHAPE_FIXTURES[name]), name

    @pytest.mark.parametrize("name", sorted(_FIXTURES))
    def test_each_seeded_fixture_is_flagged(self, name: str) -> None:
        assert forbidden_forms(_FIXTURES[name]), name

    @pytest.mark.parametrize(
        "body",
        [
            '```python\nsearch_logs(action="count", index="agent-logs", since="24h")\n```\n',
            '```python\naggs={"by_day": {"date_histogram": {"field": "@timestamp"}}}\n```\n',
            "The word psql in prose, and a span that only contains one: `a_date_field`.\n",
            "A span such as `date_histogram` or `ps-free` is not a command start.\n",
        ],
    )
    def test_a_typed_call_or_a_word_that_only_contains_a_command_is_not_flagged(
        self, body: str
    ) -> None:
        assert forbidden_forms(body) == []


# --- the real skills ----------------------------------------------------------------------


def _granted_tools(worker_type: WorkerType) -> list[str]:
    grant = evaluate_sub_agent_tool_grant(
        WORKER_TYPES[worker_type].tools, Mode.NORMAL, load_governance_config()
    )
    return [t for t in grant.granted if t != "read"]


@pytest.mark.parametrize("worker_type", list(WorkerType))
def test_no_body_that_a_worker_sees_holds_a_listed_command(worker_type: WorkerType) -> None:
    visible = skills_module.skills_for_tools(_granted_tools(worker_type))
    assert visible, "a worker sees at least one skill"
    found = {name: forbidden_forms(doc.body) for name, doc in visible.items()}
    assert {name: forms for name, forms in found.items() if forms} == {}


def test_the_skills_that_dropped_bash_are_in_the_checked_set() -> None:
    """The check would pass vacuously if the four skills were still hidden."""
    visible = skills_module.skills_for_tools(_granted_tools(WorkerType.GENERAL))
    assert {"log-search", "model-latency", "spend-and-tokens", "turn-explanation"} <= set(visible)


# --- V5, second part: each pointer names a file that read_skill returns --------------------

_POINTER = re.compile(
    r"^Past the tool's limits, and only with the `bash` tool: read `(references/[\w.-]+\.md)`",
    re.MULTILINE,
)
_EXPECTED_POINTERS = {
    "log-search": ["references/curl-steps.md", "references/index-granularity.md"],
    "spend-and-tokens": ["references/budget-snapshot.md"],
    "turn-explanation": ["references/other-substrates.md"],
    "model-latency": ["references/latency-recipe.md"],
}


@pytest.mark.parametrize("name", sorted(_EXPECTED_POINTERS))
def test_a_body_holds_the_pointer_lines_that_the_design_lists(name: str) -> None:
    body = skills_module.get_all_skills()[name].body
    assert _POINTER.findall(body) == _EXPECTED_POINTERS[name]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("name", "path"),
    [(n, p) for n, paths in sorted(_EXPECTED_POINTERS.items()) for p in paths],
)
async def test_read_skill_returns_the_file_that_a_pointer_names(name: str, path: str) -> None:
    spec = SubAgentSpec(
        task="t",
        context=[],
        timeout_seconds=30.0,
        tools=_granted_tools(WorkerType.GENERAL),
        worker_type=WorkerType.GENERAL,
    )
    with shared_layer(worker_layer()):
        run = await run_worker(spec, [("read_skill", {"name": name, "path": path})])
    payload = run.payload("c0")
    assert payload["ok"] is True
    on_disk = (skills_module.get_all_skills()[name].folder / path).read_text(encoding="utf-8")
    assert payload["content"].strip() == on_disk.strip()


# The six Recovery rows of turn-explanation that belong to the moved steps (T4). They moved to
# the reference file, so the body no longer names a command that a worker cannot run.
_MOVED_RECOVERY_ROWS = (
    "`error: no model call or tool call of trace ...` from the script",
    "`docker logs` prints nothing",
    "Step 4 returns HTTP 401 or 403",
    "Step 4 cannot resolve the host",
    "A `span_id` is absent",
    "`could not resolve host: tempo`",
)


@pytest.mark.parametrize("row", _MOVED_RECOVERY_ROWS)
def test_a_moved_recovery_row_is_in_the_reference_and_not_in_the_body(row: str) -> None:
    doc = skills_module.get_all_skills()["turn-explanation"]
    reference = (doc.folder / "references" / "other-substrates.md").read_text(encoding="utf-8")
    assert "## Recovery of these steps" in reference
    assert row in reference.split("## Recovery of these steps", 1)[1]
    assert row not in doc.body
