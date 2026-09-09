"""FRE-1122 — the run phase: identity, session isolation, and surviving a slow turn.

Three defects are under test, all recorded by master after two failed run
attempts on 2026-08-04.

**Identity.** The phase sent no ``Cf-Access-Authenticated-User-Email`` and got a
401. Sending one is not enough on its own: an unknown address is *upserted* into
``users`` and comes back as a fresh id, so a run under the wrong email measures
an empty corpus and reports a clean, meaningless baseline. The email and
``--user-id`` are therefore bound before a single turn fires.

**Session isolation.** All twenty probes threaded one session, so every probe
after the first was answered inside the conversation history of the ones before
it. One session per probe.

**Surviving a slow turn.** A legitimate turn ran 318.8 seconds against a
hardcoded 300-second ceiling; the phase aborted and nine completed turns were
lost. Tolerating that is only useful if the retry fires the probes that are
*missing* — the absent probes are single-use, and refiring a spent one answers
against a corpus that now contains its own first firing. So the ledger separates
"never reached the service" from "may have completed server-side", and only the
first is ever refired.

No substrate and no service are touched (FRE-375): the HTTP client and the
database connection are fakes.
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any

import httpx
import pytest
import yaml
from scripts.eval.fre1122_absence_probe import runner
from scripts.eval.fre1122_absence_probe.manifest import write_manifest
from scripts.eval.fre1122_absence_probe.probes import load_probe_set

_USER = "11111111-1111-1111-1111-111111111111"
_EMAIL = "fre1122-baseline@example.test"


# ── Fixture construction ──────────────────────────────────────────────────────


def _full_probe_set(tmp_path: pathlib.Path) -> pathlib.Path:
    """A valid ten-and-ten probe set. Rationales must be unique per probe."""
    probes: list[dict[str, object]] = []
    for i in range(10):
        probes.append(
            {
                "probe_id": f"present-{i:02d}",
                "status": "present",
                "question": f"What did I say about topic {i}?",
                "subject_terms": [f"topic {i}"],
                "personal_scope_rationale": (
                    f"the owner's own recorded framing of topic {i}, which exists in "
                    "no public source and only in this corpus"
                ),
                "expected_tokens": [f"token{i}"],
                "expected_source": "Turn:00000000-0000-0000-0000-000000000000",
            }
        )
        probes.append(
            {
                "probe_id": f"absent-{i:02d}",
                "status": "absent",
                "question": f"What did I say my subject {i} is called?",
                "subject_terms": [f"subject {i}"],
                "personal_scope_rationale": (
                    f"a private fact about the owner's subject {i}; unobtainable from "
                    "training data or any public source, and not inferable"
                ),
            }
        )
    path = tmp_path / "probe_set.yaml"
    path.write_text(yaml.safe_dump({"probes": probes, "absent_pool": []}))
    return path


def _prepared(tmp_path: pathlib.Path, **overrides: Any) -> argparse.Namespace:
    """A probe set, a written manifest, and the args the run phase expects."""
    probe_set_path = _full_probe_set(tmp_path)
    probe_set = load_probe_set(probe_set_path)
    artifact_root = tmp_path / "artifacts"
    write_manifest(
        artifact_root,
        probes=probe_set.probes,
        user_id=_USER,
        probe_set_path=probe_set_path,
        ground_truth_holds=True,
        replacements=(),
        created_at="2026-09-08T00:00:00+00:00",
    )
    args = argparse.Namespace(
        authorized_by="the owner",
        artifact_root=artifact_root,
        probe_set=probe_set_path,
        user_id=_USER,
        auth_email=_EMAIL,
        service_url="http://localhost:9000",
        captures_root=tmp_path / "captures",
        turn_timeout=900.0,
        resume=False,
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class _FakePg:
    """A users-table lookup, and nothing else."""

    def __init__(self, row: dict[str, Any] | None) -> None:
        self._row = row
        self.closed = False

    async def fetchrow(self, _statement: str, *_args: Any) -> dict[str, Any] | None:
        return self._row

    async def close(self) -> None:
        self.closed = True


class _FakeResponse:
    def __init__(self, payload: dict[str, Any]) -> None:
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, Any]:
        return self._payload


class _FakeClient:
    """Records every post, and can be told to fail specific probes."""

    instances: list[_FakeClient] = []

    def __init__(self, *, timeout: float | None = None, **_: Any) -> None:
        self.timeout = timeout
        self.posts: list[dict[str, Any]] = []
        self.failures: dict[str, Exception] = _FakeClient.next_failures
        _FakeClient.instances.append(self)

    next_failures: dict[str, Exception] = {}

    async def __aenter__(self) -> _FakeClient:
        return self

    async def __aexit__(self, *_: object) -> bool:
        return False

    async def post(
        self,
        url: str,
        *,
        params: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
    ) -> _FakeResponse:
        params = params or {}
        self.posts.append({"url": url, "params": dict(params), "headers": dict(headers or {})})
        message = params.get("message", "")
        for question, exc in self.failures.items():
            if question in message:
                raise exc
        index = len(self.posts)
        return _FakeResponse(
            {
                "session_id": f"session-{index:02d}",
                "response": "I have no record of that.",
                "trace_id": f"trace-{index:02d}",
            }
        )


@pytest.fixture(autouse=True)
def _wire_fakes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch the client and the database out of every test in this module."""
    _FakeClient.instances = []
    _FakeClient.next_failures = {}
    monkeypatch.setattr(runner.httpx, "AsyncClient", _FakeClient)

    async def _pg() -> _FakePg:
        return _FakePg({"user_id": _USER})

    monkeypatch.setattr(runner, "_open_pg", _pg)


async def _run(args: argparse.Namespace) -> int:
    return await runner._phase_run(args, load_probe_set(args.probe_set))


def _answers(args: argparse.Namespace) -> dict[str, Any]:
    return json.loads((args.artifact_root / "run_answers.json").read_text())


# ── Identity ──────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_run_phase_sends_the_owner_identity_header(tmp_path: pathlib.Path) -> None:
    """Without the header the service returns 401 and the run fires nothing."""
    args = _prepared(tmp_path)

    assert await _run(args) == 0

    client = _FakeClient.instances[0]
    assert client.posts
    for post in client.posts:
        assert post["headers"]["Cf-Access-Authenticated-User-Email"] == _EMAIL


@pytest.mark.asyncio
async def test_an_unknown_auth_email_is_refused_before_firing(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unknown address is upserted into ``users`` and gets a fresh id.

    The run would then measure an empty corpus and report a clean baseline for
    a user who has never said anything.
    """

    async def _pg() -> _FakePg:
        return _FakePg(None)

    monkeypatch.setattr(runner, "_open_pg", _pg)
    args = _prepared(tmp_path)

    with pytest.raises(runner.RunRefused):
        await _run(args)

    assert _FakeClient.instances == [], "a turn fired before the identity was bound"


@pytest.mark.asyncio
async def test_an_auth_email_for_another_user_is_refused(
    tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ground truth is scoped to --user-id; the turn is scoped to the header."""

    async def _pg() -> _FakePg:
        return _FakePg({"user_id": "99999999-9999-9999-9999-999999999999"})

    monkeypatch.setattr(runner, "_open_pg", _pg)
    args = _prepared(tmp_path)

    with pytest.raises(runner.RunRefused):
        await _run(args)

    assert _FakeClient.instances == []


def test_the_run_phase_requires_an_auth_email(monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The CLI refuses before dispatch, as it does for --authorized-by."""
    template = (
        pathlib.Path(__file__).resolve().parents[2]
        / "scripts"
        / "eval"
        / "fre1122_absence_probe"
        / "probe_set.template.yaml"
    )
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            "run",
            "--probe-set",
            str(template),
            "--user-id",
            _USER,
            "--authorized-by",
            "the owner",
        ],
    )

    assert runner.main() == 2
    assert "auth-email" in capsys.readouterr().err


# ── Session isolation ─────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_each_probe_gets_its_own_session(tmp_path: pathlib.Path) -> None:
    """No probe is answered inside another probe's conversation history."""
    args = _prepared(tmp_path)

    assert await _run(args) == 0

    client = _FakeClient.instances[0]
    assert len(client.posts) == 20
    for post in client.posts:
        assert "session_id" not in post["params"], "a probe was threaded onto another's session"

    artifact = _answers(args)
    assert len(set(artifact["session_ids"])) == 20


@pytest.mark.asyncio
async def test_the_compat_results_file_names_each_probes_own_session(
    tmp_path: pathlib.Path,
) -> None:
    """Every row must name its own probe's session, not the last one's."""
    args = _prepared(tmp_path)
    await _run(args)

    rows = json.loads((args.artifact_root / "results.json").read_text())
    sessions = [row["control"]["session_id"] for row in rows]
    assert len(set(sessions)) == len(rows)


# ── Surviving a slow turn ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_turn_timeout_is_configurable(tmp_path: pathlib.Path) -> None:
    """A legitimate five-iteration turn exceeded the hardcoded 300 seconds."""
    args = _prepared(tmp_path, turn_timeout=1234.0)
    await _run(args)

    assert _FakeClient.instances[0].timeout == 1234.0


@pytest.mark.asyncio
async def test_a_failing_probe_does_not_discard_completed_answers(
    tmp_path: pathlib.Path,
) -> None:
    """Losing nine good turns to one slow turn is the expensive behaviour."""
    _FakeClient.next_failures = {"topic 4": httpx.ReadTimeout("too slow")}
    args = _prepared(tmp_path)

    assert await _run(args) != 0, "an incomplete run must not report success"

    artifact = _answers(args)
    assert len(artifact["answers"]) == 19
    assert artifact["attempts"]["present-04"]["state"] == runner.STATE_SUBMITTED_UNKNOWN


@pytest.mark.asyncio
async def test_the_attempt_is_recorded_before_the_turn_is_fired(
    tmp_path: pathlib.Path,
) -> None:
    """A crash mid-turn must leave evidence that the turn may have happened."""
    seen: list[bool] = []
    original = _FakeClient.post

    async def _spy(self, url, *, params=None, headers=None):  # type: ignore[no-untyped-def]
        if not seen:
            seen.append((args.artifact_root / "run_answers.json").exists())
        return await original(self, url, params=params, headers=headers)

    args = _prepared(tmp_path)
    _FakeClient.post = _spy  # type: ignore[assignment]
    try:
        await _run(args)
    finally:
        _FakeClient.post = original  # type: ignore[assignment]

    assert seen == [True], "the ledger was not durable before the first request"


@pytest.mark.asyncio
async def test_a_submitted_probe_is_never_refired_on_resume(tmp_path: pathlib.Path) -> None:
    """A client-side timeout does not cancel the turn.

    The question is already in ``sessions.messages``, so refiring the probe
    answers against a corpus containing its own first firing. That destroys the
    ground truth rather than repeating the measurement.
    """
    _FakeClient.next_failures = {"topic 4": httpx.ReadTimeout("too slow")}
    args = _prepared(tmp_path)
    await _run(args)

    _FakeClient.next_failures = {}
    resumed = _prepared(tmp_path, resume=True)
    resumed.artifact_root = args.artifact_root
    resumed.probe_set = args.probe_set

    assert await _run(resumed) != 0, "an unresolved ambiguous probe must not report success"

    fired = [p["params"]["message"] for p in _FakeClient.instances[-1].posts]
    assert not any("topic 4" in m for m in fired), "a possibly-completed probe was refired"


@pytest.mark.asyncio
async def test_a_pre_submission_failure_is_refirable(tmp_path: pathlib.Path) -> None:
    """A connect error never reached the service, so the corpus is untouched."""
    _FakeClient.next_failures = {"topic 4": httpx.ConnectError("no route")}
    args = _prepared(tmp_path)
    await _run(args)

    assert _answers(args)["attempts"]["present-04"]["state"] == runner.STATE_NOT_SUBMITTED

    _FakeClient.next_failures = {}
    resumed = _prepared(tmp_path, resume=True)
    resumed.artifact_root = args.artifact_root
    resumed.probe_set = args.probe_set

    assert await _run(resumed) == 0

    fired = [p["params"]["message"] for p in _FakeClient.instances[-1].posts]
    assert len(fired) == 1
    assert "topic 4" in fired[0]
    assert len(_answers(args)["answers"]) == 20


@pytest.mark.asyncio
async def test_a_second_run_refuses_to_overwrite_existing_answers(
    tmp_path: pathlib.Path,
) -> None:
    """Refiring twenty spent probes voids the baseline silently."""
    args = _prepared(tmp_path)
    assert await _run(args) == 0

    again = _prepared(tmp_path)
    again.artifact_root = args.artifact_root
    again.probe_set = args.probe_set

    with pytest.raises(runner.RunRefused):
        await _run(again)


@pytest.mark.asyncio
async def test_resume_refuses_answers_from_another_manifest(tmp_path: pathlib.Path) -> None:
    """Those answers belong to a different probe set."""
    args = _prepared(tmp_path)
    await _run(args)

    artifact_path = args.artifact_root / "run_answers.json"
    artifact = json.loads(artifact_path.read_text())
    artifact["manifest_digest"] = "0" * 64
    artifact_path.write_text(json.dumps(artifact))

    resumed = _prepared(tmp_path, resume=True)
    resumed.artifact_root = args.artifact_root
    resumed.probe_set = args.probe_set

    with pytest.raises(Exception, match="manifest"):
        await _run(resumed)
