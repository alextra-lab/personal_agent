"""FRE-1614: the prompt debug viewer — selection, ordering, rendering, token counts."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.telemetry import prompt_debug, prompt_debug_viewer

SESSION = "5b6b3f5a-6f5a-4b1a-9b1a-5b6b3f5a6f5a"
TRACE_A = "0b6e2a64-3c1f-4c8e-9a51-2f4d6c7e8a90"
TRACE_B = "1c7f3b75-4d20-4d9f-8b62-3e5e7d8f9ba1"
SYSTEM_TEXT = "You are Seshat. " * 20
TOOLS = [
    {
        "type": "function",
        "function": {"name": "web_search", "description": "Search the web. " * 10},
    },
    {"type": "function", "function": {"name": "read_file", "description": "Read."}},
]


def _write(
    root: Path,
    *,
    trace: str = TRACE_A,
    session: str | None = SESSION,
    role: str = "primary",
    minute: int = 0,
    prompt_tokens: int = 1000,
) -> Path:
    record = prompt_debug.build_record(
        trace_id=trace,
        session_id=session,
        role=role,
        model="qwen-test",
        provider="slm_local",
        placement="local",
        started_at=datetime(2026, 10, 18, 9, minute, tzinfo=timezone.utc),
        duration_ms=4200,
        request={
            "messages": [
                {"role": "system", "content": SYSTEM_TEXT},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "what is new?"}],
                },
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "function": {"name": "web_search", "arguments": "{}"}}
                    ],
                },
            ],
            "tools": TOOLS,
            "temperature": 0.6,
        },
        usage={"prompt_tokens": prompt_tokens, "completion_tokens": 9},
    )
    return prompt_debug.write_record(record, root=root)


class TestSelection:
    def test_trace_selects_only_that_trace_in_call_order(self, tmp_path: Path) -> None:
        _write(tmp_path, role="planner", minute=1)
        _write(tmp_path, role="primary", minute=0)
        _write(tmp_path, trace=TRACE_B)
        records = prompt_debug_viewer.select_records(tmp_path, trace=TRACE_A)
        assert [r["role"] for r in records] == ["primary", "planner"]

    def test_session_selects_every_trace_of_the_session(self, tmp_path: Path) -> None:
        _write(tmp_path)
        _write(tmp_path, trace=TRACE_B, minute=5)
        _write(tmp_path, trace=TRACE_B, session=None, minute=6)
        records = prompt_debug_viewer.select_records(tmp_path, session=SESSION)
        assert [r["trace_id"] for r in records] == [TRACE_A, TRACE_B]

    def test_last_selects_the_trace_of_the_newest_file(self, tmp_path: Path) -> None:
        older = _write(tmp_path)
        _write(tmp_path, trace=TRACE_B, role="primary")
        _write(tmp_path, trace=TRACE_B, role="sub_agent")
        past = time.time() - 600
        os.utime(older, (past, past))
        records = prompt_debug_viewer.select_records(tmp_path, last=True)
        assert {r["trace_id"] for r in records} == {TRACE_B}
        assert len(records) == 2

    def test_nothing_captured_returns_no_records(self, tmp_path: Path) -> None:
        assert prompt_debug_viewer.select_records(tmp_path / "absent", last=True) == []


class TestRender:
    def test_render_shows_header_messages_tools_and_counts(self, tmp_path: Path) -> None:
        _write(tmp_path)
        (record,) = prompt_debug_viewer.select_records(tmp_path, trace=TRACE_A)
        text = prompt_debug_viewer.render_record(record, full=False)

        assert "#001 primary" in text
        assert "qwen-test" in text
        assert "4200 ms" in text
        assert SYSTEM_TEXT in text, "the full message text is shown"
        assert "what is new?" in text
        assert f"system · ~{estimate_tokens(SYSTEM_TEXT)} tok" in text
        assert "web_search" in text and "read_file" in text
        assert "tools (2)" in text
        assert "prompt_tokens 1000" in text
        # Without --full the tool JSON itself is not printed.
        assert "Search the web. Search the web." not in text

    def test_full_adds_the_tool_json(self, tmp_path: Path) -> None:
        _write(tmp_path)
        (record,) = prompt_debug_viewer.select_records(tmp_path, trace=TRACE_A)
        text = prompt_debug_viewer.render_record(record, full=True)
        assert "Search the web. Search the web." in text

    def test_estimate_total_is_messages_plus_tools(self, tmp_path: Path) -> None:
        _write(tmp_path)
        (record,) = prompt_debug_viewer.select_records(tmp_path, trace=TRACE_A)
        estimate = prompt_debug_viewer.estimate_record_tokens(record)
        tools_total = sum(estimate_tokens(json.dumps(t, ensure_ascii=False)) for t in TOOLS)
        assert estimate.tools == tools_total
        assert estimate.total == estimate.messages + estimate.tools
        assert estimate.messages >= estimate_tokens(SYSTEM_TEXT) + estimate_tokens("what is new?")


class TestMain:
    def test_main_prints_the_trace(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        _write(tmp_path)
        assert prompt_debug_viewer.main(["--trace", TRACE_A, "--root", str(tmp_path)]) == 0
        assert "#001 primary" in capsys.readouterr().out

    def test_main_exits_nonzero_when_nothing_matches(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        assert prompt_debug_viewer.main(["--last", "--root", str(tmp_path)]) == 1
        assert "no capture" in capsys.readouterr().err

    def test_main_needs_exactly_one_selector(self, tmp_path: Path) -> None:
        with pytest.raises(SystemExit):
            prompt_debug_viewer.main(["--root", str(tmp_path)])
