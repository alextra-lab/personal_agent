"""FRE-1122 — the manifest binding and the anti-vacuous-green validator.

Codex round 1 found that every phase read the original YAML independently, so a
preflight replacement never reached the run, and that ``report`` would happily
render a "0 / 0" baseline from an empty artifact and exit zero. Both are ways a
broken run produces a clean number, which is the failure this fixture exists to
avoid rather than commit.

These tests pin the refusals. Each one is a way the chain could silently
decouple; if any stops raising, a wrong baseline becomes reportable again.
"""

from __future__ import annotations

import json
import pathlib

import pytest
import yaml
from scripts.eval.fre1122_absence_probe.manifest import (
    MANIFEST_NAME,
    ManifestError,
    load_manifest,
    write_manifest,
)
from scripts.eval.fre1122_absence_probe.probes import load_probe_set

_USER = "00000000-0000-0000-0000-000000000000"
_OTHER_USER = "11111111-1111-1111-1111-111111111111"


def _probe_yaml(
    tmp_path: pathlib.Path, *, question: str = "What is my boat called?"
) -> pathlib.Path:
    path = tmp_path / "probe_set.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "probes": [
                    {
                        "probe_id": "absent-01",
                        "status": "absent",
                        "question": question,
                        "subject_terms": ["boat"],
                        "personal_scope_rationale": (
                            "a private fact about the owner's life, unobtainable "
                            "from training data or any public source"
                        ),
                    }
                ]
            }
        )
    )
    return path


def _write(tmp_path: pathlib.Path, probe_path: pathlib.Path, **overrides: object):  # type: ignore[no-untyped-def]
    probe_set = load_probe_set(probe_path)
    kwargs: dict[str, object] = {
        "probes": probe_set.probes,
        "user_id": _USER,
        "probe_set_path": probe_path,
        "ground_truth_holds": True,
        "replacements": (),
        "created_at": "2026-08-03T00:00:00+00:00",
    }
    kwargs.update(overrides)
    return write_manifest(tmp_path, **kwargs)  # type: ignore[arg-type]


# ── The binding ───────────────────────────────────────────────────────────────


def test_a_missing_manifest_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A phase cannot run before preflight has fixed the effective probes."""
    probe_path = _probe_yaml(tmp_path)
    with pytest.raises(ManifestError, match="preflight"):
        load_manifest(
            tmp_path,
            probe_set=load_probe_set(probe_path),
            probe_set_path=probe_path,
            user_id=_USER,
        )


def test_a_manifest_for_a_different_owner_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Ground truth established against one corpus does not transfer to another."""
    probe_path = _probe_yaml(tmp_path)
    _write(tmp_path, probe_path)

    with pytest.raises(ManifestError, match="different corpus"):
        load_manifest(
            tmp_path,
            probe_set=load_probe_set(probe_path),
            probe_set_path=probe_path,
            user_id=_OTHER_USER,
        )


def test_editing_the_probe_set_after_preflight_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the run fires probes whose status was never established."""
    probe_path = _probe_yaml(tmp_path)
    _write(tmp_path, probe_path)

    _probe_yaml(tmp_path, question="What is my other boat called?")

    with pytest.raises(ManifestError, match="changed after preflight"):
        load_manifest(
            tmp_path,
            probe_set=load_probe_set(probe_path),
            probe_set_path=probe_path,
            user_id=_USER,
        )


def test_a_failed_preflight_blocks_the_run(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A probe whose status is wrong makes the baseline meaningless."""
    probe_path = _probe_yaml(tmp_path)
    _write(tmp_path, probe_path, ground_truth_holds=False)

    with pytest.raises(ManifestError, match="did NOT hold"):
        load_manifest(
            tmp_path,
            probe_set=load_probe_set(probe_path),
            probe_set_path=probe_path,
            user_id=_USER,
        )


def test_postcheck_may_still_load_a_failed_manifest(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Cleanup must remain possible after a run that should not have happened."""
    probe_path = _probe_yaml(tmp_path)
    _write(tmp_path, probe_path, ground_truth_holds=False)

    manifest = load_manifest(
        tmp_path,
        probe_set=load_probe_set(probe_path),
        probe_set_path=probe_path,
        user_id=_USER,
        require_ground_truth=False,
    )

    assert manifest.ground_truth_holds is False


def test_the_manifest_round_trips_the_effective_probes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The replacement recorded by preflight is what later phases must fire."""
    probe_path = _probe_yaml(tmp_path)
    written = _write(tmp_path, probe_path)

    reloaded = load_manifest(
        tmp_path,
        probe_set=load_probe_set(probe_path),
        probe_set_path=probe_path,
        user_id=_USER,
    )

    assert reloaded.digest == written.digest
    assert reloaded.probes[0].subject_terms == ("boat",)
    assert isinstance(reloaded.probes[0].subject_terms, tuple)


def test_the_digest_changes_when_a_question_changes(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The digest is what binds a run's answers to a run's probes."""
    first = _write(tmp_path, _probe_yaml(tmp_path))
    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _write(second_dir, _probe_yaml(second_dir, question="Something else?"))

    assert first.digest != second.digest


# ── The anti-vacuous-green validator ──────────────────────────────────────────


def _validator():  # type: ignore[no-untyped-def]
    from scripts.eval.fre1122_absence_probe.runner import _validate_run_artifact

    return _validate_run_artifact


def test_an_empty_answers_artifact_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """This produced "0 / 0" and exited zero — a baseline with no data."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    with pytest.raises(ManifestError, match="no answers"):
        _validator()(manifest, {"answers": [], "manifest_digest": manifest.digest})


def test_answers_from_a_different_run_are_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Otherwise one run's answers get attributed to another run's probes."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    with pytest.raises(ManifestError, match="different manifest"):
        _validator()(
            manifest,
            {
                "manifest_digest": "deadbeef",
                "answers": [
                    {
                        "probe_id": "absent-01",
                        "outcome": "declared_absence",
                        "trace_id": "t-1",
                    }
                ],
            },
        )


def test_a_missing_probe_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Partial coverage must not be reportable as a rate."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    with pytest.raises(ManifestError, match="do not match"):
        _validator()(
            manifest,
            {
                "manifest_digest": manifest.digest,
                "answers": [
                    {"probe_id": "not-a-probe", "outcome": "declared_absence", "trace_id": "t"}
                ],
            },
        )


def test_an_answer_without_a_trace_id_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-5 traces every confabulation to the memory items behind it."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    with pytest.raises(ManifestError, match="trace_id"):
        _validator()(
            manifest,
            {
                "manifest_digest": manifest.digest,
                "answers": [
                    {"probe_id": "absent-01", "outcome": "declared_absence", "trace_id": ""}
                ],
            },
        )


def test_an_unknown_outcome_is_refused(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-4 requires every answer carry exactly one of the known outcomes."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    with pytest.raises(ManifestError, match="unknown outcome"):
        _validator()(
            manifest,
            {
                "manifest_digest": manifest.digest,
                "answers": [{"probe_id": "absent-01", "outcome": "probably_fine", "trace_id": "t"}],
            },
        )


def test_a_complete_artifact_validates(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The happy path must still pass, or the guard is just a blocker."""
    manifest = _write(tmp_path, _probe_yaml(tmp_path))

    _validator()(
        manifest,
        {
            "manifest_digest": manifest.digest,
            "answers": [
                {"probe_id": "absent-01", "outcome": "declared_absence", "trace_id": "t-1"}
            ],
        },
    )


def test_the_manifest_file_is_named_where_phases_look_for_it(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A rename would decouple the phases silently rather than loudly."""
    _write(tmp_path, _probe_yaml(tmp_path))

    assert (tmp_path / MANIFEST_NAME).exists()
    assert json.loads((tmp_path / MANIFEST_NAME).read_text())["digest"]
