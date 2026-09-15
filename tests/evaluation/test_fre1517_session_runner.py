"""FRE-1517 — the session runner's pure decisions and the committed scripts.

Pure-logic coverage only, following ``test_fre1372_eval_isolation.py``: nothing here reaches a
gateway, a model or a substrate. ``run_turn``'s per-session wipe is covered there.
"""

from __future__ import annotations

import pytest
from scripts.eval.fre1517 import classify
from scripts.eval.fre1517.session_runner import (
    TRAILER_MARKER,
    apply_session_facts,
    completed_before,
    load_arm,
    resume_state,
    tbd_fields,
    turn_outcome,
)


def test_ttft_probe_prompt_starts_with_its_nonce_and_scales_with_size() -> None:
    """A9: the cold prompt's first bytes are unique; its size tracks the target."""
    from scripts.eval.fre1517.ttft_probe import CHARS_PER_TOKEN, build_prompt

    small, large = build_prompt(8000, "aa11"), build_prompt(32000, "bb22")
    assert small.startswith("Probe aa11.")
    assert large.startswith("Probe bb22.")
    assert abs(len(large) - 32000 * CHARS_PER_TOKEN) < 100


def test_archive_label_is_path_safe() -> None:
    """The label becomes a directory inside the eval container, so no separator may pass."""
    from scripts.eval.fre1517.session_runner import archive_label

    label = archive_label("run/../x", "mtplx_27b", "s2 tool", 1)
    assert "/" not in label and " " not in label
    assert label.startswith("run_.._x.mtplx_27b.s2_tool.r1.")


def test_completed_before_compares_timestamps_and_keeps_absence_undecided() -> None:
    """Master 2026-09-14: record whether the previous turn had consolidated when this one began."""
    assert completed_before("2026-09-20T10:00:00Z", "2026-09-20T10:00:05Z") is True
    assert completed_before("2026-09-20T10:00:09+00:00", "2026-09-20T10:00:05Z") is False
    assert completed_before(None, "2026-09-20T10:00:05Z") is None


PER_SESSION_FIELDS = {"thermal_snapshot"}


def test_every_arm_is_recorded_except_per_session_fields() -> None:
    """Unconfirmed static values are UNVERIFIED; only relayed per-session fields read TBD."""
    for name in ("ovh_27b", "mtplx_27b", "mtplx_flash_next", "llamacpp_flash_next"):
        assert set(tbd_fields(load_arm(name))) <= PER_SESSION_FIELDS, name
    assert tbd_fields(load_arm("ovh_27b")) == []


def test_session_facts_fill_the_mtplx_fields_and_mark_them_relayed() -> None:
    """Owner decision 2026-09-14 17:45 UTC: fans stay on Apple auto; thermal state is per session."""
    assert load_arm("mtplx_27b")["fan_mode"] == "apple_auto (UNVERIFIED)"
    arm = apply_session_facts(load_arm("mtplx_27b"), ["thermal_snapshot=cpu 61C"])
    assert tbd_fields(arm) == []
    assert arm["thermal_snapshot"] == "cpu 61C (UNVERIFIED, relayed)"
    with pytest.raises(ValueError, match="existing field"):
        apply_session_facts(load_arm("mtplx_27b"), ["no_such_field=x"])


def test_outcome_delivered_when_every_condition_holds() -> None:
    outcome = turn_outcome(
        reply="Here is your plan.",
        errors_by_role={},
        primary_models=["mtplx-a", "mtplx-a"],
        telemetry_model="mtplx-a",
    )
    assert outcome == {"delivered": True, "reasons": [], "attribution": "match"}


def test_outcome_fails_on_trailer_error_and_empty_reply() -> None:
    outcome = turn_outcome(
        reply="",
        errors_by_role={"sub_agent": 1},
        primary_models=["mtplx-a"],
        telemetry_model="mtplx-a",
    )
    assert outcome["delivered"] is False
    assert outcome["reasons"] == ["empty_reply", "model_call_error"]

    trailer = turn_outcome(
        reply=f"An answer.\n\n{TRAILER_MARKER} 1 of 2 sub-tasks did not complete",
        errors_by_role={},
        primary_models=["mtplx-a"],
        telemetry_model="mtplx-a",
    )
    assert trailer["reasons"] == ["fanout_trailer"]


def test_outcome_fails_only_on_errors_of_arm_bound_roles() -> None:
    """The owner's approval: an extraction or entailment error is reported, not a failed turn."""
    background = turn_outcome(
        reply="ok",
        errors_by_role={"entity_extraction": 2, "entailment": 1},
        primary_models=["mtplx-a"],
        telemetry_model="mtplx-a",
    )
    assert background["delivered"] is True

    for role in ("primary", "planner", "sub_agent"):
        bound = turn_outcome(
            reply="ok",
            errors_by_role={role: 1},
            primary_models=["mtplx-a"],
            telemetry_model="mtplx-a",
        )
        assert bound["reasons"] == ["model_call_error"], role


def test_outcome_mismatch_is_positive_and_absence_is_unverified() -> None:
    """A4: another model on a primary call is a mismatch; no primary event at all is not."""
    mismatch = turn_outcome(
        reply="ok",
        errors_by_role={},
        primary_models=["mtplx-a", "unsloth/other"],
        telemetry_model="mtplx-a",
    )
    assert mismatch["attribution"] == "mismatch"
    assert "arm_mismatch" in mismatch["reasons"]

    absent = turn_outcome(
        reply="ok", errors_by_role={}, primary_models=[], telemetry_model="mtplx-a"
    )
    assert absent["attribution"] == "unverified"
    assert absent["delivered"] is True


def test_resume_state() -> None:
    assert resume_state([]) == (None, 1)
    rows = [{"turn": 1, "session_id": "s1"}, {"turn": 2, "session_id": "s1"}]
    assert resume_state(rows) == ("s1", 3)
    with pytest.raises(RuntimeError, match="turn 2 failed"):
        resume_state([{"turn": 1, "session_id": "s1"}, {"turn": 2, "http_error": "ReadTimeout"}])


def test_an_arm_with_unrecorded_fields_is_named() -> None:
    assert tbd_fields({"arm": "x", "quant": "TBD", "pack": "p"}) == ["quant"]
    assert load_arm("mtplx_27b")["served_model_id"] == "mtplx-qwen38-27b-optimized-speed"


def test_committed_scripts_pass_the_offline_classification() -> None:
    """A2: a script edit that breaks a route, a window label or the HYBRID floor fails here."""
    for path in sorted(classify.SCRIPTS_DIR.glob("*.yaml")):
        _, failures = classify.check_script(path)
        assert failures == [], failures
