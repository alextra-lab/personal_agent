"""FRE-970 — guard against the stale `litellm_request_complete` cost-event fact.

FRE-376 Phase 3 removed the legacy `litellm_request_*` event names; cost now lives
on `model_call_completed` and `api_cost_recorded` (see `personal_agent.telemetry.events`
and `personal_agent.llm_client.cost_tracker`). The ES skill docs pointed the agent at the
removed event name, causing it to query dead data and thrash. This test fails if that
fact ever creeps back into a skill doc's body.

Frontmatter `known_bad_patterns` entries are exempt: those exist specifically to name the
retired event as an anti-pattern to catch, not to assert it as a real cost source.
"""

from __future__ import annotations

from pathlib import Path

_SKILL_DIR = Path(__file__).parent.parent.parent / "docs" / "skills"

_STALE_EVENT_NAME = "litellm_request_complete"


def _body_without_frontmatter(text: str) -> str:
    """Strip a leading YAML frontmatter block (between the first two '---' lines)."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return text
    for idx in range(1, len(lines)):
        if lines[idx].strip() == "---":
            return "\n".join(lines[idx + 1 :])
    return text


def test_skill_docs_do_not_name_litellm_request_complete_as_cost_source() -> None:
    """No skill doc body may name the removed `litellm_request_complete` event."""
    offenders: list[str] = []
    for doc_path in sorted(_SKILL_DIR.glob("*.md")):
        body = _body_without_frontmatter(doc_path.read_text())
        if _STALE_EVENT_NAME in body:
            offenders.append(doc_path.name)

    assert not offenders, (
        f"{_STALE_EVENT_NAME!r} was removed in FRE-376 Phase 3 but is still named in the "
        "body (outside known_bad_patterns) of: "
        + ", ".join(offenders)
        + ". Cost lives on 'model_call_completed' / 'api_cost_recorded' — update the doc(s)."
    )
