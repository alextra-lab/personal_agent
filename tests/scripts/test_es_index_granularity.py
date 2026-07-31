"""Guard test for the ES index-name granularity classifier (FRE-1035).

A live agent turn once shell-stripped only the month/day off a dot-dated index
name, leaving the year attached, and reported the resulting artifact
(``agent-logs-2026``) as a real yearly index. This module's job is to prove the
tested replacement never does that: it only ever returns a period it matched in
full against a known shape, and returns ``None`` for anything else — including
the exact bug artifact, a different family's name, and a sibling family sharing
a prefix (``agent-captains-captures-subagents-*`` under the ``agent-captains-captures``
prefix).

Source of truth: ``docs/superpowers/plans/2026-07-31-fre-1035-es-skill-granularity-recipe.md``.
"""

from __future__ import annotations

from scripts.es_index_granularity import classify_index_period, report_family_granularity

# --------------------------------------------------------------------------- #
# The six observed shapes classify correctly.
# --------------------------------------------------------------------------- #


def test_dash_daily() -> None:
    """A dash-separated daily index classifies as daily with the right date."""
    period = classify_index_period("agent-captains-captures-2026-04-15", "agent-captains-captures")
    assert period is not None
    assert period.granularity == "daily"
    assert (period.year, period.month, period.day) == ("2026", "04", "15")


def test_dot_daily() -> None:
    """A dot-separated daily index classifies as daily with the right date."""
    period = classify_index_period("agent-logs-2026.04.15", "agent-logs")
    assert period is not None
    assert period.granularity == "daily"
    assert (period.year, period.month, period.day) == ("2026", "04", "15")


def test_dash_daily_with_v2_suffix() -> None:
    """A dash-daily index with a trailing -v2 suffix still classifies as daily."""
    period = classify_index_period(
        "agent-captains-captures-2026-04-15-v2", "agent-captains-captures"
    )
    assert period is not None
    assert period.granularity == "daily"
    assert (period.year, period.month, period.day) == ("2026", "04", "15")


def test_dot_daily_with_v2_suffix() -> None:
    """A dot-daily index with a trailing -v2 suffix still classifies as daily."""
    period = classify_index_period("agent-logs-2026.04.15-v2", "agent-logs")
    assert period is not None
    assert period.granularity == "daily"
    assert (period.year, period.month, period.day) == ("2026", "04", "15")


def test_dash_monthly() -> None:
    """A dash-separated monthly index classifies as monthly with no day."""
    period = classify_index_period("agent-insights-2026-07", "agent-insights")
    assert period is not None
    assert period.granularity == "monthly"
    assert (period.year, period.month, period.day) == ("2026", "07", None)


def test_dot_monthly() -> None:
    """A dot-separated monthly index classifies as monthly with no day."""
    period = classify_index_period("agent-monitors-slm-health-2026.06", "agent-monitors-slm-health")
    assert period is not None
    assert period.granularity == "monthly"
    assert (period.year, period.month, period.day) == ("2026", "06", None)


# --------------------------------------------------------------------------- #
# Invents nothing: unrecognized shapes return None, never a guess.
# --------------------------------------------------------------------------- #


def test_the_ticket_bug_artifact_is_rejected() -> None:
    """The exact FRE-1035 artifact — a truncated month/day strip leaving the year — is rejected.

    Never classifies as anything, matching the fabricated ``agent-logs-2026``
    the ticket reports.
    """
    assert classify_index_period("agent-logs-2026", "agent-logs") is None


def test_different_family_prefix_is_rejected() -> None:
    """A name belonging to a different family never classifies under the wrong prefix."""
    assert classify_index_period("agent-insights-2026-04-15", "agent-logs") is None


def test_sibling_family_under_parent_prefix_is_rejected() -> None:
    """A sibling family sharing a prefix is not misread as the parent family.

    ``agent-captains-captures-subagents-*`` must not be misread as an
    ``agent-captains-captures-*`` daily index (FRE-1036 sibling-prefix trap).
    """
    assert (
        classify_index_period(
            "agent-captains-captures-subagents-2026-04-15", "agent-captains-captures"
        )
        is None
    )


def test_garbage_suffix_is_rejected() -> None:
    """A non-date suffix after the family prefix never classifies as anything."""
    assert classify_index_period("agent-logs-not-a-date", "agent-logs") is None


def test_empty_suffix_is_rejected() -> None:
    """An index name that is exactly the family prefix (no date at all) is rejected."""
    assert classify_index_period("agent-logs-", "agent-logs") is None


# --------------------------------------------------------------------------- #
# report_family_granularity buckets correctly and never drops a name.
# --------------------------------------------------------------------------- #


def test_report_flags_mixed_family() -> None:
    """A live sample shape is flagged mixed and never counts the sibling family.

    ``agent-monitors-joinability`` today has both dot-daily and dash-monthly
    indices simultaneously (FRE-1036 migration in progress).
    """
    names = [
        "agent-monitors-joinability-2026.05.23",
        "agent-monitors-joinability-2026.05.24",
        "agent-monitors-joinability-2026-07",
        "agent-monitors-joinability-substrate-2026.05.23",  # sibling family — must not count
    ]
    report = report_family_granularity(names, "agent-monitors-joinability")
    assert report.daily == (
        "agent-monitors-joinability-2026.05.23",
        "agent-monitors-joinability-2026.05.24",
    )
    assert report.monthly == ("agent-monitors-joinability-2026-07",)
    assert report.unrecognized == ("agent-monitors-joinability-substrate-2026.05.23",)
    assert report.is_mixed is True


def test_report_not_mixed_when_only_one_shape_present() -> None:
    """A family with only monthly indices is not flagged mixed."""
    names = ["agent-insights-2026-05", "agent-insights-2026-06", "agent-insights-2026-07"]
    report = report_family_granularity(names, "agent-insights")
    assert report.daily == ()
    assert report.monthly == tuple(names)
    assert report.unrecognized == ()
    assert report.is_mixed is False


def test_report_surfaces_unrecognized_names_never_silently_drops_them() -> None:
    """Every unrecognized name is surfaced in the report, never dropped."""
    names = ["agent-logs-2026.04.15", "agent-logs-2026", "agent-logs-mystery-index"]
    report = report_family_granularity(names, "agent-logs")
    assert report.daily == ("agent-logs-2026.04.15",)
    assert report.monthly == ()
    assert set(report.unrecognized) == {"agent-logs-2026", "agent-logs-mystery-index"}
