# ruff: noqa: D103
"""Unit tests for the evidence-truncation AST lint (ADR-0125 D5, AC-5).

The lint flags silent shortening of evidence-path content — text feeding a
durable artifact (a capture, a graph node, a reflection record) or assembled
context — anywhere in the tree, matched by identifier rather than by a fixed
(file, function) location (see the script's module docstring for why: a codex
plan review found the first-draft anchor-list design silently missed sites in
a different function or file).
"""

from __future__ import annotations

import textwrap
from pathlib import Path

from scripts.check_evidence_truncation import lint_file

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_bare_slice_on_evidence_name_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(user_message: str) -> dict:
                return {"summary": user_message[:200]}
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bare_slice" for v in violations)


def test_bare_slice_on_unrelated_name_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(page_title: str) -> str:
                return page_title[:200]
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_list_count_cap_is_not_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(user_message: list) -> list:
                return user_message[:5]
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_helper_clip_on_evidence_name_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def _truncate(text, limit):
                return text[:limit]

            def build(last_error: str) -> dict:
                return {"error_summary": _truncate(last_error, 200)}
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "helper_clip" for v in violations)


def test_byte_limit_on_evidence_name_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(content: str) -> str:
                return content.encode("utf-8")[:200].decode("utf-8", "ignore")
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "byte_limit" for v in violations)


def test_get_chain_on_evidence_key_is_flagged(tmp_path: Path) -> None:
    """The FRE-1010 shape: no named variable, slicing a `.get(key, ...)` chain directly."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def render(mem: dict) -> str:
                return mem.get("summary", mem.get("user_message", ""))[:150]
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bare_slice" for v in violations)


def test_get_chain_on_unrelated_key_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def render(mem: dict) -> str:
                return mem.get("page_title", "")[:150]
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_mark_truncated_call_is_clean(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            from personal_agent.captains_log.turn_evidence import mark_truncated

            def build(user_message: str) -> dict:
                return {"summary": mark_truncated(user_message, 200)}
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_shadowed_local_mark_truncated_is_still_flagged(tmp_path: Path) -> None:
    """A local function of the same name, not imported from turn_evidence, must not exempt."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def mark_truncated(text, limit):
                return text[:limit]

            def build(user_message: str) -> dict:
                return {"summary": mark_truncated(user_message, 200)}
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    # Not resolved as an import from turn_evidence, so the call itself is
    # treated like any other truncate-named call and flagged.
    assert any(v.kind == "helper_clip" for v in violations)


def test_log_call_kwarg_is_exempt(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            import structlog
            logger = structlog.get_logger(__name__)

            def scan(user_message: str) -> None:
                logger.info("recall_cue_detected", message_excerpt=user_message[:80])
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_log_dot_receiver_kwarg_is_exempt(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            from personal_agent.telemetry import get_logger
            log = get_logger(__name__)

            def parse(content: str) -> None:
                log.warning("parse_failed", content=content[:200])
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_dict_literal_key_is_flagged(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(ep: dict) -> dict:
                return {
                    "type": "episode",
                    "summary": ep.get("summary") or ep.get("user_message", "")[:200],
                }
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bare_slice" for v in violations)


def test_bool_op_fallback_target_is_flagged(tmp_path: Path) -> None:
    """The consolidator.py shape: `(capture.user_message or "").strip()[:200]`."""
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(capture) -> str:
                return (capture.user_message or "").strip()[:200]
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bare_slice" for v in violations)


def test_allowlisted_violation_is_suppressed(tmp_path: Path) -> None:
    src = tmp_path / "x.py"
    src.write_text(
        textwrap.dedent(
            """
            def build(user_message: str) -> dict:
                return {"summary": user_message[:200]}
            """
        )
    )
    allowlist = [{"path": str(src), "line": 3}]
    violations = lint_file(src, allowlist=allowlist)
    assert violations == []


def test_whole_file_scope_catches_generically_named_local(tmp_path: Path) -> None:
    """Rule C: state_document.py-shaped file where the truncated local isn't
    named like evidence content at all (`line`, not `content`/`summary`).
    """
    fake_root = tmp_path / "request_gateway"
    fake_root.mkdir()
    src = fake_root / "state_document.py"
    src.write_text(
        textwrap.dedent(
            """
            def _extract_constraints(messages):
                constraints = []
                for msg in messages:
                    line = msg.get("content", "")
                    constraints.append(line[:150])
                return constraints
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert any(v.kind == "bare_slice" for v in violations)


def test_whole_file_scope_does_not_apply_to_other_files(tmp_path: Path) -> None:
    """The same generically-named-local shape outside state_document.py is not flagged."""
    src = tmp_path / "some_other_module.py"
    src.write_text(
        textwrap.dedent(
            """
            def f(messages):
                out = []
                for msg in messages:
                    line = msg.get("content", "")
                    out.append(line[:150])
                return out
            """
        )
    )
    violations = lint_file(src, allowlist=[])
    assert violations == []


def test_real_state_document_module_is_clean_post_fix() -> None:
    """Confirms Rule C's whole-file scope produces zero false positives against the
    real (post-fix) file — every slice there now routes through mark_truncated.
    """
    real = REPO_ROOT / "src/personal_agent/request_gateway/state_document.py"
    violations = lint_file(real, allowlist=[])
    assert violations == []


def test_real_executor_is_clean_with_no_allowlist_entry() -> None:
    """The task-assist render truncation FRE-1002 deferred is gone, not exempted.

    This test previously asserted the *opposite*: that the guard flagged
    ``mem.get('summary', ...)[:150]`` in ``step_llm_call`` when the allowlist was
    bypassed, which was FRE-1002's proof it caught real code rather than only
    synthetic fixtures. FRE-1010 removed that truncation (the value is now bounded by
    ``_MAX_ITEM_CHARS`` through ``mark_truncated``), so the subject no longer exists
    and the allowlist entry was retired with it.

    Inverted rather than deleted: as a real-tree assertion with an empty allowlist it
    still fails if the clip is ever reintroduced — which is the regression that
    actually matters now. The guard's ``.get(key, ...)``-chain detection stays covered
    by ``test_get_chain_on_evidence_key_is_flagged``.
    """
    real = REPO_ROOT / "src/personal_agent/orchestrator/executor.py"
    assert lint_file(real, allowlist=[]) == []
