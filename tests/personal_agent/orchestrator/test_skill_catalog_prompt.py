"""ADR-0156 D2 and AC-7 (FRE-1577 AC-2): the user's message selects no skill.

The primary's first call is assembled twice, from two fresh contexts with the same fixed state,
and two user messages: one with no skill word, one with every ``keywords`` entry that the skills
held before FRE-1577 deleted the field (saved in ``tests/fixtures/skill_keywords_2026-10-11.yaml``).
The system prompt must be byte-identical, hold the catalog, and hold no skill body. No message
of either request may hold a skill body either: a body reaches the model only as a
``read_skill`` result.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from personal_agent.governance.models import Mode
from personal_agent.orchestrator.channels import Channel
from personal_agent.orchestrator.executor import execute_task_safe
from personal_agent.orchestrator.session import SessionManager
from personal_agent.orchestrator.skills import assemble_skill_catalog, get_all_skills
from personal_agent.orchestrator.types import ExecutionContext

_KEYWORDS_FILE = Path(__file__).resolve().parents[2] / "fixtures" / "skill_keywords_2026-10-11.yaml"
_PLAIN = "What is the capital of Portugal?"
_STARTED = datetime(2026, 10, 11, 8, 0, 0, tzinfo=UTC)
# Harness tool-call ids derive from the trace id, so both requests share one.
_TRACE_ID = "fre1577ac2fixedtrace0000000000000"


def _every_keyword() -> list[str]:
    saved: dict[str, list[str]] = yaml.safe_load(_KEYWORDS_FILE.read_text(encoding="utf-8"))
    return [kw for keywords in saved.values() for kw in keywords]


def _body_samples() -> dict[str, str]:
    """A 200-character slice from the middle of every skill body: if it is sent, a body is."""
    samples: dict[str, str] = {}
    for name, skill in get_all_skills().items():
        middle = len(skill.body) // 2
        samples[name] = skill.body[middle : middle + 200]
    return samples


def _mock_client() -> AsyncMock:
    mock = AsyncMock()
    mock.model_configs = {}
    mock.respond.return_value = {
        "role": "assistant",
        "content": "Done.",
        "tool_calls": [],
        "reasoning_trace": None,
        "usage": {"total_tokens": 10, "prompt_tokens": 8, "completion_tokens": 2},
        "raw": {},
        "cost_usd": 0.0,
    }
    return mock


@pytest.fixture(autouse=True)
def _restore_executor_tool_globals() -> object:
    """Restore the executor's lazily-cached tool registry after each test."""
    import personal_agent.orchestrator.executor as _ex

    saved_registry = _ex._tool_registry
    yield
    _ex._tool_registry = saved_registry


async def _first_call(message: str) -> dict[str, Any]:
    """Drive the real pipeline from a fresh context; return the first LLM call's kwargs."""
    import personal_agent.orchestrator.executor as _ex

    _ex._tool_registry = None
    client = _mock_client()
    session_manager = SessionManager()
    session_id = session_manager.create_session(Mode.NORMAL, Channel.CHAT)
    ctx = ExecutionContext(
        session_id=session_id,
        trace_id=_TRACE_ID,
        user_message=message,
        mode=Mode.NORMAL,
        channel=Channel.CHAT,
    )
    ctx.turn_started_at = _STARTED
    tools = [{"name": "read_skill", "description": "d", "parameters": {"type": "object"}}]
    with (
        patch("personal_agent.llm_client.factory.get_llm_client", return_value=client),
        patch(
            "personal_agent.orchestrator.executor.get_default_registry",
            return_value=MagicMock(get_tool_definitions_for_llm=MagicMock(return_value=tools)),
        ),
    ):
        await execute_task_safe(ctx, session_manager)
    return client.respond.call_args_list[0].kwargs


def test_the_saved_keywords_cover_every_skill_that_had_them() -> None:
    keywords = _every_keyword()
    assert len(keywords) == 323
    assert "logs" in keywords and "mermaid" in keywords and "cpu load" in keywords


@pytest.mark.asyncio
async def test_every_keyword_leaves_the_system_prompt_unchanged() -> None:
    plain = await _first_call(_PLAIN)
    loaded = await _first_call("Please help: " + "; ".join(_every_keyword()))

    assert plain["system_prompt"] == loaded["system_prompt"]
    # Every message that is not the user's own text is the same in both requests.
    non_user = [
        [m for m in kwargs["messages"] if m.get("role") != "user"] for kwargs in (plain, loaded)
    ]
    assert non_user[0] == non_user[1]


@pytest.mark.asyncio
async def test_no_skill_body_is_in_the_request() -> None:
    samples = _body_samples()
    assert len(samples) == 11
    for message in (_PLAIN, "; ".join(_every_keyword())):
        kwargs = await _first_call(message)
        sent = kwargs["system_prompt"] + json.dumps(kwargs["messages"])
        leaked = sorted(name for name, sample in samples.items() if sample in sent)
        assert leaked == []


@pytest.mark.asyncio
async def test_the_catalog_is_in_the_cached_system_prompt() -> None:
    kwargs = await _first_call(_PLAIN)
    catalog = assemble_skill_catalog()
    assert catalog in kwargs["system_prompt"]
    for name, skill in get_all_skills().items():
        assert f"- {name}: {skill.description}\n" in catalog
    assert "skill_index" in kwargs["prompt_identity"].component_ids
    assert "skill_bodies" not in kwargs["prompt_identity"].component_ids


def test_the_catalog_has_one_line_per_skill_in_name_order() -> None:
    lines = [ln for ln in assemble_skill_catalog().splitlines() if ln.startswith("- ")]
    names = [ln[2:].split(":", 1)[0] for ln in lines]
    assert names == sorted(get_all_skills())
    assert len(names) == 11
