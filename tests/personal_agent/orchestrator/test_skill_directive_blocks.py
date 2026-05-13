"""Tests for the FRE-337 skill directive block assembly functions.

Both functions live in ``personal_agent.orchestrator.skills``:

- ``assemble_skill_index_directive() -> str``
  Emitted whenever the compact skill index is present. Constant XML text.

- ``assemble_skill_usage_directives(loaded, skills) -> str``
  Emitted only when ≥1 skill body is loaded. Carries per-skill nudge bullets.
"""

from __future__ import annotations

from personal_agent.orchestrator.skills import (
    SkillDoc,
    assemble_skill_index_directive,
    assemble_skill_usage_directives,
)


def _skill(name: str, nudge: str | None = None) -> SkillDoc:
    return SkillDoc(
        name=name,
        description=f"{name} description",
        when_to_use="always",
        tools=("bash",),
        keywords=(name,),
        canonical_patterns=(),
        known_bad_patterns=(),
        body=f"# {name} body",
        nudge=nudge,
    )


class TestAssembleSkillIndexDirective:
    """assemble_skill_index_directive() returns constant XML text."""

    def test_returns_nonempty_string(self) -> None:
        result = assemble_skill_index_directive()
        assert result.strip() != ""

    def test_uses_xml_tag_skill_index_directive(self) -> None:
        result = assemble_skill_index_directive()
        assert "<skill_index_directive>" in result
        assert "</skill_index_directive>" in result

    def test_does_not_use_system_reminder_tag(self) -> None:
        result = assemble_skill_index_directive()
        assert "system-reminder" not in result
        assert "system_reminder" not in result

    def test_mentions_read_skill(self) -> None:
        result = assemble_skill_index_directive()
        assert "read_skill" in result

    def test_no_per_skill_content(self) -> None:
        """Index directive is constant — never varies based on which skills are loaded."""
        result1 = assemble_skill_index_directive()
        result2 = assemble_skill_index_directive()
        assert result1 == result2


class TestAssembleSkillUsageDirectives:
    """assemble_skill_usage_directives(loaded, skills) builds the body-directive block."""

    def test_uses_xml_tag_skill_usage_directives(self) -> None:
        skills = {"bash": _skill("bash")}
        result = assemble_skill_usage_directives(["bash"], skills)
        assert "<skill_usage_directives>" in result
        assert "</skill_usage_directives>" in result

    def test_does_not_use_system_reminder_tag(self) -> None:
        skills = {"bash": _skill("bash")}
        result = assemble_skill_usage_directives(["bash"], skills)
        assert "system-reminder" not in result

    def test_wrapper_paragraph_always_present(self) -> None:
        """Wrapper text emits even when no skill has a nudge field."""
        skills = {"bash": _skill("bash", nudge=None)}
        result = assemble_skill_usage_directives(["bash"], skills)
        # Should contain the actionable-instruction framing
        assert "actionable" in result.lower() or "loaded skill" in result.lower()

    def test_bullet_emitted_for_skill_with_nudge(self) -> None:
        """Skills with a nudge: value produce a per-skill bullet."""
        nudge_text = "Always run the metrics command before answering."
        skills = {"metrics": _skill("metrics", nudge=nudge_text)}
        result = assemble_skill_usage_directives(["metrics"], skills)
        assert "metrics" in result
        assert nudge_text in result

    def test_no_bullet_for_skill_without_nudge(self) -> None:
        """Skills without a nudge: field contribute no per-skill bullet line."""
        skills = {"bash": _skill("bash", nudge=None)}
        result = assemble_skill_usage_directives(["bash"], skills)
        # The skill name should not appear as a bullet (wrapper only)
        lines = [ln.strip() for ln in result.splitlines() if ln.strip().startswith("- bash")]
        assert lines == [], f"Unexpected bullet for no-nudge skill: {lines}"

    def test_multiple_skills_only_nudge_skills_produce_bullets(self) -> None:
        """When only some loaded skills have nudge, only those get bullets."""
        skills = {
            "bash": _skill("bash", nudge=None),
            "es": _skill("es", nudge="Run a live query."),
        }
        result = assemble_skill_usage_directives(["bash", "es"], skills)
        assert "Run a live query." in result
        bullet_lines = [ln for ln in result.splitlines() if ln.strip().startswith("- ")]
        # Only es should have a bullet
        assert len(bullet_lines) == 1
        assert "es" in bullet_lines[0]

    def test_returns_empty_string_for_empty_loaded_list(self) -> None:
        """No loaded skills → empty string (caller gates the block)."""
        skills = {"bash": _skill("bash")}
        result = assemble_skill_usage_directives([], skills)
        assert result == ""

    def test_unknown_skill_names_ignored_gracefully(self) -> None:
        """Names in loaded that are not in skills dict are silently skipped."""
        skills = {"bash": _skill("bash", nudge="Be careful.")}
        result = assemble_skill_usage_directives(["bash", "nonexistent"], skills)
        assert "<skill_usage_directives>" in result
        assert "nonexistent" not in result
