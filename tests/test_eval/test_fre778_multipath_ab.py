"""Unit tests for the FRE-778 multipath A/B driver's pure logic (no substrate)."""

from __future__ import annotations

from types import SimpleNamespace

from scripts.eval.fre435_memory_recall.ab_multipath import (
    _set_multipath,
    dense_floor_invariant,
    latency_summary,
)


class TestLatencySummary:
    """Median latency vs the FRE-724 AC-6b 17s ceiling."""

    def test_median_within_ceiling(self) -> None:
        summary = latency_summary([1.0, 2.0, 3.0], ceiling_s=17.0)
        assert summary.median_s == 2.0
        assert summary.ceiling_s == 17.0
        assert summary.within_ceiling is True

    def test_median_breaches_ceiling(self) -> None:
        summary = latency_summary([18.0, 19.0, 20.0], ceiling_s=17.0)
        assert summary.median_s == 19.0
        assert summary.within_ceiling is False

    def test_median_at_ceiling_passes(self) -> None:
        """At-or-under the ceiling passes (AC-6c: 'does not exceed')."""
        summary = latency_summary([17.0], ceiling_s=17.0)
        assert summary.within_ceiling is True

    def test_empty_durations_is_undefined_not_a_crash(self) -> None:
        summary = latency_summary([], ceiling_s=17.0)
        assert summary.median_s is None
        assert summary.within_ceiling is None


class TestDenseFloorInvariant:
    """The lowest true-positive dense-arm cosine must clear the configured floor."""

    def test_holds_when_min_positive_at_or_above_floor(self) -> None:
        result = dense_floor_invariant([0.62, 0.71, 0.60], floor=0.60)
        assert result.min_positive == 0.60
        assert result.holds is True

    def test_fails_when_min_positive_below_floor(self) -> None:
        result = dense_floor_invariant([0.62, 0.58], floor=0.60)
        assert result.min_positive == 0.58
        assert result.holds is False

    def test_empty_positives_is_unknown_not_vacuously_true(self) -> None:
        """No positive cosines captured means the invariant is unproven, not passing."""
        result = dense_floor_invariant([], floor=0.60)
        assert result.min_positive is None
        assert result.holds is None


class TestSetMultipath:
    """Toggling multipath sets exactly the three arm flags + floor + relevance-bounded pin."""

    def test_enabled_sets_arms_floor_and_pins_relevance_bounded_off(self) -> None:
        fake_settings = SimpleNamespace(
            multipath_recall_enabled=False,
            lexical_arm_enabled=False,
            multiquery_arm_enabled=False,
            recall_similarity_floor=0.0,
            relevance_bounded_recall_enabled=True,
        )
        _set_multipath(fake_settings, enabled=True)
        assert fake_settings.multipath_recall_enabled is True
        assert fake_settings.lexical_arm_enabled is True
        assert fake_settings.multiquery_arm_enabled is True
        assert fake_settings.recall_similarity_floor == 0.60
        assert fake_settings.relevance_bounded_recall_enabled is False

    def test_disabled_clears_arms_but_keeps_floor_at_060(self) -> None:
        """OFF state: arms off, but the floor stays 0.60 (a constant condition, not toggled)."""
        fake_settings = SimpleNamespace(
            multipath_recall_enabled=True,
            lexical_arm_enabled=True,
            multiquery_arm_enabled=True,
            recall_similarity_floor=0.0,
            relevance_bounded_recall_enabled=True,
        )
        _set_multipath(fake_settings, enabled=False)
        assert fake_settings.multipath_recall_enabled is False
        assert fake_settings.lexical_arm_enabled is False
        assert fake_settings.multiquery_arm_enabled is False
        assert fake_settings.recall_similarity_floor == 0.60
        assert fake_settings.relevance_bounded_recall_enabled is False
