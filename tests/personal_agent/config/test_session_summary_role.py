"""AC-14 — the summariser resolves through its own role (ADR-0124 D2, FRE-947).

The criterion: ``session_summary.py`` resolves ``session_summary``, not
``captains_log``; ``config/model_roles.yaml`` carries the key in **both** the
``roles`` and ``bindings`` blocks; the config guard accepts it; and the deployment
key it resolves to is **byte-identical** to what ``captains_log`` resolved to
before the change.

*Fails if* the producer still resolves another subsystem's role, if the guard
rejects or ignores the key, or if the resolved model differs from today's — **the
role must land as an observable no-op, or it has smuggled in the model change this
ADR defers.**

Why the role exists at all: ADR-0124 D2 locates egress control at the role binding
("do these bytes leave the machine?" is answered by which deployment the role is
bound to). That control point is incoherent while the summariser borrows
reflection's role, because re-binding the summariser would re-bind reflection at
the same time. What stays deferred is the model *choice*; creating the role is what
turns that future question into a config flip rather than a code change.
"""

# ruff: noqa: D103

from __future__ import annotations

from pathlib import Path

import yaml

from personal_agent.config.config_guard import (
    check_dangling_model_references,
    check_matrix_shape,
    check_no_role_headers,
    load_matrix,
    repo_root,
)
from personal_agent.config.model_loader import resolve_role_model_key

_ROOT = repo_root()


def _matrix() -> dict:
    return yaml.safe_load((Path(_ROOT) / "config" / "model_roles.yaml").read_text(encoding="utf-8"))


def test_role_is_declared_in_both_blocks() -> None:
    """A role in `roles:` but not `bindings:` (or vice versa) is half a control point."""
    matrix = _matrix()
    assert "session_summary" in matrix["roles"]
    assert "session_summary" in matrix["bindings"]


def test_resolved_deployment_is_byte_identical_to_captains_log() -> None:
    """The no-op requirement — the whole point of AC-14.

    A different model here would mean the role's introduction smuggled in the model
    change ADR-0124 explicitly defers.
    """
    matrix = _matrix()

    assert matrix["roles"]["session_summary"]["all"] == matrix["roles"]["captains_log"]["all"]
    assert (
        matrix["bindings"]["session_summary"]["deployment"]
        == matrix["bindings"]["captains_log"]["deployment"]
    )
    assert resolve_role_model_key("session_summary") == resolve_role_model_key("captains_log")


def test_role_is_pinned_not_user_selectable() -> None:
    """`open: true` marks a role a user may select; absent means pinned (fail-closed).

    The summariser reads full tool payloads, so its binding is an egress decision
    the owner makes deliberately — not one a user flips from a model picker.
    """
    assert _matrix()["bindings"]["session_summary"].get("open") is not True


def test_config_guard_accepts_the_new_key() -> None:
    """No orphan-key, dangling-reference or shape finding from introducing the role."""
    matrix = load_matrix(_ROOT)

    findings = (
        check_dangling_model_references(_ROOT, matrix)
        + check_matrix_shape(matrix)
        + check_no_role_headers(_ROOT)
    )

    offending = [f for f in findings if "session_summary" in f.message]
    assert offending == [], f"config guard rejected the new role: {offending}"


def test_producer_resolves_its_own_role_not_captains_log() -> None:
    """The source-level half of AC-14: no borrowed role left in the producer."""
    source = (
        Path(_ROOT) / "src" / "personal_agent" / "second_brain" / "session_summary.py"
    ).read_text(encoding="utf-8")

    assert 'resolve_role_model_key("session_summary")' in source
    assert 'resolve_role_model_key("captains_log")' not in source


def test_budget_role_stays_captains_log() -> None:
    """ADR-0124 D2 is explicit that splitting cost attribution is NOT taken here.

    Pinned so a later reader does not "tidy" it into session_summary and silently
    take a decision the ADR deferred.
    """
    source = (
        Path(_ROOT) / "src" / "personal_agent" / "second_brain" / "session_summary.py"
    ).read_text(encoding="utf-8")

    assert 'budget_role="captains_log"' in source
