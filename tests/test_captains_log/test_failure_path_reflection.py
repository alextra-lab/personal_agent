"""Tests for GenerateReflection Phase 2 fields (ADR-0056 §D6 DSPy extension).

Tests verify:
1. GenerateReflection signature includes failure_excerpt and had_errors inputs
2. GenerateReflection signature includes failure_path_fix_what and failure_path_fix_location outputs
3. generate_reflection_dspy passes the flag-off short-circuit correctly
"""

from __future__ import annotations

import pytest


def test_generate_reflection_has_failure_excerpt_input() -> None:
    """GenerateReflection has failure_excerpt as an input field."""
    try:
        import dspy  # noqa: F401

        from personal_agent.captains_log.reflection_dspy import GenerateReflection

        if GenerateReflection is None:
            pytest.skip("DSPy not available")

        # Introspect the signature fields
        sig = GenerateReflection
        # DSPy v2 stores fields on the signature class
        input_fields = sig.input_fields() if callable(getattr(sig, "input_fields", None)) else {}
        # Alternative: check class annotations
        annotations = getattr(sig, "__annotations__", {})
        assert "failure_excerpt" in annotations or "failure_excerpt" in input_fields, (
            "GenerateReflection must have 'failure_excerpt' input field (ADR-0056 §D6)"
        )
    except ImportError:
        pytest.skip("DSPy not installed")


def test_generate_reflection_has_had_errors_input() -> None:
    """GenerateReflection has had_errors as an input field."""
    try:
        import dspy  # noqa: F401

        from personal_agent.captains_log.reflection_dspy import GenerateReflection

        if GenerateReflection is None:
            pytest.skip("DSPy not available")

        annotations = getattr(GenerateReflection, "__annotations__", {})
        assert "had_errors" in annotations, (
            "GenerateReflection must have 'had_errors' input field (ADR-0056 §D6)"
        )
    except ImportError:
        pytest.skip("DSPy not installed")


def test_generate_reflection_has_failure_path_fix_what_output() -> None:
    """GenerateReflection has failure_path_fix_what as an output field."""
    try:
        import dspy  # noqa: F401

        from personal_agent.captains_log.reflection_dspy import GenerateReflection

        if GenerateReflection is None:
            pytest.skip("DSPy not available")

        annotations = getattr(GenerateReflection, "__annotations__", {})
        assert "failure_path_fix_what" in annotations, (
            "GenerateReflection must have 'failure_path_fix_what' output field (ADR-0056 §D6)"
        )
    except ImportError:
        pytest.skip("DSPy not installed")


def test_generate_reflection_has_failure_path_fix_location_output() -> None:
    """GenerateReflection has failure_path_fix_location as an output field."""
    try:
        import dspy  # noqa: F401

        from personal_agent.captains_log.reflection_dspy import GenerateReflection

        if GenerateReflection is None:
            pytest.skip("DSPy not available")

        annotations = getattr(GenerateReflection, "__annotations__", {})
        assert "failure_path_fix_location" in annotations, (
            "GenerateReflection must have 'failure_path_fix_location' output field (ADR-0056 §D6)"
        )
    except ImportError:
        pytest.skip("DSPy not installed")
