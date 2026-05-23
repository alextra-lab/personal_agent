"""Deterministic session sampling for the joinability probe.

The sampler picks one session id from the last ``window_hours`` of traffic,
using a seed derived from the run's start time rounded to the nearest hour.
A failed run can be reproduced exactly by re-running with the logged seed.
"""

from __future__ import annotations

import random
from collections.abc import Sequence
from datetime import datetime


def seed_for(started_at: datetime) -> int:
    """Compute the deterministic sampling seed for a run start time.

    The seed is the epoch second of ``started_at`` rounded down to the hour.
    Two probe runs that fire within the same hour will sample the same
    session id when the eligible pool is identical — which is fine and makes
    debugging "the probe was red ten minutes ago, what did it walk?" trivial.

    Args:
        started_at: Wall-clock start of the run (timezone-aware).

    Returns:
        Integer seed suitable for :class:`random.Random`.
    """
    return int(started_at.timestamp()) - (int(started_at.timestamp()) % 3600)


def pick_session(
    eligible_session_ids: Sequence[str],
    *,
    seed: int,
) -> str | None:
    """Pick one session id from the eligible pool, deterministically.

    Args:
        eligible_session_ids: Pool of session ids that the caller has
            already filtered (e.g. ``WHERE created_at BETWEEN now()-window
            AND now()-5min`` and ``ORDER BY id`` for determinism).
        seed: Output of :func:`seed_for`, or a CLI override.

    Returns:
        Selected session id, or ``None`` when the pool is empty.
    """
    if not eligible_session_ids:
        return None
    return random.Random(seed).choice(eligible_session_ids)
