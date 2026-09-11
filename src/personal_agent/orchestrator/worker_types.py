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
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from personal_agent.config.settings import THOROUGHNESS_LEVELS, Thoroughness

__all__ = [
    "THOROUGHNESS_LEVELS",
    "WORKER_REPORT_RESPONSE_FORMAT",
    "WORKER_REPORT_SCHEMA_NAME",
    "WORKER_TYPES",
    "Finding",
    "Gap",
    "Thoroughness",
    "WorkerReport",
    "WorkerType",
    "WorkerTypeSpec",
    "render_report_instruction",
    "render_worker_report_body",
    "render_worker_report_findings",
    "render_worker_report_summary",
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


# ADR-0150 D1 — the schema every property required, every object closed. No
# field carries a default: a default would make Pydantic's JSON Schema mark it
# optional, and every property here is required by design — a "may be empty"
# field is still always present, just allowed to hold "".
class Finding(BaseModel):
    """One data point a schema-backed worker found, with its source (ADR-0150 D1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    claim: str = Field(..., min_length=1, max_length=400)
    source_url: str = Field(..., min_length=1, max_length=200)
    date_or_period: str = Field(..., max_length=60)
    why_it_matters: str = Field(..., min_length=1, max_length=150)


class Gap(BaseModel):
    """One thing the task asked for that the worker did not find (ADR-0150 D1)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    looked_for: str = Field(..., min_length=1, max_length=150)
    where: str = Field(..., min_length=1, max_length=200)


class WorkerReport(BaseModel):
    """``worker_report_v1`` — the schema a schema-backed worker's landing call returns.

    Key order is part of the contract: ``working_notes`` first, so the model
    writes free text before it commits to the constrained fields (ADR-0150 D1,
    the reasoning-before-constraining literature the ADR cites).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    working_notes: str = Field(..., max_length=1000)
    findings: list[Finding] = Field(..., max_length=20)
    gaps: list[Gap] = Field(..., max_length=10)
    tool_gap: str = Field(..., max_length=60)


WORKER_REPORT_SCHEMA_NAME = "worker_report_v1"


def _inline_refs(schema: dict[str, Any]) -> dict[str, Any]:
    """Resolve every ``$ref``/``$defs`` pair into one self-contained schema.

    Pydantic v2 emits nested models as ``$ref`` into a top-level ``$defs``
    block. Kept as-is, a strict-mode consumer that dislikes sibling keys next
    to a ``$ref`` (OpenAI's structured-outputs contract) could reject it, and
    a schema split across ``$defs`` is one more thing a grammar compiler has
    to resolve. Inlining removes both concerns.

    Args:
        schema: The output of ``WorkerReport.model_json_schema()``.

    Returns:
        The same schema with every ``$ref`` replaced by its definition and no
        ``$defs`` block remaining.
    """
    defs = schema.get("$defs", {})

    def _resolve(node: Any) -> Any:
        if isinstance(node, dict):
            if "$ref" in node:
                ref_name = node["$ref"].rsplit("/", 1)[-1]
                resolved = _resolve(defs[ref_name])
                overrides = {k: v for k, v in node.items() if k != "$ref"}
                return {**resolved, **overrides}
            return {k: _resolve(v) for k, v in node.items() if k != "$defs"}
        if isinstance(node, list):
            return [_resolve(v) for v in node]
        return node

    resolved_schema: dict[str, Any] = _resolve(schema)
    resolved_schema.pop("$defs", None)
    return resolved_schema


WORKER_REPORT_JSON_SCHEMA: Mapping[str, Any] = MappingProxyType(
    _inline_refs(WorkerReport.model_json_schema())
)

#: ADR-0150 D1's constrained-landing request shape: ``response_format`` for the
#: report-writing call of a schema-backed worker whose dialect accepts it.
WORKER_REPORT_RESPONSE_FORMAT: Mapping[str, Any] = MappingProxyType(
    {
        "type": "json_schema",
        "json_schema": {
            "name": WORKER_REPORT_SCHEMA_NAME,
            "schema": WORKER_REPORT_JSON_SCHEMA,
            "strict": True,
        },
    }
)

# ADR-0150 D5's task-message report line, rendered from the schema in words so
# the model plans for the limits rather than discovering them by truncation.
_WORKER_REPORT_INSTRUCTION = (
    "Report: working notes (up to 1000 characters, may be empty); up to 20 "
    "findings, each a claim (up to 400 characters), a source URL (up to 200 "
    "characters), a date or period (up to 60 characters, may be empty), and why "
    "it matters (up to 150 characters); up to 10 gaps, each what you looked for "
    "(up to 150 characters) and where you looked (up to 200 characters); and one "
    "tool name you lacked, if any (up to 60 characters, may be empty)."
)
_TEXT_REPORT_INSTRUCTION = "Report in text."


def render_report_instruction(worker_type: "WorkerType") -> str:
    """The task message's report line for this type (ADR-0150 D5).

    Args:
        worker_type: The registry type running the task.

    Returns:
        The schema description in words for a schema-backed type, else
        ``"Report in text."``.
    """
    if WORKER_TYPES[worker_type].report_schema is None:
        return _TEXT_REPORT_INSTRUCTION
    return _WORKER_REPORT_INSTRUCTION


def render_worker_report_findings(report: WorkerReport) -> list[str]:
    """Render every finding as one line: ``claim — why_it_matters [source_url] (date)``.

    Args:
        report: A validated worker report.

    Returns:
        One rendered line per finding, in the report's own order.
    """
    lines = []
    for f in report.findings:
        date_suffix = f" ({f.date_or_period})" if f.date_or_period else ""
        lines.append(f"{f.claim} — {f.why_it_matters} [{f.source_url}]{date_suffix}")
    return lines


def render_worker_report_body(report: WorkerReport) -> str:
    """Render a report's findings and notes — no gaps section (ADR-0150 D4).

    ``_build_synthesis_context`` uses this: every worker's gaps are combined
    into one ``Not found`` section at the end of the turn's context rather
    than repeated per worker.

    Args:
        report: A validated worker report.

    Returns:
        The rendered findings, then the notes if any.
    """
    lines = render_worker_report_findings(report)
    if report.working_notes:
        lines.append("")
        lines.append(report.working_notes)
    return "\n".join(lines)


def render_worker_report_summary(report: WorkerReport) -> str:
    """Render the whole report: findings, then ``Not found``, then notes (ADR-0150 D1).

    This is ``SubAgentResult.summary`` for a ``synthesized`` schema-backed
    landing — complete on its own, unlike :func:`render_worker_report_body`.

    Args:
        report: A validated worker report.

    Returns:
        The deterministic markdown rendering of the whole report.
    """
    lines = render_worker_report_findings(report)
    if report.gaps:
        lines.append("")
        lines.append("Not found:")
        lines.extend(f"- {g.looked_for} (looked: {g.where})" for g in report.gaps)
    if report.working_notes:
        lines.append("")
        lines.append(report.working_notes)
    return "\n".join(lines)


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
    "Stop searching when your last two searches returned the same facts. To finish, "
    "reply with the single word DONE and no tool calls. You will then be asked for "
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
            report_schema=WORKER_REPORT_SCHEMA_NAME,
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
