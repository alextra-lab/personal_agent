"""FRE-1611 AC-4 — list what the promotion pipeline would file, and write nothing.

The owner reads this list before ``AGENT_SESHAT_OPEN_TICKET_CAP`` goes back to 10. The run
uses the production decisions: the decided-kind skip, the signal ranking, the fingerprint
lookup in Linear and the ticket cap. It reads Linear, the sysgraph and the Captain's Log
files. It creates no ticket, links nothing, changes no entry and writes no event.

Run on the host, from the repository root, with the production ``.env``:
    uv run python -m scripts.fre1611_promotion_dry_run

Post the output on the ticket. Each row carries its premise (why, seen count, first seen).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Sequence

from personal_agent.captains_log.linear_client import LinearClient
from personal_agent.captains_log.promotion import PromotionCriteria, PromotionPipeline
from personal_agent.config import settings

_ROW_FIELDS: tuple[str, ...] = (
    "action",
    "entry_id",
    "title",
    "priority",
    "source",
    "category",
    "scope",
    "seen_count",
    "first_seen",
    "fingerprint",
    "existing_issue",
)


async def _forbidden_create(*_args: object, **_kwargs: object) -> str | None:
    """Fail loudly if a dry run ever reaches ticket creation."""
    raise RuntimeError("dry run must not create a Linear issue")


def render(planned: Sequence[dict[str, str]], *, cap: int, open_count: int) -> str:
    """Render the plan as text the owner can read on a ticket.

    Args:
        planned: The rows from ``PromotionPipeline.planned``.
        cap: The configured ticket cap.
        open_count: The open pipeline-filed tickets now.

    Returns:
        The report.
    """
    lines = [
        "FRE-1611 promotion dry run (nothing was written)",
        f"cap={cap}  open pipeline-filed tickets={open_count}  headroom={max(cap - open_count, 0)}",
        f"would file or link: {len(planned)}",
    ]
    for index, row in enumerate(planned, start=1):
        lines.append("")
        lines.append(f"{index}. {row['action'].upper()}  {row['title']}")
        for name in _ROW_FIELDS:
            if name not in ("action", "title") and row.get(name):
                lines.append(f"   {name}: {row[name]}")
        lines.append(f"   what: {row['what']}")
        lines.append(f"   why: {row['why']}")
        lines.append(f"   how: {row['how']}")
    return "\n".join(lines)


async def _main(argv: Sequence[str]) -> int:
    argparse.ArgumentParser(description=__doc__.split("\n\n")[0]).parse_args(argv)

    client = LinearClient()
    open_count = await client.count_open_pipeline_issues(settings.linear_team_name)

    sysgraph_repo = None
    try:
        from personal_agent.sysgraph import SysgraphRepository

        sysgraph_repo = SysgraphRepository(settings.sysgraph_database_url)
        await sysgraph_repo.connect()
    except Exception as exc:
        print(f"sysgraph unavailable, decided-kind skip and ranking are off: {exc}")  # noqa: T201

    try:
        pipeline = PromotionPipeline(
            criteria=PromotionCriteria(max_existing_linear_issues=settings.promotion_initial_cap),
            create_issue_fn=_forbidden_create,
            linear_client=client,
            sysgraph_repo=sysgraph_repo,
            dry_run=True,
        )
        await pipeline.run()
    finally:
        if sysgraph_repo is not None:
            await sysgraph_repo.disconnect()

    print(render(pipeline.planned, cap=settings.seshat_open_ticket_cap, open_count=open_count))  # noqa: T201
    return 0


def main() -> int:
    """Run the dry run."""
    return asyncio.run(_main(sys.argv[1:]))


if __name__ == "__main__":
    raise SystemExit(main())
