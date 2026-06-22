"""Unit tests for the FRE-453 canonical eval set + harness (no LLM, no DB).

Validates three things against the frozen taxonomy (RESULT_TYPE_TAXONOMY_SPEC.md /
ADR-0084 §D4):

1. **Dataset integrity** — 18 cases, two tiers, all expected values drawn from the
   frozen vocabularies (orchestration events restricted to the classifier-emittable
   subset; pedagogical outcomes ⊆ the 10-outcome list).
2. **Union coverage** (self-describing) — every skill in ``docs/skills/`` and every
   native (non-``mcp_``) tool in ``config/governance/tools.yaml`` is claimed by some
   case or explicitly allowlisted; declared MCP families are claimed.
3. **Evaluator semantics** — MATCH/MISMATCH findings on synthetic ``RouteTraceRow``s,
   including the structural route-mismatch candidate flag in both directions.
"""

from __future__ import annotations

import sys
from pathlib import Path
from uuid import uuid4

import pytest
import yaml  # type: ignore[import-untyped]

from personal_agent.observability.route_trace.types import RouteTraceRow

REPO_ROOT = Path(__file__).resolve().parents[2]
# scripts/ is a namespace package rooted at the repo root.
sys.path.insert(0, str(REPO_ROOT))

from scripts.eval.fre453_canonical_evalset.harness import (  # noqa: E402
    CLASSIFIER_EMITTABLE_EVENTS,
    MODEL_PATHS,
    PEDAGOGICAL_OUTCOMES,
    EvalCase,
    EvalSet,
    evaluate_case,
    load_dataset,
    render_markdown,
)

DATASET_PATH = REPO_ROOT / "scripts" / "eval" / "fre453_canonical_evalset" / "dataset.yaml"
SKILLS_DIR = REPO_ROOT / "docs" / "skills"
TOOLS_YAML = REPO_ROOT / "config" / "governance" / "tools.yaml"

# Non-skill markdown files living in docs/skills/.
_SKILL_FILE_EXCLUSIONS = {"SKILL_TEMPLATE", "EMPIRICAL_TEST_RESULTS"}

# The 7 required canonical turn types (ADR-0084 §Open decisions §3 / spec §7.2).
_CANONICAL_IDS = {
    "trivial_conversational",
    "memory_recall",
    "opening_ritual",
    "closing_ritual",
    "cross_thread_synthesis",
    "emotionally_loaded",
    "tool_heavy_research",
}


@pytest.fixture(scope="module")
def evalset() -> EvalSet:
    """Load the canonical dataset once per module."""
    return load_dataset(DATASET_PATH)


def _make_row(**overrides: object) -> RouteTraceRow:
    """Build a synthetic RouteTraceRow with sane turn-level defaults."""
    defaults: dict[str, object] = {
        "trace_id": uuid4(),
        "session_id": uuid4(),
        "decomposition_strategy": "single",
        "model_role": "primary",
        "orchestration_event": "primary_handled",
        "gateway_label": "conversational/single",
    }
    defaults.update(overrides)
    return RouteTraceRow(**defaults)  # type: ignore[arg-type]


def _case(evalset: EvalSet, case_id: str) -> EvalCase:
    """Fetch one case by id."""
    matches = [c for c in evalset.cases if c.id == case_id]
    assert matches, f"case {case_id!r} not in dataset"
    return matches[0]


# ---------------------------------------------------------------------------
# 1. Dataset integrity
# ---------------------------------------------------------------------------


class TestDatasetIntegrity:
    """Dataset shape and frozen-vocabulary validation."""

    def test_loads_and_has_18_cases(self, evalset: EvalSet) -> None:
        """The dataset loads and holds exactly 18 cases."""
        assert len(evalset.cases) == 18

    def test_ids_unique(self, evalset: EvalSet) -> None:
        """Case ids are unique."""
        ids = [c.id for c in evalset.cases]
        assert len(ids) == len(set(ids))

    def test_canonical_tier_is_exactly_the_seven_required_types(self, evalset: EvalSet) -> None:
        """Canonical tier matches ADR-0084 §Open-decisions-3 exactly."""
        canonical = {c.id for c in evalset.cases if c.tier == "canonical"}
        assert canonical == _CANONICAL_IDS

    def test_coverage_tier_has_eleven_cases(self, evalset: EvalSet) -> None:
        """Coverage tier holds the 11 multi-tool cases."""
        assert sum(1 for c in evalset.cases if c.tier == "coverage") == 11

    def test_orchestration_events_are_classifier_emittable(self, evalset: EvalSet) -> None:
        """Expected events stay within the classifier-emittable subset."""
        for case in evalset.cases:
            assert case.expected.orchestration_event in CLASSIFIER_EMITTABLE_EVENTS, case.id

    def test_pedagogical_outcomes_within_frozen_vocab(self, evalset: EvalSet) -> None:
        """Expected outcomes stay within the frozen 10-outcome vocabulary."""
        for case in evalset.cases:
            extra = set(case.expected.pedagogical_outcomes) - PEDAGOGICAL_OUTCOMES
            assert not extra, f"{case.id}: unknown outcomes {extra}"

    def test_model_paths_valid(self, evalset: EvalSet) -> None:
        """Expected model paths use the ticket's 4-value vocabulary."""
        for case in evalset.cases:
            assert case.expected.model_path in MODEL_PATHS, case.id

    def test_every_case_has_rubric_and_regression(self, evalset: EvalSet) -> None:
        """Every case carries rubric criteria and a regression note."""
        for case in evalset.cases:
            assert case.rubric, f"{case.id}: empty rubric"
            assert case.regression.strip(), f"{case.id}: empty regression"

    def test_every_case_has_stimulus(self, evalset: EvalSet) -> None:
        """Every case has a non-empty scored stimulus."""
        for case in evalset.cases:
            assert case.stimulus.strip(), case.id

    def test_multi_turn_cases_have_setup(self, evalset: EvalSet) -> None:
        """Context-dependent cases ship their own setup turns."""
        for case_id in (
            "memory_recall",
            "opening_ritual",
            "closing_ritual",
            "cross_thread_synthesis",
        ):
            assert _case_setup_len(evalset, case_id) >= 1

    def test_delegate_hypothesis_exists(self, evalset: EvalSet) -> None:
        """The set carries exactly one DELEGATE-path hypothesis (delegation_handoff)."""
        delegate = [c.id for c in evalset.cases if c.expected.model_path == "delegate"]
        assert delegate == ["delegation_handoff"]


def _case_setup_len(evalset: EvalSet, case_id: str) -> int:
    return len(_case(evalset, case_id).setup_messages)


# ---------------------------------------------------------------------------
# 2. Union coverage (self-describing enforcement)
# ---------------------------------------------------------------------------


class TestUnionCoverage:
    """Self-describing toolbox-coverage enforcement."""

    def _claimed_skills(self, evalset: EvalSet) -> set[str]:
        claimed: set[str] = set()
        for case in evalset.cases:
            claimed.update(case.expected.skills)
        return claimed

    def _claimed_tools(self, evalset: EvalSet) -> set[str]:
        claimed: set[str] = set()
        for case in evalset.cases:
            claimed.update(case.expected.tools_any_of)
        return claimed

    def test_all_repo_skills_claimed_or_allowlisted(self, evalset: EvalSet) -> None:
        """Every docs/skills skill is claimed by a case or allowlisted."""
        repo_skills = {
            p.stem for p in SKILLS_DIR.glob("*.md") if p.stem not in _SKILL_FILE_EXCLUSIONS
        }
        uncovered = (
            repo_skills - self._claimed_skills(evalset) - set(evalset.coverage.allowlist_skills)
        )
        assert not uncovered, f"skills with no eval case: {sorted(uncovered)}"

    def test_all_native_tools_claimed_or_allowlisted(self, evalset: EvalSet) -> None:
        """Every native tools.yaml tool is claimed or allowlisted."""
        registry = yaml.safe_load(TOOLS_YAML.read_text())
        native = {t for t in registry["tools"] if not t.startswith("mcp_")}
        uncovered = native - self._claimed_tools(evalset) - set(evalset.coverage.allowlist_tools)
        assert not uncovered, f"native tools with no eval case: {sorted(uncovered)}"

    def test_declared_families_claimed(self, evalset: EvalSet) -> None:
        """Every declared MCP family is claimed by some case."""
        for family in evalset.coverage.tool_families:
            assert family in self._claimed_tools(evalset), f"family {family!r} unclaimed"

    def test_claimed_skills_exist_in_repo(self, evalset: EvalSet) -> None:
        """No typo'd skill names: every claim must be a real docs/skills file."""
        repo_skills = {p.stem for p in SKILLS_DIR.glob("*.md")}
        ghost = self._claimed_skills(evalset) - repo_skills
        assert not ghost, f"claimed skills not in docs/skills/: {sorted(ghost)}"

    def test_claimed_tools_exist_in_registry_or_families(self, evalset: EvalSet) -> None:
        """No typo'd tool claims: all exist in tools.yaml or families."""
        registry = yaml.safe_load(TOOLS_YAML.read_text())
        known = set(registry["tools"]) | set(evalset.coverage.tool_families)
        ghost = self._claimed_tools(evalset) - known
        assert not ghost, f"claimed tools not in tools.yaml or families: {sorted(ghost)}"


# ---------------------------------------------------------------------------
# 3. Evaluator semantics
# ---------------------------------------------------------------------------


class TestEvaluator:
    """MATCH/MISMATCH evaluator semantics on synthetic rows."""

    def _trivial(self, evalset: EvalSet) -> EvalCase:
        return _case(evalset, "trivial_conversational")

    def _by_name(self, evaluation: object, name: str) -> object:
        findings = {f.name: f for f in evaluation.findings}  # type: ignore[attr-defined]
        assert name in findings, f"no finding named {name!r}: {sorted(findings)}"
        return findings[name]

    def test_event_match(self, evalset: EvalSet) -> None:
        """Matching orchestration event yields a match finding."""
        row = _make_row(orchestration_event="primary_handled")
        ev = evaluate_case(self._trivial(evalset), row)
        assert self._by_name(ev, "orchestration_event").verdict == "match"  # type: ignore[attr-defined]

    def test_event_mismatch(self, evalset: EvalSet) -> None:
        """Diverging orchestration event yields a mismatch finding."""
        row = _make_row(orchestration_event="delegate_called", decomposition_strategy="hybrid")
        ev = evaluate_case(self._trivial(evalset), row)
        assert self._by_name(ev, "orchestration_event").verdict == "mismatch"  # type: ignore[attr-defined]

    @pytest.mark.parametrize(
        ("model_path", "strategy", "role", "verdict"),
        [
            ("single_primary", "single", "primary", "match"),
            ("single_primary", "single", "sub_agent", "mismatch"),
            ("single_sub_agent", "single", "sub_agent", "match"),
            ("hybrid", "hybrid", "primary", "match"),
            ("hybrid", "single", "primary", "mismatch"),
            ("delegate", "delegate", "primary", "match"),
            ("delegate", "single", "primary", "mismatch"),
        ],
    )
    def test_model_path_mapping(
        self,
        evalset: EvalSet,
        model_path: str,
        strategy: str,
        role: str,
        verdict: str,
    ) -> None:
        """Model path maps to its two field comparisons correctly."""
        case = self._trivial(evalset)
        case = _with_expected(case, model_path=model_path)
        row = _make_row(decomposition_strategy=strategy, model_role=role)
        ev = evaluate_case(case, row)
        assert self._by_name(ev, "model_path").verdict == verdict  # type: ignore[attr-defined]

    def test_route_mismatch_flag_hybrid_declared_but_primary_handled(
        self, evalset: EvalSet
    ) -> None:
        """Declared expansion + primary_handled raises the structural flag."""
        row = _make_row(decomposition_strategy="hybrid", orchestration_event="primary_handled")
        ev = evaluate_case(self._trivial(evalset), row)
        assert ev.route_mismatch_candidate is True  # type: ignore[attr-defined]

    def test_route_mismatch_flag_single_declared_but_delegate_called(
        self, evalset: EvalSet
    ) -> None:
        """Declared single + delegation raises the structural flag."""
        row = _make_row(decomposition_strategy="single", orchestration_event="delegate_called")
        ev = evaluate_case(self._trivial(evalset), row)
        assert ev.route_mismatch_candidate is True  # type: ignore[attr-defined]

    def test_route_mismatch_flag_clear_when_consistent(self, evalset: EvalSet) -> None:
        """Consistent strategy/event keeps the structural flag clear."""
        row = _make_row(decomposition_strategy="single", orchestration_event="primary_handled")
        ev = evaluate_case(self._trivial(evalset), row)
        assert ev.route_mismatch_candidate is False  # type: ignore[attr-defined]

    def test_tools_used_nonempty_check(self, evalset: EvalSet) -> None:
        """tools_used_nonempty check matches/mismatches on tool usage."""
        case = _case(evalset, "tool_heavy_research")
        assert case.expected.tools_used_nonempty is True
        ev_empty = evaluate_case(case, _make_row(tools_used=()))
        assert self._by_name(ev_empty, "tools_used_nonempty").verdict == "mismatch"  # type: ignore[attr-defined]
        ev_used = evaluate_case(case, _make_row(tools_used=("bash",)))
        assert self._by_name(ev_used, "tools_used_nonempty").verdict == "match"  # type: ignore[attr-defined]

    def test_expected_skills_matching_normalizes_separators(self, evalset: EvalSet) -> None:
        """skills_loaded may report snake_case where docs/skills uses kebab-case."""
        case = _with_expected(self._trivial(evalset), skills=("self-telemetry",))
        ev = evaluate_case(case, _make_row(skills_loaded=("self_telemetry",)))
        assert self._by_name(ev, "skill:self-telemetry").verdict == "match"  # type: ignore[attr-defined]

    def test_expected_skill_missing_is_mismatch(self, evalset: EvalSet) -> None:
        """An expected skill absent from skills_loaded is a mismatch."""
        case = _with_expected(self._trivial(evalset), skills=("self-telemetry",))
        ev = evaluate_case(case, _make_row(skills_loaded=()))
        assert self._by_name(ev, "skill:self-telemetry").verdict == "mismatch"  # type: ignore[attr-defined]

    def test_tools_any_of_direct_and_family(self, evalset: EvalSet) -> None:
        """tools_any_of matches direct names and family prefixes."""
        case = _with_expected(self._trivial(evalset), tools_any_of=("browser",))
        families = {"browser": "mcp_browser_"}
        ev = evaluate_case(
            case, _make_row(tools_used=("mcp_browser_navigate",)), tool_families=families
        )
        assert self._by_name(ev, "tools_any_of").verdict == "match"  # type: ignore[attr-defined]
        ev_miss = evaluate_case(case, _make_row(tools_used=("bash",)), tool_families=families)
        assert self._by_name(ev_miss, "tools_any_of").verdict == "mismatch"  # type: ignore[attr-defined]


def _with_expected(case: EvalCase, **overrides: object) -> EvalCase:
    """Clone a frozen case with expected-block overrides."""
    from dataclasses import replace

    return replace(case, expected=replace(case.expected, **overrides))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 4. Renderer smoke
# ---------------------------------------------------------------------------


class TestRenderer:
    """Markdown report renderer smoke checks."""

    def test_markdown_contains_core_sections(self, evalset: EvalSet) -> None:
        """Rendered markdown carries the report's core sections."""
        case = _case(evalset, "trivial_conversational")
        row = _make_row()
        evaluation = evaluate_case(case, row)
        md = render_markdown(
            run_meta={"run_id": "unit-smoke", "profile": "local", "timestamp": "t"},
            results=[
                {
                    "case": case,
                    "row": row,
                    "evaluation": evaluation,
                    "response_text": "hi!",
                    "background": [],
                }
            ],
            evalset=evalset,
            pwa_url="https://seshat.example.com",
        )
        assert "trivial_conversational" in md
        assert "MATCH" in md
        assert "- [ ]" in md  # fillable rubric boxes
        assert "Coverage matrix" in md
        assert "Backend surfaces" in md
        assert "/c/" in md  # FRE-522: session deep-link rendered

    def _render_single(self, evalset: EvalSet, case: EvalCase, row: RouteTraceRow) -> str:
        evaluation = evaluate_case(case, row)
        return render_markdown(
            run_meta={"run_id": "unit-smoke", "profile": "local", "timestamp": "t"},
            results=[
                {
                    "case": case,
                    "row": row,
                    "evaluation": evaluation,
                    "response_text": "hi!",
                    "background": [],
                }
            ],
            evalset=evalset,
        )

    def test_disposition_block_rendered_for_delegate_called(self, evalset: EvalSet) -> None:
        """FRE-515: delegate_called rows get the disposition rubric block."""
        case = _case(evalset, "tool_heavy_research")
        row = _make_row(
            orchestration_event="delegate_called",
            decomposition_strategy="hybrid",
            sub_agent_count=2,
            delegate_result_passed_to_synthesis=True,
            final_reply_chars=5000,
            sub_agents=(
                {
                    "task_id": "sub-1",
                    "success": True,
                    "summary_chars": 800,
                    "output_chars": 800,
                    "reply_overlap": 0.62,
                    "error": None,
                },
            ),
        )
        md = self._render_single(evalset, case, row)
        assert "Delegate disposition (FRE-515" in md
        assert "used_candidate" in md
        assert "- [ ] `delegate_result_used` confirmed" in md
        assert "- [ ] `delegate_result_discarded` confirmed" in md
        assert "reply_overlap" in md
        assert "0.62" in md

    def test_disposition_candidate_discarded_on_error_row(self, evalset: EvalSet) -> None:
        """FRE-515: the artifact_study_guide baseline shape leans discarded."""
        case = _case(evalset, "artifact_study_guide")
        row = _make_row(
            orchestration_event="delegate_called",
            decomposition_strategy="hybrid",
            sub_agent_count=4,
            delegate_result_passed_to_synthesis=True,
            final_reply_chars=501,
            error_type="LLMServerError",
        )
        md = self._render_single(evalset, case, row)
        assert "discarded_candidate" in md

    def test_disposition_block_absent_for_primary_handled(self, evalset: EvalSet) -> None:
        """FRE-515: primary_handled rows carry no disposition block."""
        case = _case(evalset, "trivial_conversational")
        md = self._render_single(evalset, case, _make_row())
        assert "Delegate disposition" not in md

    def test_disposition_block_absent_for_fallback_triggered(self, evalset: EvalSet) -> None:
        """FRE-515: fallback rows carry subs but are their own terminal event (§3.5)."""
        case = _case(evalset, "tool_heavy_research")
        row = _make_row(
            orchestration_event="fallback_triggered",
            decomposition_strategy="hybrid",
            sub_agent_count=2,
            fallback_triggered=True,
        )
        md = self._render_single(evalset, case, row)
        assert "Delegate disposition" not in md
