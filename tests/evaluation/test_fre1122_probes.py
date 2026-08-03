"""FRE-1122 — probe-set schema and its load-time validation (AC-1, AC-2, AC-7).

The validation here is what makes the acceptance criteria enforceable rather
than aspirational: a probe set that violates AC-7's personal-scoping rule or
AC-2's stored-row requirement fails to load at all, so it cannot reach a run and
quietly produce a number that means something else.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1122_absence_probe.probes import (
    Probe,
    ProbeSetError,
    load_probe_set,
    validate_run_shape,
)


def _present(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "probe_id": "present-01",
        "status": "present",
        "question": "What did I say the diffraction limit depends on?",
        "subject_terms": ["diffraction limit"],
        "personal_scope_rationale": "the owner's own framing of it in a stored turn",
        "expected_tokens": ["wavelength", "numerical aperture"],
        "expected_source": "Turn:abc-123",
    }
    base.update(overrides)
    return base


def _absent(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "probe_id": "absent-01",
        "status": "absent",
        "question": "What did I say my sister's dog is called?",
        "subject_terms": ["sister's dog"],
        "personal_scope_rationale": (
            "a private fact about the owner's family; unobtainable from training data"
        ),
    }
    base.update(overrides)
    return base


def _write(tmp_path, probes, absent_pool=()):  # type: ignore[no-untyped-def]
    import yaml

    path = tmp_path / "probe_set.yaml"
    path.write_text(yaml.safe_dump({"probes": list(probes), "absent_pool": list(absent_pool)}))
    return path


# ── Happy path ────────────────────────────────────────────────────────────────


def test_a_well_formed_probe_set_loads(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A set satisfying every construction rule loads and splits by half."""
    probe_set = load_probe_set(_write(tmp_path, [_present(), _absent()]))

    assert len(probe_set.probes) == 2
    assert isinstance(probe_set.probes[0], Probe)
    assert probe_set.present_probes[0].expected_tokens == ("wavelength", "numerical aperture")
    assert probe_set.absent_probes[0].expected_tokens == ()


# ── AC-7: personal scoping, both halves ───────────────────────────────────────


def test_an_absent_probe_without_a_personal_scope_rationale_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-7: a publicly knowable subject gets answered from weights and proves nothing."""
    with pytest.raises(ProbeSetError, match="personal_scope_rationale"):
        load_probe_set(_write(tmp_path, [_absent(personal_scope_rationale="")]))


def test_a_present_probe_also_requires_a_personal_scope_rationale(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-7's argument applies to the present half too.

    A correct answer to a publicly knowable *present* probe is equally
    uninformative: the model could have produced it from training rather than
    from the store, so it evidences knowledge rather than recall. AC-7 mandates
    the rule for absent probes; applying it to both is what makes
    ASSERTED_CORRECT mean recall.
    """
    with pytest.raises(ProbeSetError, match="personal_scope_rationale"):
        load_probe_set(_write(tmp_path, [_present(personal_scope_rationale="")]))


# ── AC-2: the present half names its stored row ───────────────────────────────


def test_a_present_probe_without_expected_tokens_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-2: without expected tokens there is nothing to score correctness against."""
    with pytest.raises(ProbeSetError, match="expected_tokens"):
        load_probe_set(_write(tmp_path, [_present(expected_tokens=[])]))


def test_a_present_probe_without_a_stored_source_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """AC-2 requires the specific stored row, by identifier."""
    with pytest.raises(ProbeSetError, match="expected_source"):
        load_probe_set(_write(tmp_path, [_present(expected_source=None)]))


# ── The absent half carries no expected content ───────────────────────────────


def test_an_absent_probe_carrying_expected_tokens_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Expected content on an absent probe contradicts its own ground truth."""
    with pytest.raises(ProbeSetError, match="expected_tokens"):
        load_probe_set(_write(tmp_path, [_absent(expected_tokens=["bramble"])]))


# ── Structural integrity ──────────────────────────────────────────────────────


def test_duplicate_probe_ids_are_rejected_across_set_and_pool(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A replacement drawn from the pool must be distinguishable in the report."""
    with pytest.raises(ProbeSetError, match="duplicate"):
        load_probe_set(
            _write(tmp_path, [_absent()], absent_pool=[_absent()]),
        )


def test_a_non_absent_probe_in_the_pool_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The pool exists to replace absent probes only."""
    with pytest.raises(ProbeSetError, match="absent_pool"):
        load_probe_set(
            _write(tmp_path, [_absent()], absent_pool=[_present(probe_id="present-02")]),
        )


def test_a_probe_without_subject_terms_is_rejected(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Without terms there is no query to evidence AC-1/AC-3 with."""
    with pytest.raises(ProbeSetError, match="subject_terms"):
        load_probe_set(_write(tmp_path, [_absent(subject_terms=[])]))


# ── Run shape (AC: ten and ten) ───────────────────────────────────────────────


def test_validate_run_shape_requires_ten_and_ten(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A partial set is loadable while being authored, but not reportable."""
    probe_set = load_probe_set(_write(tmp_path, [_present(), _absent()]))

    with pytest.raises(ProbeSetError, match="ten"):
        validate_run_shape(probe_set)


def test_validate_run_shape_accepts_a_full_set(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Ten present and ten absent is the shape a reportable run requires."""
    probes = [_present(probe_id=f"present-{i:02d}") for i in range(10)]
    probes += [_absent(probe_id=f"absent-{i:02d}") for i in range(10)]

    validate_run_shape(load_probe_set(_write(tmp_path, probes)))
