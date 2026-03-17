"""Guard-rail tests for self-analysis stream model indirection (spec 4.1.1).

These tests verify the MECHANISM — configurable process-role keys and
provider-based dispatch — not specific provider values. They must pass
regardless of whether streams point at cloud or local models.
"""

import inspect

from personal_agent.config import load_model_config


def test_process_role_keys_resolve_to_valid_models() -> None:
    """Process-role keys must resolve to entries in the models registry.

    Defense-in-depth: ModelConfig._validate_process_roles already enforces
    this at load time, but we assert it explicitly so a Slice 1 refactoring
    that removes the validator is caught immediately.
    """
    config = load_model_config()

    for role_name in ("entity_extraction_role", "captains_log_role", "insights_role"):
        role_value = getattr(config, role_name)
        assert role_value in config.models, (
            f"{role_name}={role_value!r} does not match any entry in models"
        )


def test_self_analysis_consumers_use_process_role_indirection() -> None:
    """Consumers must read model assignment from config, not hardcode a ModelRole."""
    from personal_agent.captains_log import reflection
    from personal_agent.second_brain import entity_extraction

    ee_source = inspect.getsource(entity_extraction)
    refl_source = inspect.getsource(reflection)

    # Must reference the configurable process-role key (not a hardcoded ModelRole)
    assert "entity_extraction_role" in ee_source
    assert "captains_log_role" in refl_source

    # Must branch on the provider field (the dispatch mechanism)
    assert ".provider" in ee_source
    assert ".provider" in refl_source

    # NOTE: insights/engine.py does not yet use LLM-based analysis.
    # When insights_role dispatch is added to engine.py, add assertions here.
    # See spec Section 4.1.1 invariant.
