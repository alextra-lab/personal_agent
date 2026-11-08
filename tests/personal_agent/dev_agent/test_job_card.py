# ruff: noqa: D101, D102
"""The job card payload and the event sink (ADR-0159 D10.6; FRE-1688)."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from decimal import Decimal
from uuid import UUID, uuid4

import pytest
from personal_agent.dev_agent.job_card import (
    CardContext,
    NullJobEventSink,
    PoolJobEventSink,
    card_payload,
)

pytestmark = pytest.mark.asyncio

CONTEXT = CardContext(cap_usd=Decimal("2.36"), repo_base_url="https://github.com/example-org")


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "id": uuid4(),
        "session_id": uuid4(),
        "mode": "build",
        "state": "running",
        "failure_reason": None,
        "check_result": None,
        "repo": "page-abc",
        "result_artifact_id": None,
        "main_moved": None,
        "spend_usd": Decimal("0.123456"),
    }
    row.update(overrides)
    return row


class FakePool:
    """Returns one row for the job id, or ``None``."""

    def __init__(self, row: Mapping[str, object] | None) -> None:
        self.row = row
        self.reads = 0

    async def fetchrow(self, sql: str, job_id: UUID) -> Mapping[str, object] | None:
        self.reads += 1
        return self.row


class Emitted:
    def __init__(self, ok: bool = True, fail: bool = False) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []
        self.ok = ok
        self.fail = fail

    async def __call__(self, *, session_id: str, payload: dict[str, object]) -> bool:
        if self.fail:
            raise RuntimeError("emit failed")
        self.calls.append((session_id, payload))
        return self.ok


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class TestPayload:
    async def test_spend_is_the_row_value_as_an_exact_string(self) -> None:
        row = _row(spend_usd=Decimal("0.123456"))
        payload = card_payload(row, event="state", step=None, context=CONTEXT)
        assert payload["spend_usd"] == str(row["spend_usd"]) == "0.123456"

    async def test_a_job_with_no_spend_row_has_no_spend(self) -> None:
        payload = card_payload(_row(spend_usd=None), event="state", step=None, context=CONTEXT)
        assert payload["spend_usd"] is None

    async def test_the_cap_and_repo_link_come_from_the_context(self) -> None:
        payload = card_payload(_row(), event="state", step=None, context=CONTEXT)
        assert payload["cap_usd"] == "2.36"
        assert payload["repo_url"] == "https://github.com/example-org/page-abc"

    async def test_no_repo_means_no_link(self) -> None:
        payload = card_payload(_row(repo=None), event="state", step=None, context=CONTEXT)
        assert payload["repo_url"] is None

    async def test_check_result_text_from_the_pool_is_decoded_to_an_object(self) -> None:
        result = {"passed": False, "steps": [{"name": "load", "passed": False, "messages": ["x"]}]}
        text_row = _row(state="failed_check", check_result=json.dumps(result))
        object_row = _row(state="failed_check", check_result=result)
        assert (
            card_payload(text_row, event="end", step=None, context=CONTEXT)["check_result"]
            == result
        )
        assert (
            card_payload(object_row, event="end", step=None, context=CONTEXT)["check_result"]
            == result
        )

    async def test_the_card_gets_the_check_steps_and_not_the_digests(self) -> None:
        stored = {
            "passed": False,
            "steps": [{"name": "load", "passed": False, "messages": ["x"]}],
            "image_digest": "sha256:aaa",
            "toolkit_digest": "sha256:bbb",
            "csp_manifest_digest": "sha256:ccc",
        }
        row = _row(state="failed_check", check_result=json.dumps(stored))
        payload = card_payload(row, event="end", step=None, context=CONTEXT)
        assert payload["check_result"] == {"passed": False, "steps": stored["steps"]}

    @pytest.mark.parametrize(
        ("state", "ended"),
        [
            ("running", False),
            ("checking", False),
            ("delivering", False),
            ("succeeded", True),
            ("failed", True),
            ("failed_check", True),
            ("cap_reached", True),
            ("cost_unknown", True),
            ("timed_out", True),
            ("cancelled", True),
        ],
    )
    async def test_ended_follows_the_state(self, state: str, ended: bool) -> None:
        payload = card_payload(_row(state=state), event="state", step=None, context=CONTEXT)
        assert payload["ended"] is ended

    async def test_the_payload_is_json_serializable_and_carries_the_ids_as_text(self) -> None:
        artifact = uuid4()
        row = _row(state="succeeded", result_artifact_id=artifact, main_moved=False)
        payload = card_payload(row, event="end", step="step 3", context=CONTEXT)
        decoded = json.loads(json.dumps(payload))
        assert decoded["job_id"] == str(row["id"])
        assert decoded["result_artifact_id"] == str(artifact)
        assert decoded["main_moved"] is False
        assert decoded["event"] == "end"
        assert decoded["step"] == "step 3"


class TestSink:
    def _sink(
        self, row: Mapping[str, object] | None, emit: Emitted, clock: Clock | None = None
    ) -> tuple[PoolJobEventSink, FakePool]:
        pool = FakePool(row)
        sink = PoolJobEventSink(
            pool,  # type: ignore[arg-type]
            emit,
            context=CONTEXT,
            clock=clock or Clock(),
            step_interval_seconds=2.0,
        )
        return sink, pool

    async def test_a_state_event_goes_to_the_jobs_session(self) -> None:
        row = _row()
        emit = Emitted()
        sink, _ = self._sink(row, emit)
        await sink.notify(row["id"], "state")  # type: ignore[arg-type]
        assert [(sid, p["event"], p["state"]) for sid, p in emit.calls] == [
            (str(row["session_id"]), "state", "running")
        ]

    async def test_a_missing_row_emits_nothing(self) -> None:
        emit = Emitted()
        sink, _ = self._sink(None, emit)
        await sink.notify(uuid4(), "end")
        assert emit.calls == []

    async def test_steps_are_throttled_and_a_repeated_step_is_skipped(self) -> None:
        row = _row()
        emit = Emitted()
        clock = Clock()
        sink, _ = self._sink(row, emit, clock)
        job_id: UUID = row["id"]  # type: ignore[assignment]
        await sink.notify(job_id, "step", "step 1")
        clock.now += 0.5
        await sink.notify(job_id, "step", "step 2")  # too soon
        clock.now += 2.0
        await sink.notify(job_id, "step", "step 2")  # late enough, new step
        clock.now += 2.0
        await sink.notify(job_id, "step", "step 2")  # the same step
        assert [p["step"] for _, p in emit.calls] == ["step 1", "step 2"]

    async def test_a_state_event_keeps_the_latest_step_and_is_never_throttled(self) -> None:
        row = _row()
        emit = Emitted()
        sink, _ = self._sink(row, emit)
        job_id: UUID = row["id"]  # type: ignore[assignment]
        await sink.notify(job_id, "step", "tool shell")
        await sink.notify(job_id, "state")
        await sink.notify(job_id, "state")
        assert [(p["event"], p["step"]) for _, p in emit.calls] == [
            ("step", "tool shell"),
            ("state", "tool shell"),
            ("state", "tool shell"),
        ]

    async def test_only_a_running_job_carries_the_agents_step(self) -> None:
        job_id = uuid4()
        emit = Emitted()
        running = _row(id=job_id, state="running")
        sink, pool = self._sink(running, emit)
        await sink.notify(job_id, "step", "tool shell")
        pool.row = _row(id=job_id, state="checking")
        await sink.notify(job_id, "state")
        assert [(p["state"], p["step"]) for _, p in emit.calls] == [
            ("running", "tool shell"),
            ("checking", None),
        ]

    async def test_a_send_that_hangs_ends_at_the_timeout(self) -> None:
        class Hangs:
            async def __call__(self, *, session_id: str, payload: dict[str, object]) -> bool:
                await asyncio.Event().wait()
                return True

        row = _row()
        sink = PoolJobEventSink(
            FakePool(row),  # type: ignore[arg-type]
            Hangs(),
            context=CONTEXT,
            timeout_seconds=0.1,
        )
        await asyncio.wait_for(sink.notify(row["id"], "step", "tool shell"), timeout=2)  # type: ignore[arg-type]

    async def test_a_failed_send_does_not_hide_the_step_for_good(self) -> None:
        row = _row()
        emit = Emitted(fail=True)
        sink, _ = self._sink(row, emit)
        job_id: UUID = row["id"]  # type: ignore[assignment]
        await sink.notify(job_id, "step", "tool shell")
        assert emit.calls == []
        emit.fail = False
        await sink.notify(job_id, "step", "tool shell")  # the same step: it was never shown
        assert [p["step"] for _, p in emit.calls] == ["tool shell"]

    async def test_a_send_that_was_not_persisted_does_not_mark_the_step_shown(self) -> None:
        row = _row()
        emit = Emitted(ok=False)
        sink, _ = self._sink(row, emit)
        job_id: UUID = row["id"]  # type: ignore[assignment]
        await sink.notify(job_id, "step", "tool shell")
        emit.ok = True
        await sink.notify(job_id, "step", "tool shell")
        assert len(emit.calls) == 2

    async def test_a_step_after_the_end_is_ignored(self) -> None:
        row = _row(state="succeeded")
        emit = Emitted()
        sink, pool = self._sink(row, emit)
        job_id: UUID = row["id"]  # type: ignore[assignment]
        await sink.notify(job_id, "end")
        reads = pool.reads
        await sink.notify(job_id, "step", "late step")
        assert pool.reads == reads
        assert [p["event"] for _, p in emit.calls] == ["end"]

    async def test_a_failing_emit_never_raises(self) -> None:
        row = _row()
        sink, _ = self._sink(row, Emitted(fail=True))
        await sink.notify(row["id"], "end")  # type: ignore[arg-type]

    async def test_a_failing_read_never_raises(self) -> None:
        class Broken:
            async def fetchrow(self, sql: str, job_id: UUID) -> None:
                raise ConnectionError("pool closed")

        emit = Emitted()
        sink = PoolJobEventSink(Broken(), emit, context=CONTEXT)  # type: ignore[arg-type]
        await sink.notify(uuid4(), "end")
        assert emit.calls == []

    async def test_the_null_sink_does_nothing(self) -> None:
        await NullJobEventSink().notify(uuid4(), "end")


class TestCardContext:
    async def test_a_failed_cap_read_is_not_kept(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from personal_agent.dev_agent import job_card
        from personal_agent.dev_broker import config as broker_config

        monkeypatch.setattr(job_card, "_context", None)
        real = broker_config.load_broker_config
        monkeypatch.setattr(
            broker_config, "load_broker_config", lambda: (_ for _ in ()).throw(OSError("seeded"))
        )
        assert job_card.card_context().cap_usd is None

        monkeypatch.setattr(broker_config, "load_broker_config", real)
        assert job_card.card_context().cap_usd == real().job_cap_usd

    async def test_a_malformed_broker_file_gives_a_card_with_no_cap(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import yaml

        from personal_agent.dev_agent import job_card
        from personal_agent.dev_broker import config as broker_config

        monkeypatch.setattr(job_card, "_context", None)
        monkeypatch.setattr(
            broker_config,
            "load_broker_config",
            lambda: (_ for _ in ()).throw(yaml.YAMLError("seeded: bad syntax")),
        )
        assert job_card.card_context().cap_usd is None
