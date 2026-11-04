"""FRE-1730 AC-3: the hard stop. The number is the owner's, so it has no default."""

from __future__ import annotations

from dataclasses import dataclass

#: lifecycle-rules § Paid runs: stop at 1.5 times the projection.
PROJECTION_STOP_FACTOR = 1.5
#: The model reads its budget in a system message. A fixed value keeps that text the same in both arms until the
#: last sessions of a run.
SESSION_CAP_USD = 10.0


class RunStopped(Exception):
    """The run must stop, and master must be told why."""


@dataclass
class SpendStop:
    """Adds up the spend and stops the run.

    Each session also receives ``session_budget_usd`` as ``--max-budget-usd``, so one session cannot pass the stop by
    more than its last call. This stop runs on our side. It holds for a session that does not attack it, and a hook
    that reads the token can spend through the gate. Only a limit on the vendor's side is a real cap.

    Attributes:
        hard_stop_usd: The owner's number.
        projected_usd: The preflight's projection.
        spent_usd: The spend so far, which includes earlier attempts of this run.
    """

    hard_stop_usd: float
    projected_usd: float
    spent_usd: float = 0.0

    @property
    def cap_usd(self) -> float:
        """Return the lower of the two stops.

        Returns:
            The cap in USD.
        """
        return min(self.hard_stop_usd, self.projected_usd * PROJECTION_STOP_FACTOR)

    @property
    def remaining_usd(self) -> float:
        """Return what remains under the cap.

        Returns:
            The remainder in USD, zero or more.
        """
        return max(self.cap_usd - self.spent_usd, 0.0)

    @property
    def session_budget_usd(self) -> float:
        """Return the next session's budget.

        Returns:
            The fixed session cap, or what remains if that is less.
        """
        return min(SESSION_CAP_USD, self.remaining_usd)

    def check(self) -> None:
        """Stop the run if nothing remains.

        Raises:
            RunStopped: If the spend reached the cap.
        """
        if self.remaining_usd <= 0:
            raise RunStopped(
                f"spend ${self.spent_usd:.2f} reached the cap ${self.cap_usd:.2f} "
                f"(hard stop ${self.hard_stop_usd:.2f}, projection ${self.projected_usd:.2f})"
            )

    def add(self, cost_usd: float) -> None:
        """Add one session.

        Args:
            cost_usd: The session's cost.
        """
        self.spent_usd += cost_usd
