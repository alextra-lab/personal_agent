"""The closed sub-agent worker registry (ADR-0150 D2).

A worker type is a bundle the planner picks by name: a description the planner
reads, a prompt block appended to the shared base prompt, a closed tool list, a
report schema name, and a default thoroughness. The planner writes the task and
picks the type and the level. It chooses nothing else. Before this registry the
role, the tools and the report shape were re-invented by the planner on every
turn, which is where "the prompts were never configured" came from.

A type's tool list is a request. Governance still filters it
(``expansion_controller._compute_sub_agent_grants``), so a type cannot grant a
tool that ``config/governance/tools.yaml`` refuses.

Every worker of one type renders byte-identical system bytes and tool bytes, so
same-type workers share one cached prefix across a fan-out, across turns and
across thoroughness levels. Two types in one fan-out are two prefixes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from personal_agent.config.settings import THOROUGHNESS_LEVELS, Thoroughness

__all__ = [
    "THOROUGHNESS_LEVELS",
    "WORKER_TYPES",
    "Thoroughness",
    "WorkerType",
    "WorkerTypeSpec",
    "worker_types_declaring",
]


class WorkerType(str, Enum):
    """The closed set of sub-agent worker types (ADR-0150 D2)."""

    RESEARCHER = "researcher"
    GENERAL = "general"


@dataclass(frozen=True)
class WorkerTypeSpec:
    """Everything one worker type declares. The planner chooses none of it.

    Attributes:
        description: One sentence rendered into the planner prompt so it can pick
            the type.
        prompt_block: Text appended to the base system prompt for this type only.
            Empty for a type with no block.
        tools: The closed tool list this type requests, filtered by governance.
        report_schema: The report schema's name, or ``None`` for a text-reporting
            type. ADR-0150 T3 (FRE-1494) builds the schema. Until then every type
            reports in text and this is carried as a name only.
        default_thoroughness: The level used when the plan names none.
    """

    description: str
    prompt_block: str
    tools: tuple[str, ...]
    report_schema: str | None
    default_thoroughness: Thoroughness


# ADR-0150 D5, verbatim through the self-stop sentence. Two departures, both
# deliberate:
#   - The absence sentence (the one beginning "A report that something is
#     absent") is the owner's 2026-09-11 requirement on FRE-1493: absence is an
#     acceptable outcome, stated neutrally, tied to what was searched, never
#     encouraged.
#   - The ADR ends with "To finish, reply with the single word DONE and no tool
#     calls. You will then be asked for your report." That needs T3's
#     voluntary-stop report-writing call. Without it the completed path returns
#     the reply itself as the report, so an obedient worker would report "DONE".
#     T3 replaces "Then write your report." with the ADR's two sentences.
_RESEARCHER_BLOCK = (
    "You research one bounded question on the open web.\n"
    "Prefer primary sources: the organiser, the venue, the official listing, the "
    "publisher. A news article that names its source is second. An aggregator is "
    "last, and never the only source for a claim.\n"
    "Record a claim only when a fetched result states it. Quote a source's exact "
    "words only when the wording is load-bearing. Do not recap pages you merely read.\n"
    "If you find nothing for part of the task, say so and say what you searched — a "
    "report that names nothing you looked for is indistinguishable from never having "
    "looked.\n"
    "A report that something is absent, naming what you searched and where, is "
    "complete for that part. It needs no apology and no substitute answer. Absence "
    "you did not search for is not a finding.\n"
    "Stop searching when your last two searches returned the same facts. Then write "
    "your report."
)

WORKER_TYPES: Mapping[WorkerType, WorkerTypeSpec] = MappingProxyType(
    {
        WorkerType.RESEARCHER: WorkerTypeSpec(
            description=(
                "Finds facts on the open web for one bounded question and reports "
                "them as data with sources and gaps"
            ),
            prompt_block=_RESEARCHER_BLOCK,
            tools=("web_search",),
            report_schema="worker_report_v1",
            default_thoroughness="standard",
        ),
        WorkerType.GENERAL: WorkerTypeSpec(
            description=(
                "Answers a bounded question from its own knowledge, a computation, "
                "or the user's own memory, and reports in text"
            ),
            prompt_block="",
            tools=("run_python", "search_memory", "recall_personal_history"),
            report_schema=None,
            default_thoroughness="quick",
        ),
    }
)


def worker_types_declaring(tool_name: str) -> tuple[WorkerType, ...]:
    """The worker types whose closed tool list names ``tool_name``.

    Args:
        tool_name: A tool name, typically from a worker's stated tool gap.

    Returns:
        Every declaring type, in registry order. Empty when no type declares it.
    """
    return tuple(t for t, spec in WORKER_TYPES.items() if tool_name in spec.tools)
