"""Unit tests for intra-turn tool-result digest infrastructure (ADR-0085, FRE-475 PR-A).

PR-A ships the *pure* digest machinery — key builder, format-aware extractors,
byte-stable digest serializer, content-intrinsic gating, R2 persist helper, and
telemetry record — none of it wired into the executor. These tests exercise that
surface directly; the byte-stability fixed point (D3) is release-blocking.
"""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest

from personal_agent.config import settings
from personal_agent.orchestrator.tool_result_digest import (
    build_digest_message,
    build_tool_result_key,
    compute_content_hash,
    digest_saves_enough,
    digest_tool_content,
    persist_tool_result,
    should_digest,
)
from personal_agent.storage.artifact_store import ArtifactKeyError, ArtifactStoreError

_SESSION = UUID("11111111-1111-1111-1111-111111111111")


# ---------------------------------------------------------------------------
# A2 — canonical key builder
# ---------------------------------------------------------------------------


def test_build_tool_result_key_happy_path() -> None:
    key = build_tool_result_key(_SESSION, "trace-abc123", "call_t3_0_xy")
    assert key == f"tool-results/{_SESSION}/trace-abc123/call_t3_0_xy"


@pytest.mark.parametrize(
    "trace_id,tool_call_id",
    [
        ("..", "call_1"),  # traversal segment
        ("trace/../etc", "call_1"),  # slash + traversal
        ("trace abc", "call_1"),  # space (URL-unsafe)
        ("trace", "call?x=1"),  # query char
        ("trace", "call#frag"),  # fragment char
        ("trace", "call\n1"),  # control char
        ("", "call_1"),  # empty
        ("trace", ""),  # empty
    ],
)
def test_build_tool_result_key_rejects_unsafe_segments(trace_id: str, tool_call_id: str) -> None:
    with pytest.raises(ArtifactKeyError):
        build_tool_result_key(_SESSION, trace_id, tool_call_id)


# ---------------------------------------------------------------------------
# content hash
# ---------------------------------------------------------------------------


def test_compute_content_hash_is_full_sha256_hex() -> None:
    h = compute_content_hash("hello world")
    assert len(h) == 64  # full sha256, not the 16-hex stable_hash
    assert all(c in "0123456789abcdef" for c in h)
    assert compute_content_hash("hello world") == h  # deterministic
    assert compute_content_hash("hello worlds") != h


# ---------------------------------------------------------------------------
# A3 — format-aware extractors
# ---------------------------------------------------------------------------


def test_digest_bash_keeps_exit_code_command_note_and_truncates_stdout() -> None:
    big_stdout = "\n".join(f"line {i}" for i in range(500))
    content = json.dumps(
        {
            "success": True,
            "exit_code": 0,
            "stdout": big_stdout,
            "stderr": "",
            "command": "seq 500",
            "truncated_path": "/tmp/x",
            "note": None,
        }
    )
    body = digest_tool_content("bash", content)
    assert body["format"] == "bash"
    assert body["exit_code"] == 0
    assert body["command"] == "seq 500"
    assert body["truncated_path"] == "/tmp/x"
    serialized = json.dumps(body)
    assert "line 0" in serialized  # head kept
    assert "line 499" in serialized  # tail kept
    assert "line 250" not in serialized  # middle elided
    assert len(serialized) < len(content)  # genuinely smaller


def test_digest_bash_structured_middle_in_stdout_retains_frames() -> None:
    """Codex Q3: a traceback inside bash.stdout must keep the failing frame,
    not be head/tail-masked into oblivion.
    """
    pad = "\n".join(f"noise {i}" for i in range(300))
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/app/widget.py", line 88, in run\n'
        "    raise ValueError('boom')\n"
        "ValueError: boom"
    )
    stdout = pad + "\n" + tb + "\n" + pad
    content = json.dumps(
        {"success": False, "exit_code": 1, "stdout": stdout, "stderr": "", "command": "pytest"}
    )
    body = digest_tool_content("bash", content)
    # Assert against the digested stream itself (json.dumps would escape the quotes).
    assert 'File "/app/widget.py", line 88' in body["stdout"]
    assert "ValueError: boom" in body["stdout"]
    assert "noise 150" not in body["stdout"]  # surrounding noise elided


def test_digest_read_keeps_outline_and_region() -> None:
    big = "\n".join(f"src line {i}" for i in range(400))
    content = json.dumps(
        {
            "path": "/app/x.py",
            "content": big,
            "truncated": True,
            "offset": 1,
            "limit": 400,
            "total_lines": 400,
            "marker": "[truncated head]",
        }
    )
    body = digest_tool_content("read", content)
    assert body["format"] == "read"
    assert body["path"] == "/app/x.py"
    assert body["total_lines"] == 400
    assert body["marker"] == "[truncated head]"
    serialized = json.dumps(body)
    assert len(serialized) < len(content)


def test_digest_generic_json_extracts_keys_and_counts() -> None:
    content = json.dumps({"results": [1, 2, 3, 4], "status": "ok", "nested": {"a": 1, "b": 2}})
    body = digest_tool_content("memory_search", content)
    assert body["format"] == "json"
    assert "results" in body["keys"]
    assert body["counts"]["results"] == 4


def test_digest_unrecognized_text_falls_back_to_head_tail() -> None:
    text = "\n".join(f"row {i}" for i in range(400))
    body = digest_tool_content("some_tool", text)
    assert body["format"] == "text"
    serialized = json.dumps(body)
    assert "row 0" in serialized
    assert "row 399" in serialized
    assert "row 200" not in serialized


# ---------------------------------------------------------------------------
# A4b — content-intrinsic gating
# ---------------------------------------------------------------------------


def test_should_digest_below_threshold_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", 1500)
    assert should_digest("bash", "tiny output") is False


def test_should_digest_above_threshold_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", 50)
    big = json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"})
    assert should_digest("bash", big) is True


def test_should_digest_error_payload_kept_verbatim(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", 1)
    err = json.dumps({"status": "error", "hint": "x " * 5000})
    assert should_digest("bash", err) is False


def test_should_digest_excluded_tool_is_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_threshold_tokens", 1)
    monkeypatch.setattr(settings, "tool_result_digest_exclude_tools", ["read_skill"])
    assert should_digest("read_skill", "x " * 5000) is False


def test_digest_saves_enough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "tool_result_digest_min_savings_tokens", 100)
    big = json.dumps(
        {"stdout": "\n".join(str(i) for i in range(5000)), "exit_code": 0, "command": "y"}
    )
    body = digest_tool_content("bash", big)
    msg = build_digest_message(
        tool_call_id="c1",
        tool_name="bash",
        r2_key="tool-results/x/y/c1",
        content_hash=compute_content_hash(big),
        full_byte_len=len(big.encode("utf-8")),
        body=body,
    )
    assert digest_saves_enough(big, msg) is True
    # A tiny original cannot clear the savings floor.
    assert digest_saves_enough("short", msg) is False


# ---------------------------------------------------------------------------
# A4 / D3 — byte-stable digest serializer (release-blocking)
# ---------------------------------------------------------------------------


def _make_message(content: str) -> dict[str, object]:
    body = digest_tool_content("bash", content)
    return build_digest_message(
        tool_call_id="call_t1_0_z",
        tool_name="bash",
        r2_key=build_tool_result_key(_SESSION, "trace1", "call_t1_0_z"),
        content_hash=compute_content_hash(content),
        full_byte_len=len(content.encode("utf-8")),
        body=body,
    )


def test_digest_message_preserves_tool_pair_fields() -> None:
    msg = _make_message(json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"}))
    assert msg["role"] == "tool"
    assert msg["tool_call_id"] == "call_t1_0_z"
    assert msg["name"] == "bash"
    assert isinstance(msg["content"], str)


def test_digest_message_byte_identical_across_repeated_calls() -> None:
    content = json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"})
    assert _make_message(content)["content"] == _make_message(content)["content"]


def test_digest_message_stable_through_serialize_roundtrip() -> None:
    """Cross-turn Postgres-replay proxy: parse the stored message and re-serialize."""
    content = json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"})
    msg = _make_message(content)
    replayed = json.loads(json.dumps(msg))
    # The persisted `content` string is the cache-relevant payload; it must survive.
    assert replayed["content"] == msg["content"]
    reparsed = json.loads(replayed["content"])
    assert json.dumps(reparsed, sort_keys=True, separators=(",", ":")) == replayed["content"]


def test_digest_message_regenerate_from_same_raw_content_is_identical() -> None:
    """Codex Q2: full regenerate path (extractor + hash + placeholder), not just re-serialize."""
    content = json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"})
    first = _make_message(content)
    second = _make_message(content)
    assert first == second


def test_digest_message_has_no_volatile_fields() -> None:
    content = json.dumps({"stdout": "x " * 5000, "exit_code": 0, "command": "y"})
    payload = json.loads(_make_message(content)["content"])
    flat = json.dumps(payload).lower()
    for forbidden in ("timestamp", "created_at", "expires", "x-amz", "retry", "presigned"):
        assert forbidden not in flat
    # Size is reported as stable bytes, not an estimated token count.
    assert "bytes" in payload
    assert payload["content_hash"] == compute_content_hash(content)
    assert payload["r2_key"] in payload["hint"]


# ---------------------------------------------------------------------------
# A5 — R2 persist helper
# ---------------------------------------------------------------------------


class _FakeStore:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.puts: list[tuple[str, bytes]] = []

    async def put(
        self, *, r2_key: str, content: bytes, content_type: str, metadata=None, trace_id=None
    ) -> None:
        if self.fail:
            raise ArtifactStoreError("boom")
        self.puts.append((r2_key, content))


@pytest.mark.asyncio
async def test_persist_tool_result_success() -> None:
    store = _FakeStore()
    ok = await persist_tool_result(
        store, r2_key="tool-results/a/b/c", content="payload", trace_id="t1"
    )
    assert ok is True
    assert store.puts[0][0] == "tool-results/a/b/c"
    assert store.puts[0][1] == b"payload"


@pytest.mark.asyncio
async def test_persist_tool_result_failure_returns_false() -> None:
    store = _FakeStore(fail=True)
    ok = await persist_tool_result(
        store, r2_key="tool-results/a/b/c", content="payload", trace_id="t1"
    )
    assert ok is False


# ---------------------------------------------------------------------------
# A6 — telemetry record dual-write
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_record_digest_writes_durable_jsonl(tmp_path: Path) -> None:
    from personal_agent.telemetry.tool_result_digest import (
        ToolResultDigestRecord,
        record_digest,
    )

    rec = ToolResultDigestRecord(
        trace_id="t1",
        session_id="s1",
        tool_name="bash",
        tool_call_id="c1",
        bytes_in=4096,
        tokens_in=1200,
        tokens_out=180,
        format="bash",
        persisted=True,
        r2_key="tool-results/s1/t1/c1",
        content_hash="ab" * 32,
    )
    await record_digest(rec, None, output_dir=tmp_path)
    files = list(tmp_path.glob("TRD-*.jsonl"))
    assert len(files) == 1
    line = json.loads(files[0].read_text().strip())
    assert line["trace_id"] == "t1"
    assert line["tool_name"] == "bash"
    assert line["tokens_out"] == 180
