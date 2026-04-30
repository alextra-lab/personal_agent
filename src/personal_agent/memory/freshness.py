"""Knowledge graph freshness decay function and staleness classification (FRE-165 / ADR-0042 Step 5).

Provides:
- ``compute_freshness`` — exponential decay modulated by access frequency.
- ``classify_staleness`` — tier classification for brainstem review job (FRE-166).

Decay formula (ADR-0042 Decision 4)::

    freshness = base_decay(days_since_last_access) × frequency_boost(access_count)

    base_decay(days)    = e^(-λ × days)    where λ = ln(2) / half_life_days
    frequency_boost(n)  = min(1.0 + α × ln(1 + n), max_boost)
    freshness           = min(base_decay × frequency_boost, 1.0)

Returns 0.0 when ``access_count == 0`` or ``last_accessed_at`` is ``None``
(no access data → no freshness signal; caller falls back to existing weights).

Example values (half_life_days=30, α=0.1, max_boost=1.5)::

    yesterday  + 50 accesses → freshness ≈ 1.46 → capped to 1.0
    30 days    +  5 accesses → freshness ≈ 0.59
    90 days    +  1 access   → freshness ≈ 0.13
    never accessed           → freshness  = 0.0
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from personal_agent.config.settings import AppConfig as Settings


class StalenessTier(str, Enum):
    """Access-based staleness classification for Neo4j entities/relationships.

    Tiers follow ADR-0042 Decision 5. Used by the brainstem review job (FRE-166)
    to prioritise staleness actions and generate Captain's Log proposals.
    """

    WARM = "warm"
    """Accessed within half_life_days — actively relevant, no action needed."""

    COOLING = "cooling"
    """Last accessed between half_life_days and 2× half_life_days.
    Downweighted via decay function; no structural action."""

    COLD = "cold"
    """Last accessed between 2× half_life_days and cold_threshold_days.
    Flagged in telemetry; available for manual review."""

    DORMANT = "dormant"
    """Last accessed > cold_threshold_days OR never accessed and created >
    cold_threshold_days ago. Candidate for archival review."""


def compute_freshness(
    last_accessed_at: datetime | None,
    access_count: int,
    half_life_days: float,
    alpha: float,
    max_boost: float,
) -> float:
    """Compute a freshness score in [0.0, 1.0] for an entity or relationship.

    Returns ``0.0`` when no access data is available (``access_count == 0``
    or ``last_accessed_at is None``), signalling graceful degradation to
    the caller's existing weight distribution.

    Args:
        last_accessed_at: Timezone-aware UTC datetime of the most recent access.
            Naive datetimes are treated as UTC.
        access_count: Cumulative number of accesses recorded.
        half_life_days: Days until the base decay score halves (λ = ln2/half_life).
        alpha: Frequency-boost coefficient (ADR-0042 default: 0.1).
        max_boost: Maximum frequency multiplier (ADR-0042 default: 1.5).

    Returns:
        Freshness score in the range [0.0, 1.0].
    """
    if access_count == 0 or last_accessed_at is None:
        return 0.0

    # Normalise to UTC for safe subtraction
    if last_accessed_at.tzinfo is None:
        last_accessed_at = last_accessed_at.replace(tzinfo=timezone.utc)
    now = datetime.now(timezone.utc)
    days_since = max((now - last_accessed_at).total_seconds() / 86400.0, 0.0)

    # Exponential decay: e^(-λ × days),  λ = ln(2) / half_life_days
    if half_life_days <= 0:
        half_life_days = 1.0  # guard against misconfiguration
    decay_lambda = math.log(2) / half_life_days
    base_decay = math.exp(-decay_lambda * days_since)

    # Frequency boost: min(1 + α × ln(1 + count), max_boost)
    boost = min(1.0 + alpha * math.log1p(access_count), max_boost)

    return min(base_decay * boost, 1.0)


def classify_staleness(
    last_accessed_at: datetime | None,
    access_count: int,
    created_at: datetime | None,
    settings: Settings,
) -> StalenessTier:
    """Classify an entity or relationship into a staleness tier.

    Uses the ADR-0042 Decision 5 tier model:

    - **WARM**: Accessed within ``half_life_days``.
    - **COOLING**: Last access between ``half_life_days`` and
      ``2 × half_life_days``.
    - **COLD**: Last access between ``2 × half_life_days`` and
      ``cold_threshold_days``.
    - **DORMANT**: Last access > ``cold_threshold_days``, OR never accessed
      and created > ``cold_threshold_days`` ago.

    Args:
        last_accessed_at: Most recent access timestamp (UTC or naive UTC).
        access_count: Total recorded accesses.
        created_at: Node creation timestamp (used for never-accessed nodes).
        settings: Application settings carrying ``FreshnessSettings`` fields.

    Returns:
        The appropriate ``StalenessTier`` for this entity.
    """
    now = datetime.now(timezone.utc)

    def _days_since(dt: datetime | None) -> float | None:
        if dt is None:
            return None
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return max((now - dt).total_seconds() / 86400.0, 0.0)

    half_life = settings.freshness_half_life_days
    cold_threshold = settings.freshness_cold_threshold_days

    if access_count == 0 or last_accessed_at is None:
        # Never accessed — classify by creation age
        age_days = _days_since(created_at)
        if age_days is None or age_days > cold_threshold:
            return StalenessTier.DORMANT
        return StalenessTier.COLD

    days_since_access = _days_since(last_accessed_at)
    assert days_since_access is not None  # last_accessed_at is set

    if days_since_access <= half_life:
        return StalenessTier.WARM
    if days_since_access <= 2 * half_life:
        return StalenessTier.COOLING
    if days_since_access <= cold_threshold:
        return StalenessTier.COLD
    return StalenessTier.DORMANT


def staleness_tier_from_freshness_score(score: float) -> StalenessTier:
    """Derive a staleness tier from a pre-computed freshness score (ADR-0060 §D5).

    Avoids a second Neo4j round-trip in the reranking hot path by deriving the
    tier from the already-fetched freshness float.  Uses approximate thresholds
    calibrated for ``half_life_days=30`` (ADR-0042 default):

    - WARM     ≥ 0.50  (accessed within ~30 days)
    - COOLING  ≥ 0.25  (accessed within ~30–60 days)
    - COLD     ≥ 0.10  (accessed within ~60–90 days)
    - DORMANT  <  0.10  (last access >90 days ago or never accessed)

    If ``half_life_days`` is changed from its default, the tier boundaries
    will drift; adjust ``freshness_tier_factors`` in config instead of this
    function to compensate.

    Args:
        score: Freshness score in [0.0, 1.0] from ``compute_freshness()``.
            Callers must only invoke this for entities where ``score > 0.0``
            (zero-access entities are excluded upstream at the call site).

    Returns:
        The ``StalenessTier`` corresponding to the score range.
    """
    if score >= 0.50:
        return StalenessTier.WARM
    if score >= 0.25:
        return StalenessTier.COOLING
    if score >= 0.10:
        return StalenessTier.COLD
    return StalenessTier.DORMANT
