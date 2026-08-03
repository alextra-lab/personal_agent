"""FRE-1122 — the run-phase authorization gate, and the committed template.

The gate is the only thing standing between running this fixture's harmless
phases and firing twenty real turns at the live gateway under the owner's
identity, permanently writing to the real corpus. It is tested rather than
trusted.

The template test is an anti-vacuous check in the FRE-435 tradition: the
construction rules in ``probes.py`` are only worth anything if the committed
example set actually satisfies them, so a rule that silently stops being
enforced fails here rather than at run time.
"""

from __future__ import annotations

import pathlib

import pytest
from scripts.eval.fre1122_absence_probe.probes import load_probe_set
from scripts.eval.fre1122_absence_probe.runner import main

_TEMPLATE = (
    pathlib.Path(__file__).resolve().parents[2]
    / "scripts"
    / "eval"
    / "fre1122_absence_probe"
    / "probe_set.template.yaml"
)


# ── The authorization gate ────────────────────────────────────────────────────


def test_the_run_phase_refuses_without_authorization(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """No ``--authorized-by`` means no turns, and a non-zero exit."""
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            "run",
            "--probe-set",
            str(_TEMPLATE),
            "--user-id",
            "00000000-0000-0000-0000-000000000000",
        ],
    )

    assert main() == 2
    assert "authorized-by" in capsys.readouterr().err


def test_the_refusal_happens_before_the_probe_set_is_even_loaded(monkeypatch, capsys) -> None:  # type: ignore[no-untyped-def]
    """The gate must not be reachable past a malformed-probe-set error.

    If the refusal came after loading, a probe set that failed to parse would
    raise before the gate ever ran — and a caller fixing the parse error would
    then be one retry away from an unauthorized live run.
    """
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            "run",
            "--probe-set",
            "/nonexistent/probe_set.yaml",
            "--user-id",
            "00000000-0000-0000-0000-000000000000",
        ],
    )

    assert main() == 2
    assert "authorized-by" in capsys.readouterr().err


def test_a_missing_user_id_is_also_refused(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Without the owner's id the queries would silently scope to nothing."""
    monkeypatch.setattr("sys.argv", ["runner.py", "preflight", "--probe-set", str(_TEMPLATE)])

    assert main() == 2


# ── The committed template ────────────────────────────────────────────────────


def test_the_committed_template_satisfies_its_own_construction_rules() -> None:
    """A rule that stops being enforced fails here, not at run time."""
    probe_set = load_probe_set(_TEMPLATE)

    assert probe_set.present_probes, "the template must show a worked present probe"
    assert probe_set.absent_probes, "the template must show a worked absent probe"
    assert probe_set.absent_pool, "the template must show the pre-registered pool"

    for probe in (*probe_set.probes, *probe_set.absent_pool):
        assert probe.personal_scope_rationale.strip(), probe.probe_id
        assert probe.subject_terms, probe.probe_id

    for probe in probe_set.absent_probes:
        assert not probe.expected_tokens
        assert probe.expected_source is None

    for probe in probe_set.present_probes:
        assert probe.expected_tokens
        assert probe.expected_source


def test_the_template_carries_no_real_owner_content() -> None:
    """The template is committed to a public repository.

    The real probe set is gitignored precisely because AC-2 and AC-7 force real
    personal content into it. The template must stay free of that, so its
    example identifiers are placeholders.
    """
    probe_set = load_probe_set(_TEMPLATE)

    for probe in probe_set.present_probes:
        assert probe.expected_source is not None
        assert set(probe.expected_source.split(":")[-1]) <= set("0-"), (
            f"{probe.probe_id}: expected_source must stay a placeholder in the "
            "committed template, not a real stored-row identifier"
        )


@pytest.mark.parametrize("phase", ["preflight", "postcheck"])
def test_read_and_cleanup_phases_do_not_require_authorization(monkeypatch, phase: str) -> None:  # type: ignore[no-untyped-def]
    """Only the turn-firing phase is gated.

    Preflight is read-only and postcheck defaults to a dry run, so gating them
    would train the operator to pass the authorization flag reflexively — which
    is exactly how the gate on the phase that matters gets defeated.

    ``_dispatch`` is stubbed rather than executed: these phases open Neo4j and
    Postgres connections, and a unit test must never reach a substrate
    (FRE-375). What is under test is the gate's routing, not the phase body.
    """
    dispatched: list[str] = []

    async def _stub(args) -> int:  # type: ignore[no-untyped-def]
        dispatched.append(args.phase)
        return 0

    monkeypatch.setattr("scripts.eval.fre1122_absence_probe.runner._dispatch", _stub)
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            phase,
            "--probe-set",
            str(_TEMPLATE),
            "--user-id",
            "00000000-0000-0000-0000-000000000000",
        ],
    )

    assert main() == 0
    assert dispatched == [phase], "the phase was refused before it could dispatch"


def test_the_run_phase_never_dispatches_when_unauthorized(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The gate must stop the run *before* dispatch, not inside it.

    A refusal that happened after dispatch would already have opened the client
    and, depending on ordering, fired the first turn.
    """
    dispatched: list[str] = []

    async def _stub(args) -> int:  # type: ignore[no-untyped-def]
        dispatched.append(args.phase)
        return 0

    monkeypatch.setattr("scripts.eval.fre1122_absence_probe.runner._dispatch", _stub)
    monkeypatch.setattr(
        "sys.argv",
        [
            "runner.py",
            "run",
            "--probe-set",
            str(_TEMPLATE),
            "--user-id",
            "00000000-0000-0000-0000-000000000000",
        ],
    )

    assert main() == 2
    assert dispatched == [], "an unauthorized run reached dispatch"
