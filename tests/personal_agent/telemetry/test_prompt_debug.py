"""FRE-1614: the prompt debug capture writer — record, secrets, layout, retention."""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from personal_agent.telemetry import prompt_debug

TRACE_ID = "0b6e2a64-3c1f-4c8e-9a51-2f4d6c7e8a90"
SESSION_ID = "5b6b3f5a-6f5a-4b1a-9b1a-5b6b3f5a6f5a"
SEEDED_KEY = "sk-seeded-FRE1614-api-key-value"
SEEDED_BEARER = "Bearer seeded-FRE1614-authorization-value"


def _request(**overrides: Any) -> dict[str, Any]:
    request: dict[str, Any] = {
        "model": "anthropic/claude-test",
        "messages": [
            {"role": "system", "content": "You are Seshat."},
            {"role": "user", "content": "hello"},
        ],
        "tools": [{"type": "function", "function": {"name": "web_search", "parameters": {}}}],
        "max_tokens": 256,
        "temperature": 0.2,
        "tool_choice": "auto",
        "api_key": SEEDED_KEY,
        "api_base": "https://user:password-in-url@example.invalid/v1",
        "extra_headers": {"Authorization": SEEDED_BEARER},
        "client": object(),
    }
    request.update(overrides)
    return request


def _record(**request_overrides: Any) -> dict[str, object]:
    return prompt_debug.build_record(
        trace_id=TRACE_ID,
        session_id=SESSION_ID,
        role="primary",
        model="anthropic/claude-test",
        provider="anthropic",
        placement="cloud",
        started_at=datetime(2026, 10, 18, 9, 0, tzinfo=timezone.utc),
        duration_ms=1234,
        request=_request(**request_overrides),
        usage={"prompt_tokens": 100, "completion_tokens": 7, "cache_read_input_tokens": 80},
    )


class TestRecord:
    def test_record_holds_the_full_request_and_the_usage(self) -> None:
        record = _record()
        assert record["trace_id"] == TRACE_ID
        assert record["session_id"] == SESSION_ID
        assert record["role"] == "primary"
        assert record["placement"] == "cloud"
        assert record["started_at"] == "2026-10-18T09:00:00+00:00"
        assert record["duration_ms"] == 1234
        assert record["messages"] == _request()["messages"]
        assert record["tools"] == _request()["tools"]
        assert record["params"] == {
            "model": "anthropic/claude-test",
            "max_tokens": 256,
            "temperature": 0.2,
            "tool_choice": "auto",
        }
        assert record["usage"] == {
            "prompt_tokens": 100,
            "completion_tokens": 7,
            "cache_read_input_tokens": 80,
        }

    def test_an_unknown_request_key_is_dropped(self) -> None:
        """Allow-list, not deny-list: a key nobody listed never reaches the file."""
        record = _record(some_future_kwarg="not-listed")
        assert "some_future_kwarg" not in json.dumps(record, default=str)


class TestSecrets:
    """AC-2: a seeded api_key and Authorization header never reach the file."""

    def test_seeded_secrets_are_absent_from_the_written_bytes(self, tmp_path: Path) -> None:
        path = prompt_debug.write_record(_record(), root=tmp_path)
        raw = path.read_bytes()
        assert SEEDED_KEY.encode() not in raw
        assert b"seeded-FRE1614-authorization-value" not in raw
        assert b"password-in-url" not in raw
        assert b"Authorization" not in raw
        # The file still holds the request itself.
        assert b"You are Seshat." in raw


class TestLayout:
    def test_file_lands_under_session_and_trace(self, tmp_path: Path) -> None:
        path = prompt_debug.write_record(_record(), root=tmp_path)
        assert path == tmp_path / SESSION_ID / TRACE_ID / "001-primary.json"
        assert json.loads(path.read_text(encoding="utf-8"))["seq"] == 1

    def test_the_file_is_private_to_its_owner(self, tmp_path: Path) -> None:
        path = prompt_debug.write_record(_record(), root=tmp_path)
        assert path.stat().st_mode & 0o777 == 0o600

    def test_sequence_numbers_count_up_within_a_trace(self, tmp_path: Path) -> None:
        first = prompt_debug.write_record(_record(), root=tmp_path)
        second = prompt_debug.write_record(_record(), root=tmp_path)
        assert first.name == "001-primary.json"
        assert second.name == "002-primary.json"
        assert json.loads(second.read_text(encoding="utf-8"))["seq"] == 2

    def test_a_taken_sequence_number_is_never_overwritten(self, tmp_path: Path) -> None:
        """A concurrent writer took 002 between the count and the open."""
        first = prompt_debug.write_record(_record(), root=tmp_path)
        (first.parent / "002-sub_agent.json").write_text("{}", encoding="utf-8")
        (first.parent / "unrelated.txt").write_text("x", encoding="utf-8")
        third = prompt_debug.write_record(_record(), root=tmp_path)
        assert third.name == "003-primary.json"
        assert (first.parent / "002-sub_agent.json").read_text(encoding="utf-8") == "{}"

    def test_a_missing_session_gets_a_placeholder_directory(self, tmp_path: Path) -> None:
        record = _record()
        record["session_id"] = None
        path = prompt_debug.write_record(record, root=tmp_path)
        assert path.parent.parent.name == "no-session"

    def test_path_components_cannot_escape_the_root(self, tmp_path: Path) -> None:
        record = _record()
        record["session_id"] = "../../etc"
        record["role"] = "primary/../../x"
        path = prompt_debug.write_record(record, root=tmp_path)
        assert path.resolve().is_relative_to(tmp_path.resolve())
        assert ".." not in path.relative_to(tmp_path).parts


class TestRetention:
    """AC-4: a file older than 7 days is deleted, a newer one is kept."""

    def test_old_file_deleted_new_file_kept(self, tmp_path: Path) -> None:
        old = prompt_debug.write_record(_record(), root=tmp_path)
        record = _record()
        record["trace_id"] = "1c7f3b75-4d20-4d9f-8b62-3e5e7d8f9ba1"
        new = prompt_debug.write_record(record, root=tmp_path)
        now = time.time()
        eight_days_ago = now - 8 * 86400
        # The trace's capture file and its .seq-001 marker were written together.
        for entry in [*old.parent.iterdir(), old.parent]:
            os.utime(entry, (eight_days_ago, eight_days_ago))
        one_day_ago = now - 86400
        os.utime(new, (one_day_ago, one_day_ago))

        deleted = prompt_debug.prune_old_captures(tmp_path, now=now)

        assert deleted == 2, "the capture file and its sequence marker"
        assert not old.exists()
        assert not old.parent.exists(), "an emptied old trace directory is removed"
        assert new.exists()

    def test_a_new_empty_directory_is_kept(self, tmp_path: Path) -> None:
        """A writer may have created it a moment ago and not yet opened its file."""
        fresh = tmp_path / SESSION_ID / TRACE_ID
        fresh.mkdir(parents=True)
        prompt_debug.prune_old_captures(tmp_path, now=time.time())
        assert fresh.is_dir()

    def test_a_missing_root_prunes_nothing(self, tmp_path: Path) -> None:
        assert prompt_debug.prune_old_captures(tmp_path / "absent", now=time.time()) == 0


class TestCapture:
    @pytest.mark.asyncio
    async def test_capture_writes_one_file(self, tmp_path: Path) -> None:
        from tests._helpers.trace import make_test_ctx

        ctx = make_test_ctx("fre1614")
        await prompt_debug.capture_model_call(
            trace_ctx=ctx,
            role="primary",
            model="anthropic/claude-test",
            provider="anthropic",
            placement="cloud",
            started_at=datetime.now(timezone.utc),
            duration_ms=5,
            request=_request(),
            usage={"prompt_tokens": 1},
            root=tmp_path,
        )
        files = list(tmp_path.rglob("*.json"))
        assert len(files) == 1
        assert json.loads(files[0].read_text(encoding="utf-8"))["trace_id"] == str(ctx.trace_id)

    @pytest.mark.asyncio
    async def test_a_write_failure_never_raises(self, tmp_path: Path) -> None:
        """The model call must not fail because the debug file could not be written."""
        from tests._helpers.trace import make_test_ctx

        blocker = tmp_path / "root-is-a-file"
        blocker.write_text("x", encoding="utf-8")
        await prompt_debug.capture_model_call(
            trace_ctx=make_test_ctx("fre1614"),
            role="primary",
            model="m",
            provider="p",
            placement="local",
            started_at=datetime.now(timezone.utc),
            duration_ms=5,
            request=_request(),
            usage={},
            root=blocker,
        )


def test_capture_is_off_by_default() -> None:
    from personal_agent.config.settings import AppConfig

    assert AppConfig.model_fields["prompt_debug_capture_enabled"].default is False


class _SecretHolder:
    def __repr__(self) -> str:
        return "SecretHolder(token=seeded-FRE1614-repr-secret)"

    __str__ = __repr__


def test_an_unknown_object_is_written_as_its_type_name_only(tmp_path: Path) -> None:
    """An allow-listed value that is not JSON never leaks its repr."""
    import httpx

    record = _record(
        response_format={"schema": _SecretHolder()},
        timeout=httpx.Timeout(10.0, read=600.0),
    )
    path = prompt_debug.write_record(record, root=tmp_path)
    raw = path.read_text(encoding="utf-8")
    assert "seeded-FRE1614-repr-secret" not in raw
    params = json.loads(raw)["params"]
    assert params["response_format"] == {"schema": "<_SecretHolder>"}
    assert params["timeout"]["read"] == 600.0


def test_publishing_leaves_no_temporary_file(tmp_path: Path) -> None:
    path = prompt_debug.write_record(_record(), root=tmp_path)
    assert sorted(p.name for p in path.parent.iterdir()) == [".seq-001", "001-primary.json"]


def test_a_number_reserved_by_another_role_is_not_reused(tmp_path: Path) -> None:
    """A concurrent sub_agent writer reserved 001 but has not published yet."""
    trace_dir = tmp_path / SESSION_ID / TRACE_ID
    trace_dir.mkdir(parents=True)
    (trace_dir / ".seq-001").touch()
    path = prompt_debug.write_record(_record(), root=tmp_path)
    assert path.name == "002-primary.json"
    assert json.loads(path.read_text(encoding="utf-8"))["seq"] == 2


def test_every_capture_directory_is_private(tmp_path: Path) -> None:
    root = tmp_path / "prompt_debug"
    path = prompt_debug.write_record(_record(), root=root)
    for directory in (root, path.parent.parent, path.parent):
        assert directory.stat().st_mode & 0o777 == 0o700, directory


def test_captures_are_ignored_by_git() -> None:
    """A host process with capture on must not leave committable private files."""
    import subprocess

    repo = Path(__file__).resolve().parents[3]
    result = subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "check-ignore",
            "-q",
            "telemetry/prompt_debug/s/t/001-primary.json",
        ],
        check=False,
    )
    assert result.returncode == 0


@pytest.mark.asyncio
async def test_startup_prune_runs_whether_capture_is_on_or_off(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from personal_agent.config import settings

    monkeypatch.setattr(settings, "prompt_debug_capture_enabled", False)
    monkeypatch.setattr(prompt_debug, "prompt_debug_root", lambda: tmp_path)
    old = prompt_debug.write_record(_record(), root=tmp_path)
    eight_days_ago = time.time() - 8 * 86400
    os.utime(old, (eight_days_ago, eight_days_ago))

    await prompt_debug.prune_captures_at_startup()

    assert not old.exists()
