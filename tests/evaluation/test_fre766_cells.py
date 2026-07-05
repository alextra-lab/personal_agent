"""FRE-766 — pure tests for the model×reasoning benchmark matrix + eval helpers.

No LLM, no substrate: the cost function, the smoke failure-classifier, the baseline
compatibility gate, and the owner-specified matrix shape.
"""

from __future__ import annotations

from scripts.eval.fre630_extraction_quality.cells import (
    BASELINE_CELL,
    CELLS,
    CELLS_BY_NAME,
    baseline_compatible,
    classify_smoke,
    cost_usd,
)


def test_cost_is_usage_times_rates() -> None:
    """Cost = prompt×input_rate + completion×output_rate (authoritative, not litellm)."""
    cell = BASELINE_CELL
    got = cost_usd(cell, prompt_tokens=1000, completion_tokens=500)
    assert got == 1000 * cell.input_rate + 500 * cell.output_rate


class TestClassifySmoke:
    """A smoke failure is a finding, not a silent quality-zero (codex P1.4)."""

    def test_provider_rejection_from_error_class(self) -> None:
        """An API-level error (bad model/effort) is a provider_rejection."""
        result = {"entities": [], "stances": [], "claims": []}
        stats = [{"error_class": "AuthenticationError"}]
        assert classify_smoke(result, stats) == "provider_rejection:AuthenticationError"

    def test_empty_fallback_when_nothing_extracted(self) -> None:
        """A clean call that produced nothing usable is an empty_fallback."""
        result = {"entities": [], "stances": [], "claims": []}
        assert classify_smoke(result, [{"error_class": None}]) == "empty_fallback"

    def test_schema_violation_on_off_vocab_type(self) -> None:
        """A structured result with an off-vocab entity type is a schema_violation."""
        result = {"entities": [{"name": "X", "type": "Widget", "class": "World"}]}
        assert classify_smoke(result, [{"error_class": None}]) == "schema_violation"

    def test_schema_violation_on_off_vocab_class(self) -> None:
        """An off-vocab knowledge class is a schema_violation."""
        result = {"entities": [{"name": "X", "type": "Concept", "class": "Cosmic"}]}
        assert classify_smoke(result, [{"error_class": None}]) == "schema_violation"

    def test_ok_on_contract_honouring_output(self) -> None:
        """Valid vocabulary → ok."""
        result = {"entities": [{"name": "Neo4j", "type": "TechnicalArtifact", "class": "World"}]}
        assert classify_smoke(result, [{"error_class": None}]) == "ok"

    def test_ok_on_new_v2_only_type(self) -> None:
        """A V2-only type (FRE-771) classifies ok, not schema_violation (D4)."""
        result = {
            "entities": [{"name": "ADR-0109", "type": "KnowledgeArtifact", "class": "System"}]
        }
        assert classify_smoke(result, [{"error_class": None}]) == "ok"

    def test_provider_rejection_takes_precedence_over_content(self) -> None:
        """An API error is reported even if a fallback result is also present."""
        result = {"entities": [], "stances": [], "claims": []}
        stats = [{"error_class": "BadRequestError"}]
        assert classify_smoke(result, stats).startswith("provider_rejection")


class TestBaselineCompatibility:
    """Reuse a prior baseline row only when it is comparable (codex P1.3)."""

    _BASE = {
        "gold_schema_version": "1.0",
        "matcher_version": "1.0",
        "prompt_hash": "8a1bdd119ca3",
        "samples": 3,
        "fuzzy_threshold": 0.86,
        "gold_set": "scripts/eval/fre630_extraction_quality/gold_extraction.yaml",
    }

    def test_identical_meta_is_compatible(self) -> None:
        """Matching schema/matcher/prompt/samples/threshold/gold → reuse is valid."""
        assert baseline_compatible(self._BASE, dict(self._BASE)) is True

    def test_fuzzy_threshold_drift_is_incompatible(self) -> None:
        """A different matcher threshold means the scores are not comparable (codex P1.2)."""
        current = {**self._BASE, "fuzzy_threshold": 0.5}
        assert baseline_compatible(self._BASE, current) is False

    def test_gold_set_drift_is_incompatible(self) -> None:
        """A different gold-set file (same schema) is not comparable (codex P1.2)."""
        current = {**self._BASE, "gold_set": "some/other_gold.yaml"}
        assert baseline_compatible(self._BASE, current) is False

    def test_prompt_hash_drift_is_incompatible(self) -> None:
        """A different prompt hash means the numbers are not comparable."""
        current = {**self._BASE, "prompt_hash": "deadbeef0000"}
        assert baseline_compatible(self._BASE, current) is False

    def test_sample_count_drift_is_incompatible(self) -> None:
        """A different sample count means the std bands are not comparable."""
        current = {**self._BASE, "samples": 1}
        assert baseline_compatible(self._BASE, current) is False

    def test_extra_keys_ignored(self) -> None:
        """Non-compat keys (git_commit, timestamp) don't affect the decision."""
        current = {**self._BASE, "git_commit": "different", "timestamp": "later"}
        assert baseline_compatible(self._BASE, current) is True


class TestMatrixShape:
    """The owner-specified matrix — 5 new cells + a mini@medium baseline."""

    def test_five_measured_cells(self) -> None:
        """The 5 measured cells by name (mini-xhigh cut — measured non-viable)."""
        assert len(CELLS) == 5
        names = {c.name for c in CELLS}
        assert names == {
            "mini-medium",
            "mini-high",
            "full-medium",
            "full-high",
            "sonnet5-adaptive",
        }
        assert "mini-xhigh" not in names

    def test_baseline_is_mini_none_prod(self) -> None:
        """The baseline row is mini@none = current prod (measured 0 reasoning tokens)."""
        assert BASELINE_CELL.name == "mini-none"
        assert BASELINE_CELL.override.reasoning_effort is None  # unset == 'none' on gpt-5.4
        assert BASELINE_CELL.override.temperature == 0.0  # FRE-758 pin, allowed at 'none'

    def test_names_unique_including_baseline(self) -> None:
        """Cell names (incl. baseline) are unique and fully indexed."""
        all_names = [BASELINE_CELL.name, *(c.name for c in CELLS)]
        assert len(set(all_names)) == len(all_names)
        assert set(CELLS_BY_NAME) == set(all_names)

    def test_reasoning_cells_drop_the_temp_pin(self) -> None:
        """Any cell that sets a reasoning level runs temperature None (gpt-5 temp rule)."""
        for cell in CELLS:
            if cell.override.reasoning_effort is not None:
                assert cell.override.temperature is None, cell.name

    def test_sonnet_uses_adaptive_not_effort(self) -> None:
        """The Claude cell leaves reasoning_effort None (adaptive thinking)."""
        sonnet = CELLS_BY_NAME["sonnet5-adaptive"]
        assert sonnet.override.provider == "anthropic"
        assert sonnet.override.reasoning_effort is None

    def test_medium_rungs_are_explicit(self) -> None:
        """The 'medium' rungs set reasoning_effort explicitly (unset == 'none' on 5.4)."""
        assert CELLS_BY_NAME["mini-medium"].override.reasoning_effort == "medium"
        assert CELLS_BY_NAME["full-medium"].override.reasoning_effort == "medium"

    def test_reasoning_ladder_per_gpt_cell(self) -> None:
        """Each GPT cell carries its explicit effort; baseline is none (prod default)."""
        assert CELLS_BY_NAME["mini-high"].override.reasoning_effort == "high"
        assert CELLS_BY_NAME["full-high"].override.reasoning_effort == "high"
        assert BASELINE_CELL.override.reasoning_effort is None  # none = current prod
