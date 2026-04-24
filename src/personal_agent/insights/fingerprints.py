"""Deterministic fingerprint utilities for InsightsEngine (ADR-0057 §D6).

Extracted to a shared module so both InsightsEngine and pipeline_handlers
import the same implementation, ensuring fingerprints in bus events match
fingerprints in CaptainLogEntry proposals.
"""

import hashlib
import re
from typing import Literal

_DIGIT_RUN_RE = re.compile(r"\d+")


def normalise_insight_title(title: str) -> str:
    """Collapse digit runs in a title string for stable dedup fingerprinting.

    Converts "$4.12 on 2026-04-19" and "$5.23 on 2026-04-20" to the same
    normalised form so they produce the same fingerprint (ADR-0057 §D6).

    Args:
        title: Raw insight title.

    Returns:
        Lowercased title with all digit runs replaced by ``#``.
    """
    return _DIGIT_RUN_RE.sub("#", title.strip().lower())


def pattern_fingerprint(insight_type: str, pattern_kind: str, title: str) -> str:
    """Deterministic 16-hex fingerprint for an insight (ADR-0057 §D6).

    Args:
        insight_type: Insight type (e.g. "correlation", "delegation").
        pattern_kind: Sub-discriminator (e.g. "delegation_success_rate" or "").
        title: Raw insight title — normalised internally.

    Returns:
        First 16 hex characters of SHA-256 hash.
    """
    key = f"{insight_type}:{pattern_kind}:{normalise_insight_title(title)}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def cost_fingerprint(anomaly_type: str, observation_date: str) -> str:
    """Deterministic 16-hex fingerprint for a cost anomaly (ADR-0057 §D6).

    Args:
        anomaly_type: Anomaly type (e.g. "daily_cost_spike").
        observation_date: ISO yyyy-mm-dd of the date that spiked.

    Returns:
        First 16 hex characters of SHA-256 hash.
    """
    key = f"{anomaly_type}:{observation_date}".encode()
    return hashlib.sha256(key).hexdigest()[:16]


def severity_for_cost_ratio(ratio: float) -> Literal["low", "medium", "high"]:
    """Classify cost anomaly severity (ADR-0057 §D5).

    Args:
        ratio: observed_cost / baseline_cost.

    Returns:
        ``"high"`` (≥ 4.0), ``"medium"`` (2.5–4.0), or ``"low"`` (< 2.5).
    """
    if ratio >= 4.0:
        return "high"
    if ratio >= 2.5:
        return "medium"
    return "low"
