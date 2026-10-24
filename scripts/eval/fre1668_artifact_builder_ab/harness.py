"""FRE-1668: artifact builder side-by-side, claude_sonnet against claude_sonnet_5_5 and gpt-6.1-sol.

The owner asked on 2026-10-24 for a test of the artifact building of Sol and Sonnet 5.5. The
ticket asks one question: must the ``artifact_builder`` role move from ``claude_sonnet`` to
``claude_sonnet_5_5`` or to ``gpt-6.1-sol``? This harness reports a result. It changes no binding.

Pilot (owner, 2026-10-24): 3 rich prompts, the same 3 for every arm, 12 builds. Arms: ``A`` claude_sonnet (baseline) · ``A2`` claude_sonnet again (noise) · ``B`` claude_sonnet_5_5 ·
``C`` gpt-6.1-sol. The unit under test is the real ``artifact_draft_executor``. Each prompt has one
frozen plan (step ``plans``), so only the builder model varies between arms.

Launch every paid step with the eval Postgres as the database, so that the cost gate, the cost
tracker and the session engine all write there and nowhere else (the harness checks all three)::

    export AGENT_DATABASE_URL=postgresql+asyncpg://agent:$POSTGRES_PASSWORD@127.0.0.1:5434/personal_agent
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness prod-count --label before
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness plans --ids p1
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness build --ids p1
    # ... the owner's go is recorded on FRE-1668 ...
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness plans
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness build
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness prod-count --label after
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness bundle   # hand <run>/rating/ to the owner
    uv run python -m scripts.eval.fre1668_artifact_builder_ab.harness report   # after <run>/scores.json

No production write: ``artifact_write_executor`` is replaced by a function that saves the HTML to the
run directory, no build has a session id, and ``prod-count`` reads the production row counts before and
after. The render check makes GET requests to the artifacts origin for ``/lib/`` files. These are reads.

All output lands in ``telemetry/evaluation/fre1668-artifact-builder-ab/`` (gitignored).
"""

from __future__ import annotations

import argparse
import asyncio
import html as html_lib
import json
import random
import re
import shutil
import statistics
import subprocess  # nosec B404 — fixed argv, no shell
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlsplit

import yaml
from structlog.testing import capture_logs

from personal_agent.config import settings
from personal_agent.config.model_loader import load_model_config
from personal_agent.llm_client.models import ModelConfig

HERE = Path(__file__).parent
REPO = HERE.parents[2]
OUT_DIR = Path("telemetry/evaluation/fre1668-artifact-builder-ab")
HTML_DIR = OUT_DIR / "html"
RENDER_JS = HERE / "render_check.mjs"
LIB_MAP = REPO / "config/artifact_lib_substitution_map.json"

#: Arm name to catalog key. ``A2`` repeats ``A``: the gap between them is the noise band.
ARMS: dict[str, str] = {
    "A": "claude_sonnet",
    "A2": "claude_sonnet",
    "B": "claude_sonnet_5_5",
    "C": "gpt-6.1-sol",
}
ARM_LABEL = {"A": "A", "A2": "A'", "B": "B", "C": "C"}
PLAN_KEY = "claude_sonnet"
BLIND_SEED = 20261024
#: The owner's five questions (FRE-1668 comment, 2026-10-24). Each page gets a 1 to 5 score on each.
QUESTIONS: dict[str, str] = {
    "widgets": "The interactive widgets work",
    "creative": "It is creative",
    "easy_to_learn": "It is easy to learn from",
    "attractive": "It is attractive and enticing",
    "encourages_use": "It encourages learning or continued use",
}
EVAL_DB = (
    "127.0.0.1",
    5434,
)  # fre-375-allow: the eval Postgres, the only database this harness may use
DB_BINDINGS = ("settings", "cost_tracker", "engine")
PROD_PG_CONTAINER = "cloud-sim-postgres"
EVAL_PG_CONTAINER = "cloud-sim-postgres-eval"
PLAN_MAX_TOKENS = 24000  # room for adaptive thinking plus a plan of at most 16,000 characters
WORST_CASE_USD_PER_BUILD = Decimal("0.40")  # 32,768 output tokens at $10/MTok, plus input
WORST_CASE_USD_PER_PLAN = Decimal("0.15")

JSONDict = dict[str, object]


class EvalIsolationError(RuntimeError):
    """A paid step would write somewhere other than the eval stack."""


class ArmResolutionError(RuntimeError):
    """An arm would silently run on another model than the one requested."""


class IncompleteRunError(RuntimeError):
    """The run data cannot support a report."""


class BudgetShortfallError(RuntimeError):
    """A budget cap lacks the headroom for the projected spend."""


_CREDIT_RE = re.compile(
    r"\b402\b|credit balance|insufficient[_ ]quota|exceeded your current quota|billing|"
    r"payment required|out of credits?|credits? (?:are|is) (?:exhausted|depleted)",
    re.IGNORECASE,
)


def is_vendor_credit_error(text: str) -> bool:
    """Tell a vendor credit or quota failure from any other failure.

    Args:
        text: A failure reason.

    Returns:
        True for a 402, a low credit balance, an exhausted quota or a billing limit.
    """
    return bool(_CREDIT_RE.search(text))


def is_infrastructure_stop(reason: str) -> bool:
    """Tell a stop that has no model output (credit, quota, budget) from a model failure.

    Args:
        reason: A failure reason.

    Returns:
        True for a vendor credit or quota error, or a denial by the cost gate.
    """
    return is_vendor_credit_error(reason) or "BudgetDenied" in reason


@dataclass(frozen=True)
class Prompt:
    """One artifact request.

    Attributes:
        id: ``p1``..``p8``.
        category: What the request covers.
        request: The user's request text.
        fixture: Path (relative to this package) of an existing page that the request edits.
    """

    id: str
    category: str
    request: str
    fixture: str | None = None


@dataclass(frozen=True)
class Plan:
    """The frozen builder input for one prompt (the arguments of ``artifact_draft``)."""

    prompt_id: str
    slug: str
    title: str
    summary: str
    plan: str
    cost_usd: float = 0.0
    trace_id: str = ""
    at: str = ""


@dataclass(frozen=True)
class Budget:
    """The connected cost gate and the spend at the start of a command.

    Attributes:
        gate: The connected ``CostGate``.
        caps: The budget caps.
        start: Spend per ``(window, role)`` at the start (the larger of the gate and the own record).
        own_start: The own record at the start, so later builds can be added on top of ``start``.
    """

    gate: Any
    caps: Sequence["Cap"]
    start: Mapping[tuple[str, str], Decimal]
    own_start: Mapping[tuple[str, str], Decimal]


@dataclass(frozen=True)
class Cap:
    """One budget cap, as ``config/governance/budget.yaml`` states it."""

    time_window: str
    role: str
    cap_usd: Decimal


# ── io helpers ────────────────────────────────────────────────────────────────


def _read_jsonl(path: Path) -> list[JSONDict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _append_jsonl(path: Path, row: Mapping[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row) + "\n")


def load_prompts() -> list[Prompt]:
    """Read the 3 pilot prompts.

    Returns:
        The prompts in file order.
    """
    spec = yaml.safe_load((HERE / "prompts.yaml").read_text(encoding="utf-8"))["prompts"]
    return [
        Prompt(
            id=p["id"],
            category=p["category"],
            request=p["request"].strip(),
            fixture=p.get("fixture"),
        )
        for p in spec
    ]


# ── isolation (AC-6) ──────────────────────────────────────────────────────────


def assert_eval_database(urls: Mapping[str, str]) -> None:
    """Stop unless every database binding names the eval Postgres.

    Args:
        urls: Binding name to URL. Must hold every name in ``DB_BINDINGS``.

    Raises:
        EvalIsolationError: A binding is missing or names another host or port.
    """
    missing = [name for name in DB_BINDINGS if name not in urls]
    if missing:
        raise EvalIsolationError(f"database bindings not checked: {missing}")
    for name, url in urls.items():
        parts = urlsplit(url)
        if (parts.hostname, parts.port) != EVAL_DB:
            raise EvalIsolationError(
                f"{name} binds {parts.hostname}:{parts.port}, not the eval Postgres "
                f"{EVAL_DB[0]}:{EVAL_DB[1]}. Set AGENT_DATABASE_URL before the run."
            )


def collect_db_bindings() -> dict[str, str]:
    """Return the database URL of the settings, the cost tracker and the session engine.

    Returns:
        Binding name to URL. The engine URL has its password hidden.
    """
    from personal_agent.llm_client.cost_tracker import cost_tracker_service  # noqa: PLC0415
    from personal_agent.service.database import engine  # noqa: PLC0415

    return {
        "settings": settings.database_url,
        "cost_tracker": cost_tracker_service.db_url,
        "engine": engine.url.render_as_string(hide_password=True),
    }


def assert_isolated() -> None:
    """Check the database bindings, the R2 settings and the event bus before a paid call.

    Raises:
        EvalIsolationError: Any binding could reach production.
    """
    from personal_agent.events.bus import get_event_bus  # noqa: PLC0415

    assert_eval_database(collect_db_bindings())
    endpoint = settings.r2_endpoint_url
    if endpoint and not urlsplit(endpoint).hostname.endswith(".invalid"):  # type: ignore[union-attr]
        raise EvalIsolationError("AGENT_R2_ENDPOINT_URL names a real endpoint; use a .invalid host")
    if type(get_event_bus()).__name__ != "NoOpBus":
        raise EvalIsolationError(
            "an event bus is attached; the harness must run with the no-op bus"
        )


_PROD_SQL = """
select 'artifacts', count(*), coalesce(max(created_at)::text, '') from artifacts
union all select 'session_events', count(*), coalesce(max(created_at)::text, '') from session_events
union all select 'api_costs', count(*), coalesce(max(timestamp)::text, '') from api_costs;
"""


def _psql(container: str, sql: str) -> list[list[str]]:
    """Run one read-only statement inside a Postgres container. Credentials stay in the container."""
    # fre-375-allow: read-only SELECT; production is read for the AC-6 proof
    proc = subprocess.run(  # nosec B603 B607 — fixed argv, no user input
        [
            "docker", "exec", "-i", container, "sh", "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|"',
        ],
        input=sql, capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    return [line.split("|") for line in proc.stdout.splitlines()]


def prod_counts() -> dict[str, dict[str, object]]:
    """Read production row counts. Read only.

    Returns:
        Table name to ``{"count": n, "newest": timestamp}``.
    """
    return {t: {"count": int(c), "newest": n} for t, c, n in _psql(PROD_PG_CONTAINER, _PROD_SQL)}


def trace_hits(container: str, trace_ids: Sequence[str]) -> dict[str, int]:
    """Count the rows that carry the harness's trace ids, in one database.

    ``budget_reservations`` is the positive control: the cost gate writes one row per paid call to
    the database it is connected to. ``api_costs`` stays empty on purpose, because a call with no
    session or user identity records no cost row.

    Args:
        container: The Postgres container to read.
        trace_ids: Trace ids of every paid call the harness made. Each must be a UUID.

    Returns:
        Table name to row count. Production must give 0 in both (AC-6). The eval database must
        give one ``budget_reservations`` row per trace id, which proves the query can see the rows.

    Raises:
        ValueError: A trace id is not a UUID.
    """
    ids = [str(uuid.UUID(t)) for t in trace_ids]  # rejects anything that is not a UUID
    if not ids:
        return {"budget_reservations": 0, "api_costs": 0}
    listing = ",".join(f"'{i}'" for i in ids)
    return {
        table: int(
            _psql(container, f"select count(*) from {table} where trace_id in ({listing});")[0][0]
        )
        for table in ("budget_reservations", "api_costs")
    }


# ── arms and budget ───────────────────────────────────────────────────────────


def resolve_arms(
    catalog: ModelConfig, *, is_provider_available: Callable[[str], bool]
) -> dict[str, str]:
    """Check that every arm resolves to the key it asks for.

    Args:
        catalog: The model catalog.
        is_provider_available: Provider-availability predicate (credential present).

    Returns:
        Arm to resolved key. Equal to ``ARMS`` when the check passes.

    Raises:
        ArmResolutionError: An arm would run on another key (for example a missing API key).
    """
    from personal_agent.orchestrator.constraint_options import (  # noqa: PLC0415
        ConstraintDecision,
        resolve_effective_artifact_builder_deployment,
    )

    resolved: dict[str, str] = {}
    for arm, key in ARMS.items():
        got = resolve_effective_artifact_builder_deployment(
            ConstraintDecision(key, "user_choice"),
            catalog,
            is_provider_available=is_provider_available,
        )
        if got != key:
            raise ArmResolutionError(
                f"arm {arm} asks for {key} but would run on {got}. "
                "Check the provider API key and the catalog entry."
            )
        resolved[arm] = got
    return resolved


def check_headroom(
    caps: Sequence[Cap],
    spent: Mapping[tuple[str, str], Decimal],
    projected: Mapping[str, Decimal],
) -> None:
    """Stop unless every cap can hold the projected spend.

    Args:
        caps: The budget caps.
        spent: Current spend per ``(time_window, role)``.
        projected: Projected spend per role. The ``_total`` caps take the sum of all roles.

    Raises:
        BudgetShortfallError: Naming every cap that would be exceeded.
    """
    total = sum(projected.values(), Decimal(0))
    short: list[str] = []
    for cap in caps:
        extra = total if cap.role == "_total" else projected.get(cap.role, Decimal(0))
        used = spent.get((cap.time_window, cap.role), Decimal(0))
        if used + extra > cap.cap_usd:
            short.append(
                f"{cap.time_window} {cap.role}: spent {used} + projected {extra} > cap {cap.cap_usd}"
            )
    if short:
        raise BudgetShortfallError("; ".join(short))


def own_spend(
    builds: Sequence[Mapping[str, object]],
    plans: Sequence[Mapping[str, object]],
    now: datetime,
) -> dict[tuple[str, str], Decimal]:
    """Sum the harness's own recorded costs per budget window and role.

    The cost gate reserves a call for a short time, and a reservation that expires before the call
    ends records $0. A build that runs longer than about three minutes therefore never reaches the
    gate's counters, although the vendor charged it. The model log gives the true cost, so the
    budget check takes the larger of the gate's counter and this sum.

    Args:
        builds: Build rows. A row without ``at`` is ignored.
        plans: Plan rows. A row without ``at`` is ignored.
        now: The current time.

    Returns:
        ``(window, role)`` to spend. Builds count to ``artifact_builder``, plans to ``study``, and
        both to ``_total``.
    """
    out: defaultdict[tuple[str, str], Decimal] = defaultdict(Decimal)
    for role, rows in (("artifact_builder", builds), ("study", plans)):
        for row in rows:
            if not row.get("at"):
                continue
            when = datetime.fromisoformat(str(row["at"]))
            cost = Decimal(str(row.get("cost_usd") or 0))
            windows = []
            if when.date() == now.date():
                windows.append("daily")
            if when.isocalendar()[:2] == now.isocalendar()[:2]:
                windows.append("weekly")
            for window in windows:
                out[(window, role)] += cost
                out[(window, "_total")] += cost
    return dict(out)


async def _gate_and_headroom(
    n_plans: int,
    n_builds: int,
    per_plan: Decimal = WORST_CASE_USD_PER_PLAN,
    per_build: Decimal = WORST_CASE_USD_PER_BUILD,
) -> Budget:
    """Connect the cost gate to the eval Postgres and check the caps.

    Args:
        n_plans: Plan calls that the command will make.
        n_builds: Builds that the command will make.
        per_plan: Estimated cost of one plan call.
        per_build: Estimated cost of one build.

    Returns:
        The connected gate with the caps and the starting spend.

    Raises:
        BudgetShortfallError: A cap lacks the headroom for the projected spend.
    """
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )
    from personal_agent.cost_gate.gate import _window_start  # noqa: PLC0415

    config = load_budget_config()
    gate = CostGate(config=config, db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    caps = [Cap(c.time_window, c.role, c.cap_usd) for c in config.caps]
    spent: dict[tuple[str, str], Decimal] = {}
    now = datetime.now(timezone.utc)
    async with gate.pool.acquire() as conn:  # type: ignore[union-attr]
        for cap in caps:
            row = await conn.fetchrow(
                "SELECT running_total FROM budget_counters WHERE user_id IS NULL AND "
                "time_window = $1 AND provider IS NULL AND role = $2 AND window_start = $3",
                cap.time_window, cap.role, _window_start(cap.time_window, now),
            )  # fmt: skip
            spent[(cap.time_window, cap.role)] = (
                Decimal(str(row["running_total"])) if row else Decimal(0)
            )
    own = own_spend(
        _read_jsonl(OUT_DIR / "builds.jsonl"), _read_jsonl(OUT_DIR / "plans.jsonl"), now
    )
    for key, value in own.items():
        spent[key] = max(spent.get(key, Decimal(0)), value)
    own_start = dict(own)
    projected = {
        "artifact_builder": per_build * n_builds,
        "study": per_plan * n_plans,
    }
    try:
        check_headroom(caps, spent, projected)
    except BudgetShortfallError:
        await gate.disconnect()
        set_default_gate(None)
        raise
    return Budget(gate=gate, caps=caps, start=spent, own_start=own_start)


def running_spend(
    start: Mapping[tuple[str, str], Decimal],
    own_start: Mapping[tuple[str, str], Decimal],
    own_now: Mapping[tuple[str, str], Decimal],
) -> dict[tuple[str, str], Decimal]:
    """Add the spend of this command's calls to the starting spend.

    Args:
        start: Spend at the start of the command.
        own_start: The own record at the start.
        own_now: The own record now.

    Returns:
        Spend per ``(window, role)`` now.
    """
    keys = set(start) | set(own_now)
    return {
        k: start.get(k, Decimal(0)) + own_now.get(k, Decimal(0)) - own_start.get(k, Decimal(0))
        for k in keys
    }


# ── plans ─────────────────────────────────────────────────────────────────────

_PLAN_SYSTEM = (
    "You are the planning model of a personal assistant. The user asked for a web page. Call the "
    "artifact_draft tool once. Give it a slug, a title, a one-sentence summary and a structured plan "
    "(sections, all data and content, style guidance). Do not write HTML. The plan limit is 16,000 "
    "characters, so keep the plan under 12,000: for a large table, describe the fields and the "
    "ranges and let the builder write the data, instead of listing every number."
)


def draft_tool_schema() -> dict[str, object]:
    """Return the ``artifact_draft`` tool in OpenAI function format, as the registry exports it.

    Returns:
        The tool definition for the planning call.
    """
    from personal_agent.tools.artifact_tools import artifact_draft_tool  # noqa: PLC0415

    props: dict[str, object] = {
        p.name: p.json_schema or {"type": p.type, "description": p.description}
        for p in artifact_draft_tool.parameters
    }
    return {
        "type": "function",
        "function": {
            "name": artifact_draft_tool.name,
            "description": artifact_draft_tool.description,
            "parameters": {
                "type": "object",
                "properties": props,
                "required": [p.name for p in artifact_draft_tool.parameters if p.required],
            },
        },
    }


async def make_plan(prompt: Prompt) -> Plan:
    """Ask the planning model for the ``artifact_draft`` arguments of one prompt.

    Args:
        prompt: The request.

    Returns:
        The frozen plan. For the edit prompt, the existing page is appended to the plan.

    Raises:
        RuntimeError: The model did not call ``artifact_draft`` with a plan.
    """
    from personal_agent.llm_client import ModelRole  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415
    from personal_agent.telemetry.trace import SystemTraceContext  # noqa: PLC0415

    existing = (HERE / prompt.fixture).read_text(encoding="utf-8") if prompt.fixture else None
    user = prompt.request + (f"\n\nThe existing page:\n{existing}" if existing else "")
    client = get_llm_client_for_key(PLAN_KEY, budget_role="study")
    ctx = SystemTraceContext.new("fre1668_plan")
    resp = await client.respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": user}],
        system_prompt=_PLAN_SYSTEM,
        tools=[draft_tool_schema()],
        max_tokens=PLAN_MAX_TOKENS,
        trace_ctx=ctx,
    )
    calls = [c for c in resp.get("tool_calls") or [] if c["name"] == "artifact_draft"]
    if not calls:
        evidence: JSONDict = {
            "prompt_id": prompt.id,
            "finish_reason": resp.get("finish_reason"),
            "usage": resp.get("usage"),
            "cost_usd": resp.get("cost_usd"),
            "tool_calls": len(resp.get("tool_calls") or []),
            "content_head": str(resp.get("content") or "")[:1500],
            "trace_id": ctx.trace_id,
        }
        _append_jsonl(OUT_DIR / "plan-failures.jsonl", evidence)
        raise RuntimeError(
            f"{prompt.id}: the planning model did not call artifact_draft "
            f"(finish_reason={evidence['finish_reason']}, usage={evidence['usage']}, "
            f"cost={evidence['cost_usd']}); evidence is in plan-failures.jsonl"
        )
    args = json.loads(calls[0]["arguments"])
    plan = str(args["plan"])
    if existing:
        plan += f"\n\nExisting page to revise (keep what the request does not change):\n{existing}"
    return Plan(
        prompt_id=prompt.id,
        slug=str(args["slug"]),
        title=str(args["title"]),
        summary=str(args["summary"]),
        plan=plan,
        cost_usd=float(resp.get("cost_usd") or 0.0),
        trace_id=ctx.trace_id,
        at=datetime.now(timezone.utc).isoformat(),
    )


# ── builds ────────────────────────────────────────────────────────────────────


def render_html(path: Path) -> JSONDict:
    """Run the headless render check on one saved page.

    Args:
        path: The saved HTML.

    Returns:
        The check result: error lists, blank flag, and ``render_ok``.
    """
    origin = settings.artifacts_public_base_url or "none"
    try:
        proc = subprocess.run(  # nosec B603 B607 — fixed argv, no shell
            ["node", str(RENDER_JS), str(path), origin, str(LIB_MAP), str(path.with_suffix(".png"))],
            capture_output=True, text=True, timeout=300, check=False,
        )  # fmt: skip
        result = cast(JSONDict, json.loads(proc.stdout.strip().splitlines()[-1]))
    except (subprocess.TimeoutExpired, OSError, IndexError, json.JSONDecodeError) as exc:
        result = {"page_errors": [f"render check crashed: {type(exc).__name__}: {exc}"[:300]]}
    for field in (
        "page_errors",
        "console_errors",
        "failed_requests",
        "blocked_requests",
        "lib_unavailable",
    ):
        result.setdefault(field, [])
    result.setdefault("blank", False)
    result.setdefault("widgets", {})
    result["render_ok"] = not (
        result["page_errors"] or result["console_errors"] or result["failed_requests"]
        or result["blocked_requests"] or result["blank"]
    )  # fmt: skip
    return result


def render_fields(path: Path) -> JSONDict:
    """Run the render check and the widget check and return the fields a build row stores.

    Args:
        path: The saved HTML.

    Returns:
        ``render_ok``, the error counts, and the widget summary (controls found, worked, with no
        reaction, unverifiable, not operated, with errors) with one entry per control.
    """
    check = render_html(path)
    w = cast(JSONDict, check["widgets"])
    return {
        "render_ok": check["render_ok"],
        "console_errors": len(cast(list[str], check["console_errors"])),
        "page_errors": len(cast(list[str], check["page_errors"])),
        "failed_requests": len(cast(list[str], check["failed_requests"])),
        "blocked_requests": len(cast(list[str], check["blocked_requests"])),
        "lib_unavailable": len(cast(list[str], check["lib_unavailable"])),
        "blank": check["blank"],
        "controls_found": w.get("found", 0),
        "controls_worked": w.get("worked", 0),
        "controls_no_reaction": w.get("no_reaction", 0),
        "controls_unverifiable": w.get("unverifiable", 0),
        "controls_not_operated": w.get("not_operated", 0),
        "controls_with_errors": w.get("with_errors", 0),
        "widget_errors": w.get("errors", 0),
        "controls": w.get("controls", []),
    }


async def build_one(
    plan: Plan, arm: str, *, skip_render: bool = False, attempt: int = 1
) -> JSONDict:
    """Build one page with the real ``artifact_draft_executor`` on one arm.

    Args:
        plan: The frozen builder input.
        arm: A key of ``ARMS``.
        skip_render: Skip the headless render check (tests).
        attempt: Attempt number. The first attempt is the scored one; later attempts save to
            a separate file.

    Returns:
        The per-build record. A failure is a row with ``ok=False`` and a reason, never a raise.
    """
    from personal_agent.orchestrator import constraint_options as co  # noqa: PLC0415
    from personal_agent.telemetry.trace import SystemTraceContext  # noqa: PLC0415
    from personal_agent.tools import artifact_tools  # noqa: PLC0415

    key = ARMS[arm]
    catalog = load_model_config()
    resolved = co.resolve_effective_artifact_builder_deployment(
        co.ConstraintDecision(key, "user_choice"),
        catalog,
        is_provider_available=co.build_provider_availability(catalog, settings),
    )
    suffix = "" if attempt == 1 else f"-r{attempt}"
    html_path = HTML_DIR / f"{plan.prompt_id}-{arm}{suffix}.html"
    saved: dict[str, int] = {}
    ctx = SystemTraceContext.new("fre1668_build")

    async def _capture_write(*, content: str, **_: object) -> dict[str, object]:
        html_path.parent.mkdir(parents=True, exist_ok=True)
        html_path.write_text(content, encoding="utf-8")
        saved["bytes"] = len(content.encode("utf-8"))
        return {"artifact_id": None, "size_bytes": saved["bytes"]}

    real_write = artifact_tools.artifact_write_executor
    artifact_tools.artifact_write_executor = _capture_write  # type: ignore[assignment]
    token = co.set_artifact_builder_resolution(co.ConstraintDecision(key, "user_choice"))
    failure = ""
    start = time.monotonic()
    try:
        with capture_logs() as logs:
            await artifact_tools.artifact_draft_executor(
                slug=plan.slug,
                title=plan.title,
                summary=plan.summary,
                plan=plan.plan,
                ctx=ctx,
            )
    except Exception as exc:  # noqa: BLE001 — every failure becomes a row with its reason
        failure = f"{type(exc).__name__}: {exc}"
    finally:
        latency = time.monotonic() - start
        artifact_tools.artifact_write_executor = real_write
        co.reset_artifact_builder_resolution(token)

    done = [e for e in logs if e.get("event") == "model_call_completed"]
    served = str(done[0].get("model", "")) if done else ""
    if not failure and resolved != key:
        failure = f"arm {arm} asked for {key} but resolved to {resolved}"
    if not failure and len(done) != 1:
        failure = f"expected one model_call_completed event, found {len(done)}"
    if not failure and served.split("/")[-1] != catalog.models[key].id:
        failure = f"arm {arm} asked for {catalog.models[key].id} but {served} answered"
    cap = next((e for e in logs if e.get("event") == "artifact_draft_output_cap_hit"), None)
    if cap is not None:
        # The owner's rule: a build that stops at the output ceiling is a failure with that reason.
        ceiling = (
            f"output ceiling reached ({cap.get('output_tokens')} of {cap.get('max_tokens')} tokens)"
        )
        failure = f"{ceiling}: {failure}" if failure else ceiling
    ok = not failure
    row: JSONDict = {
        "arm": arm,
        "prompt_id": plan.prompt_id,
        "requested_key": key,
        "resolved_key": resolved,
        "served_model": served,
        "effort": str(done[0].get("mode", "")) if done else "",
        "attempt": attempt,
        "at": datetime.now(timezone.utc).isoformat(),
        "trace_id": ctx.trace_id,
        "ok": ok,
        "failure_reason": failure,
        "render_ok": None,
        "latency_s": round(latency, 2),
        "input_tokens": done[0].get("input_tokens") if done else None,
        "output_tokens": done[0].get("output_tokens") if done else None,
        "reasoning_tokens": done[0].get("reasoning_tokens") if done else None,
        "thinking_present": done[0].get("anthropic_thinking_present") if done else None,
        "cost_usd": float(cast(float, done[0].get("cost_usd") or 0.0)) if done else 0.0,
        "html_bytes": saved.get("bytes", 0),
        "output_cap_hit": any(e.get("event") == "artifact_draft_output_cap_hit" for e in logs),
        "html_path": str(html_path) if "bytes" in saved else "",
    }
    if ok and not skip_render:
        row.update(render_fields(html_path))
    elif ok:
        row["render_ok"] = True
    return row


# ── rating pack and report ────────────────────────────────────────────────────


def lib_to_cdn(page: str, origin: str, entries: Sequence[Mapping[str, object]]) -> str:
    """Point ``/lib/`` script and style references at their pinned public-CDN copies.

    The owner's rating pack shows each page in a sandboxed iframe, which cannot pass the artifacts
    origin's access login. A font has no public copy, so it is left as it is and falls back to a
    system font on every arm alike.

    Args:
        page: The generated HTML.
        origin: The artifacts origin that the builder prompt names in ``/lib/`` references.
        entries: The ``entries`` of ``config/artifact_lib_substitution_map.json``.

    Returns:
        The HTML with each mapped reference rewritten.
    """
    for entry in entries:
        twin = entry.get("public_cdn_url")
        if twin:
            page = page.replace(f"{origin.rstrip('/')}/{entry['lib_path']}", str(twin))
    return page


def write_rating_pack(
    rows: Sequence[Mapping[str, object]],
    rating_dir: Path,
    key_path: Path,
    requests: Mapping[str, str] | None = None,
    transform: Callable[[str], str] = lambda page: page,
) -> dict[str, JSONDict]:
    """Write the blind pack for the owner and the key that stays with the run.

    Args:
        rows: Build rows. Only rows with ``ok`` and an ``html_path`` are rated.
        rating_dir: The directory handed to the owner. It receives ``index.html`` only.
        key_path: Where the label key goes. Must lie outside ``rating_dir``.
        requests: Prompt id to request text, shown beside each page.
        transform: Applied to each page before it goes into the pack (see ``lib_to_cdn``).

    Returns:
        Label to ``{"arm", "prompt_id"}``. The order is shuffled with a fixed seed.
    """
    rated = sorted(
        (r for r in rows if r.get("ok") and r.get("html_path")),
        key=lambda r: (str(r["prompt_id"]), str(r["arm"])),
    )
    random.Random(BLIND_SEED).shuffle(rated)
    key: dict[str, JSONDict] = {}
    parts: list[str] = []
    for n, row in enumerate(rated, start=1):
        label = f"P{n:02d}"
        key[label] = {"arm": row["arm"], "prompt_id": row["prompt_id"]}
        page = transform(Path(str(row["html_path"])).read_text(encoding="utf-8"))
        ask = html_lib.escape((requests or {}).get(str(row["prompt_id"]), ""))
        parts.append(
            f'<section><h2>{label}</h2><p class="ask">{ask}</p>'
            f'<iframe sandbox="allow-scripts" loading="lazy" srcdoc="{html_lib.escape(page, quote=True)}">'
            "</iframe></section>"
        )
    rating_dir.mkdir(parents=True, exist_ok=True)
    (rating_dir / "index.html").write_text(
        '<!DOCTYPE html><html lang="en"><head><meta charset="utf-8"><title>Rate the pages</title>'
        "<style>body{font-family:system-ui,sans-serif;max-width:70rem;margin:1rem auto;padding:0 1rem}"
        "iframe{width:100%;height:46rem;border:1px solid #888;background:#fff}"
        ".ask{color:#555}section{margin-bottom:2.5rem}</style></head><body>"
        "<h1>Rate each page from 1 (poor) to 5 (excellent) on five questions</h1><ol>"
        + "".join(f"<li>{html_lib.escape(q)}</li>" for q in QUESTIONS.values())
        + "</ol><p>Reply with one line per label, for example: P01 4 3 5 4 4</p>"
        + "".join(parts)
        + "</body></html>",
        encoding="utf-8",
    )
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_text(json.dumps(key, indent=1), encoding="utf-8")
    return key


def check_complete(
    rows: Sequence[Mapping[str, object]],
    labels: Mapping[str, object],
    scores: Mapping[str, object],
    *,
    n_prompts: int,
    need_scores: bool = True,
) -> None:
    """Fail unless the run data can support a report (AC-3, AC-4).

    Args:
        rows: Build rows.
        labels: The blind key.
        scores: The owner's scores per label: one 1 to 5 integer for each question in ``QUESTIONS``.
        n_prompts: Prompts in the set.
        need_scores: Require five valid scores per label.

    Raises:
        IncompleteRunError: A build is missing or repeated, a failure has no reason, a successful
            build has no render result, or a score is missing or invalid.
    """
    pairs = [(r["arm"], r["prompt_id"]) for r in rows]
    if len(pairs) != len(set(pairs)) or len(set(pairs)) != len(ARMS) * n_prompts:
        raise IncompleteRunError(
            f"expected {len(ARMS) * n_prompts} unique (arm, prompt) rows, found "
            f"{len(set(pairs))} unique in {len(pairs)}"
        )
    for r in rows:
        if not r.get("ok") and not str(r.get("failure_reason") or "").strip():
            raise IncompleteRunError(f"failed build {r['arm']}/{r['prompt_id']} has no reason")
        if r.get("ok") and r.get("render_ok") is None:
            raise IncompleteRunError(f"build {r['arm']}/{r['prompt_id']} has no render result")
    if need_scores:
        rated = sorted((str(v["arm"]), str(v["prompt_id"])) for v in labels.values())  # type: ignore[index]
        built = sorted((str(r["arm"]), str(r["prompt_id"])) for r in rows if r.get("ok"))
        if rated != built:
            raise IncompleteRunError(
                "the rating labels do not match the successful builds; bundle again"
            )
        bad = [k for k in labels if not _valid_scores(scores.get(k))]
        if bad or set(scores) != set(labels):
            raise IncompleteRunError(
                f"scores missing or invalid (five 1-to-5 integers needed) for labels: {sorted(bad)}"
            )


def _valid_scores(entry: object) -> bool:
    if not isinstance(entry, dict) or set(entry) != set(QUESTIONS):
        return False
    return all(
        isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 5 for v in entry.values()
    )


def _numbers(rows: Sequence[Mapping[str, object]], column: str) -> list[float]:
    return [float(cast(float, r[column])) for r in rows if r.get(column) is not None]


PAIRED = ", prompts every arm built"


def paired_prompts(rows: Sequence[Mapping[str, object]]) -> set[str]:
    """Return the prompts for which every arm produced a page.

    Latency, tokens, cost and widget counts depend on the prompt. They are only comparable between
    arms on prompts that every arm built.

    Args:
        rows: Build rows.

    Returns:
        Prompt ids.
    """
    built: defaultdict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("ok"):
            built[str(r["prompt_id"])].add(str(r["arm"]))
    return {pid for pid, arms in built.items() if set(ARMS) <= arms}


def blind_prompts(rows: Sequence[Mapping[str, object]]) -> set[str]:
    """Return the prompts whose pages the owner could compare blind: two or more arms made a page.

    Args:
        rows: Build rows.

    Returns:
        Prompt ids. A prompt with one page is not blind, because the arm is the only one that built.
    """
    built: defaultdict[str, set[str]] = defaultdict(set)
    for r in rows:
        if r.get("ok"):
            built[str(r["prompt_id"])].add(str(r["arm"]))
    return {pid for pid, arms in built.items() if len(arms) >= 2}


def _measure(rows: Sequence[Mapping[str, object]], arm: str, measure: str) -> float | None:
    if measure.endswith(PAIRED):
        keep = paired_prompts(rows)
        rows = [r for r in rows if r["prompt_id"] in keep]
        measure = measure[: -len(PAIRED)] + " [paired]"
    mine = [r for r in rows if r["arm"] == arm]
    ok = [r for r in mine if r.get("ok")]
    if measure == "success rate":
        return sum(1 for r in mine if r.get("ok")) / len(mine) if mine else None
    if measure == "render pass rate":
        return sum(1 for r in mine if r.get("render_ok")) / len(mine) if mine else None
    if measure == "controls worked share [paired]":
        found = sum(_numbers(ok, "controls_found"))
        return sum(_numbers(ok, "controls_worked")) / found if found else None
    if measure == "widget errors (sum) [paired]":
        return sum(_numbers(ok, "widget_errors")) if ok else None
    column = {
        "median latency (s), all builds": "latency_s",
        "median output tokens, all builds": "output_tokens",
        "mean cost (usd), all builds": "cost_usd",
        "median latency (s) [paired]": "latency_s",
        "median output tokens [paired]": "output_tokens",
        "mean cost (usd) [paired]": "cost_usd",
        "controls found (mean) [paired]": "controls_found",
        "owner: mean of five": "score",
        **{f"owner: {text}": f"score_{key}" for key, text in QUESTIONS.items()},
    }[measure]
    # Failed builds cost money and time, and they count in the owner's scores (as 1). Widget counts
    # exist only for pages.
    pool = ok if column.startswith("controls") else mine
    values = _numbers(pool, column)
    if not values:
        return None
    return statistics.median(values) if measure.startswith("median") else statistics.fmean(values)


MEASURES = (
    "success rate",
    "render pass rate",
    "median latency (s), all builds",
    "median output tokens, all builds",
    "mean cost (usd), all builds",
    "median latency (s)" + PAIRED,
    "median output tokens" + PAIRED,
    "mean cost (usd)" + PAIRED,
    "controls found (mean)" + PAIRED,
    "controls worked share" + PAIRED,
    "widget errors (sum)" + PAIRED,
    *(f"owner: {text}" for text in QUESTIONS.values()),
    "owner: mean of five",
)


def decision_table(rows: Sequence[Mapping[str, object]]) -> list[JSONDict]:
    """Compare B and C with A, against the noise band ``abs(A' - A)`` (AC-2).

    Args:
        rows: Build rows. The ``score_*`` and ``score`` fields are optional.

    Returns:
        One entry per measure and per arm in B, C. ``verdict`` is ``no decision`` when the
        difference is not larger than the noise band.
    """
    out: list[JSONDict] = []
    for measure in MEASURES:
        a, a2 = _measure(rows, "A", measure), _measure(rows, "A2", measure)
        if a is None or a2 is None:
            continue
        band = abs(a2 - a)
        for arm in ("B", "C"):
            value = _measure(rows, arm, measure)
            if value is None:
                continue
            diff = value - a
            out.append(
                {
                    "measure": measure, "arm": arm, "a": a, "a_prime": a2, "value": value,
                    "diff": diff, "noise_band": band,
                    "verdict": "no decision" if abs(diff) <= band else ("higher" if diff > 0 else "lower"),
                }
            )  # fmt: skip
    return out


def pilot_verdict(table: Sequence[Mapping[str, object]]) -> str:
    """Return the pilot go or no-go line (owner comment on FRE-1668, 2026-10-24 12:27).

    Go: B or C beats A by more than the A to A' difference on the owner's rating (the mean of the
    five questions, a failed build scoring 1), or is cheaper or faster by more than that difference
    at an equal rating. An arm that loses on success rate or render pass rate cannot qualify.
    No-go: no arm qualifies.

    Args:
        table: The output of ``decision_table``.

    Returns:
        One line that starts with ``Pilot verdict: GO`` or ``Pilot verdict: NO-GO``. A go line names
        every arm that qualifies, the strongest first.
    """
    found: list[tuple[int, float, str, str]] = []
    for arm in ("B", "C"):
        mine = {str(r["measure"]): r for r in table if r["arm"] == arm}
        if any(
            mine.get(m, {}).get("verdict") == "lower" for m in ("success rate", "render pass rate")
        ):
            continue
        rating = mine.get("owner: mean of five")
        if rating is None:
            continue
        if rating["verdict"] == "higher":
            why, rank = (
                "beats A on the owner's rating (mean of five, a failed build scoring 1) "
                "by more than the noise band",
                1,
            )
        elif rating["verdict"] == "no decision" and any(
            mine.get(m, {}).get("verdict") == "lower"
            for m in ("mean cost (usd), all builds", "median latency (s), all builds")
        ):
            why, rank = "equals A on rating and is cheaper or faster by more than the noise band", 0
        else:
            continue
        found.append((rank, cast(float, rating["diff"]), arm, why))
    if not found:
        return (
            "Pilot verdict: NO-GO. No arm beats A by more than the A to A' difference on the "
            "owner's rating, or on cost per build or latency at an equal rating. Stop. "
            "The binding stays on Sonnet 5."
        )
    found.sort(reverse=True)
    first, rest = found[0], found[1:]
    line = f"Pilot verdict: GO. Arm {first[2]} ({ARMS[first[2]]}) {first[3]}."
    line += "".join(f" Arm {c[2]} ({ARMS[c[2]]}) also {c[3]}." for c in rest)
    return line + (
        " Propose a full run with a larger prompt set and its own preflight. "
        "The owner decides any swap."
    )


def _fmt(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, float) else "" if value is None else str(value)


def _controls(r: Mapping[str, object]) -> str:
    if not r.get("ok"):
        return "no page"
    return " / ".join(
        str(r.get(k, ""))
        for k in (
            "controls_found",
            "controls_worked",
            "controls_no_reaction",
            "controls_unverifiable",
            "widget_errors",
        )
    )


def attach_scores(
    rows: list[JSONDict],
    key: Mapping[str, JSONDict],
    scores: Mapping[str, Mapping[str, int]],
) -> None:
    """Put the owner's scores on the build rows. A failed build scores 1 on every question.

    A failed build has no page to rate. Counting it as the lowest score keeps an arm from winning
    by being judged only on the pages it managed to build.

    Args:
        rows: Build rows. Changed in place.
        key: Label to ``{"arm", "prompt_id"}``.
        scores: Label to the five scores.
    """
    for label, meta in key.items():
        for r in rows:
            if (r["arm"], r["prompt_id"]) == (meta["arm"], meta["prompt_id"]):
                for q in QUESTIONS:
                    r[f"score_{q}"] = scores[label][q]
                r["score"] = statistics.fmean(scores[label][q] for q in QUESTIONS)
    for r in rows:
        if not r.get("ok"):
            for q in QUESTIONS:
                r[f"score_{q}"] = 1
            r["score"] = 1.0


def _report(
    rows: list[JSONDict], key: Mapping[str, JSONDict], scores: Mapping[str, Mapping[str, int]]
) -> str:
    attach_scores(rows, key, scores)
    table = decision_table(rows)
    lines = [
        "# FRE-1668 artifact builder pilot",
        "",
        f"The sample is small: {len(load_prompts())} prompts, one build per arm and prompt. Treat the "
        "result as a direction, not a proof. The noise band is the gap between A and A' on the same prompts.",
        "",
        "Confound: the arms differ in mode as well as in model. Sonnet 5 and Sonnet 5.5 run adaptive "
        "effort `high`. Sol runs reasoning effort `medium`. Each is its catalog default today.",
        "",
        "A failed build has no page to rate. It scores 1 on every question, so an arm is not judged only "
        "on the pages that it managed to build.",
        "",
        "Widget check: a control that shows no reaction may act only on a later step, for example a "
        "name field or a drag switch that applies to the next launch. Read it with the owner's score "
        "for question 1.",
        "",
    ]
    counts_path = OUT_DIR / "prod-counts.json"
    if counts_path.exists():
        lines += [
            "Production counts (AC-6):",
            "```",
            counts_path.read_text(encoding="utf-8"),
            "```",
            "",
        ]
    for arm in ARMS:
        mine = sorted((r for r in rows if r["arm"] == arm), key=lambda r: str(r["prompt_id"]))
        lines += [
            f"## Arm {ARM_LABEL[arm]} — {ARMS[arm]}",
            "",
            "| prompt | ok | reason | render | controls found / worked / no reaction / unverifiable / errors | latency s | in tok | out tok "
            "| cost usd | " + " | ".join(f"Q{i}" for i in range(1, len(QUESTIONS) + 1)) + " |",
            "|---|---|---|---|---|---|---|---|---|" + "---|" * len(QUESTIONS),
        ]
        lines += [
            f"| {r['prompt_id']} | {r['ok']} | {r['failure_reason']} | {r['render_ok']} "
            f"| {_controls(r)} "
            f"| {r['latency_s']} | {r['input_tokens']} | {r['output_tokens']} | {r['cost_usd']:.4f} | "
            + " | ".join(str(r.get(f"score_{q}", "")) for q in QUESTIONS)
            + " |"
            for r in mine
        ]
        cost = [float(cast(float, r["cost_usd"])) for r in mine]
        lines += [
            "",
            f"Cost per build: mean {statistics.fmean(cost):.4f} usd, total {sum(cost):.4f} usd.",
            "",
        ]
    lines += ["## Owner scores by question (mean of the rated pages)", ""]
    lines += ["| question | A | A' | B | C | noise band |", "|---|---|---|---|---|---|"]
    for i, text in enumerate(QUESTIONS.values(), start=1):
        vals = {a: _measure(rows, a, f"owner: {text}") for a in ARMS}
        a, a2 = vals["A"], vals["A2"]
        band = _fmt(abs(a2 - a)) if a is not None and a2 is not None else ""
        lines.append(
            f"| Q{i} {text} | "
            + " | ".join(_fmt(vals[x]) for x in ("A", "A2", "B", "C"))
            + f" | {band} |"
        )
    lines += [
        "",
        "## Decision table",
        "",
        "| measure | arm | A | A' | arm value | diff | noise band | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {r['measure']} | {r['arm']} | {_fmt(r['a'])} | {_fmt(r['a_prime'])} | {_fmt(r['value'])} "
        f"| {_fmt(r['diff'])} | {_fmt(r['noise_band'])} | {r['verdict']} |"
        for r in table
    ]
    lines += _blind_section(rows, key, scores)
    lines += _reasoning_section(rows)
    every = _read_jsonl(OUT_DIR / "builds.jsonl")
    retries = [r for r in every if int(cast(int, r.get("attempt", 1))) > 1]
    spend = sum(float(cast(float, r["cost_usd"])) for r in every)
    lines += [
        f"Total build spend over all attempts: {spend:.4f} usd. Retried attempts: {len(retries)}.",
        "",
    ]
    lines += [
        f"- retry {r['arm']}/{r['prompt_id']} attempt {r['attempt']}: ok={r['ok']} {r['failure_reason']}"
        for r in retries
    ]
    lines += ["", pilot_verdict(table), ""]
    return "\n".join(lines)


def _blind_section(
    rows: Sequence[Mapping[str, object]],
    key: Mapping[str, JSONDict],
    scores: Mapping[str, Mapping[str, int]],
) -> list[str]:
    """Say which ratings were blind and show the blind-only comparison."""
    blind = blind_prompts(rows)
    lines = [
        "## Which ratings were blind",
        "",
        "The owner rated each page without its arm. A prompt is a blind comparison only when two or more "
        "arms built a page for it. Where one arm built the only page, the arm is known from the build "
        "results, so that rating is not blind in effect.",
        "",
        "| label | prompt | arm (unblinded) | blind comparison | "
        + " | ".join(f"Q{i}" for i in range(1, 6))
        + " |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for label in sorted(key):
        meta = key[label]
        lines.append(
            f"| {label} | {meta['prompt_id']} | {ARM_LABEL[str(meta['arm'])]} "
            f"| {'yes' if meta['prompt_id'] in blind else 'no, the only page'} | "
            + " | ".join(str(scores[label][q]) for q in QUESTIONS)
            + " |"
        )
    blind_rows = [r for r in rows if r["prompt_id"] in blind]
    table = [r for r in decision_table(blind_rows) if str(r["measure"]).startswith("owner:")]
    lines += [
        "",
        f"Blind-only comparison (prompts {', '.join(sorted(blind))}, where every page was rated blind). "
        "The noise band is the gap between A and A' on these pages.",
        "",
        "| question | arm | A | A' | arm value | diff | noise band | verdict |",
        "|---|---|---|---|---|---|---|---|",
    ]
    lines += [
        f"| {str(r['measure']).removeprefix('owner: ')} | {r['arm']} | {_fmt(r['a'])} | {_fmt(r['a_prime'])} "
        f"| {_fmt(r['value'])} | {_fmt(r['diff'])} | {_fmt(r['noise_band'])} | {r['verdict']} |"
        for r in table
    ]
    return lines + [""]


def _reasoning_section(rows: Sequence[Mapping[str, object]]) -> list[str]:
    """Show the thinking tokens of each build against the output ceiling."""
    ceiling = int(settings.artifact_draft_max_tokens)
    lines = [
        f"## Thinking tokens against the output ceiling ({ceiling:,} tokens)",
        "",
        "The output ceiling counts thinking tokens and the page together. A build stops at the ceiling "
        "when the thinking and the page need more than it holds. A blank means the trial row did not log "
        "the number (the p9 trial ran before the harness logged it).",
        "",
        "| prompt | arm | outcome | thinking tokens | output tokens | thinking share of output |",
        "|---|---|---|---|---|---|",
    ]
    for r in sorted(rows, key=lambda r: (str(r["prompt_id"]), str(r["arm"]))):
        think, out = r.get("reasoning_tokens"), r.get("output_tokens")
        share = (
            f"{float(cast(float, think)) / float(cast(float, out)):.0%}" if think and out else ""
        )
        outcome = (
            "page built"
            if r.get("ok")
            else "stopped at the ceiling"
            if r.get("output_cap_hit")
            else "failed"
        )
        lines.append(
            f"| {r['prompt_id']} | {ARM_LABEL[str(r['arm'])]} | {outcome} | {think if think is not None else ''} | {out} | {share} |"
        )
    return lines + [""]


# ── commands ──────────────────────────────────────────────────────────────────


def _select(ids: str | None) -> list[Prompt]:
    wanted = set(ids.split(",")) if ids else None
    return [p for p in load_prompts() if wanted is None or p.id in wanted]


def plans_to_run(
    prompts: Sequence[Prompt],
    done: set[str],
    failed: set[str],
    retry_failed: bool,
) -> list[Prompt]:
    """Pick the prompts that still need a plan, and refuse to repeat a failed paid call.

    Args:
        prompts: The selected prompts.
        done: Prompt ids that have a plan.
        failed: Prompt ids whose last plan call failed (the evidence is in ``plan-failures.jsonl``).
        retry_failed: Allow a new call for a prompt in ``failed``.

    Returns:
        The prompts to plan.

    Raises:
        IncompleteRunError: A selected prompt failed before and ``retry_failed`` is off.
    """
    todo = [p for p in prompts if p.id not in done]
    again = sorted(p.id for p in todo if p.id in failed)
    if again and not retry_failed:
        raise IncompleteRunError(
            f"the plan call failed before for {again}: read plan-failures.jsonl, fix the cause, "
            "then pass --retry-failed. A repeat call costs money."
        )
    return todo


async def _cmd_plans(ids: str | None, per_plan: Decimal, retry_failed: bool = False) -> int:
    assert_isolated()
    done = {str(r["prompt_id"]) for r in _read_jsonl(OUT_DIR / "plans.jsonl")}
    failed = {str(r["prompt_id"]) for r in _read_jsonl(OUT_DIR / "plan-failures.jsonl")}
    todo = plans_to_run(_select(ids), done, failed, retry_failed)
    budget = await _gate_and_headroom(len(todo), 0, per_plan=per_plan)
    try:
        for prompt in todo:
            try:
                plan = await make_plan(prompt)
            except Exception as exc:  # noqa: BLE001 — a credit error stops the run, anything else is raised
                if is_vendor_credit_error(str(exc)) or "BudgetDenied" in str(exc):
                    print(
                        f"STOP: a vendor credit, quota or budget error ({type(exc).__name__}). No retry."
                    )  # noqa: T201
                    return 3
                raise
            _append_jsonl(OUT_DIR / "plans.jsonl", plan.__dict__)
            print(f"{prompt.id} plan: {len(plan.plan)} chars, {plan.cost_usd:.4f} usd")  # noqa: T201
    finally:
        await budget.gate.disconnect()
    return 0


async def _cmd_build(ids: str | None, per_build: Decimal, retry_failed: bool = False) -> int:
    assert_isolated()
    _assert_render_tools()
    catalog = load_model_config()
    resolve_arms(catalog, is_provider_available=_availability(catalog))
    plans = {
        str(r["prompt_id"]): Plan(
            prompt_id=str(r["prompt_id"]),
            slug=str(r["slug"]),
            title=str(r["title"]),
            summary=str(r["summary"]),
            plan=str(r["plan"]),
        )
        for r in _read_jsonl(OUT_DIR / "plans.jsonl")
    }
    prompts = [p for p in _select(ids) if p.id in plans]
    if len(prompts) != len(_select(ids)):
        raise IncompleteRunError("run `plans` first: some selected prompts have no plan")
    # The first attempt of a build is the scored one. A rerun never replaces it (best-of-N would
    # flatter an arm). Only --retry-failed runs a build again, and only if no attempt succeeded.
    tries: dict[tuple[str, str], list[JSONDict]] = {}
    for r in _read_jsonl(OUT_DIR / "builds.jsonl"):
        tries.setdefault((str(r["arm"]), str(r["prompt_id"])), []).append(r)
    jobs = [
        (plans[p.id], arm, len(tries.get((arm, p.id), [])) + 1)
        for i, p in enumerate(prompts)
        for arm in (list(ARMS)[i % 4 :] + list(ARMS)[: i % 4])  # alternate the arm order per prompt
        if (arm, p.id) not in tries
        or (retry_failed and not any(r["ok"] for r in tries[(arm, p.id)]))
    ]
    budget = await _gate_and_headroom(0, len(jobs), per_build=per_build)
    try:
        for plan, arm, attempt in jobs:
            now = datetime.now(timezone.utc)
            own_now = own_spend(
                _read_jsonl(OUT_DIR / "builds.jsonl"), _read_jsonl(OUT_DIR / "plans.jsonl"), now
            )
            try:
                check_headroom(
                    budget.caps,
                    running_spend(budget.start, budget.own_start, own_now),
                    {"artifact_builder": per_build},
                )
            except BudgetShortfallError as exc:
                print(
                    f"STOP before {plan.prompt_id} {ARM_LABEL[arm]}: {exc}. Resume after the window resets."
                )  # noqa: T201
                return 3
            row = await build_one(plan, arm, attempt=attempt)
            if not row["ok"] and is_infrastructure_stop(str(row["failure_reason"])):
                # No model output exists, so this is not a scored failure. Keep the evidence.
                _append_jsonl(OUT_DIR / "stops.jsonl", row)
                print(  # noqa: T201
                    "STOP: a vendor credit, quota or budget error. No retry. Not scored. "
                    "Report it to master and the owner."
                )
                return 3
            _append_jsonl(OUT_DIR / "builds.jsonl", row)
            print(  # noqa: T201
                f"{plan.prompt_id} {ARM_LABEL[arm]} ok={row['ok']} render={row['render_ok']} "
                f"{row['latency_s']}s {row['cost_usd']:.4f} usd {row['failure_reason']}"
            )
    finally:
        await budget.gate.disconnect()
    return 0


def _assert_render_tools() -> None:
    """Stop before the first paid call if the render check or the /lib/ host cannot work."""
    if shutil.which("node") is None or not (REPO / "seshat-pwa/node_modules/playwright").exists():
        raise IncompleteRunError(
            "the render check needs node and seshat-pwa/node_modules/playwright (run npm ci there)"
        )
    if not settings.artifacts_public_base_url:
        raise IncompleteRunError(
            "AGENT_ARTIFACTS_PUBLIC_BASE_URL is unset: the builder prompt would name a placeholder "
            "/lib/ host and every chart page would fail to render on every arm"
        )


def _availability(catalog: ModelConfig) -> Callable[[str], bool]:
    from personal_agent.orchestrator.constraint_options import (  # noqa: PLC0415
        build_provider_availability,
    )

    return build_provider_availability(catalog, settings)


def _scored_builds() -> list[JSONDict]:
    """Return the first attempt of each (arm, prompt). Later attempts never replace it."""
    first: dict[tuple[str, str], JSONDict] = {}
    for r in _read_jsonl(OUT_DIR / "builds.jsonl"):
        first.setdefault((str(r["arm"]), str(r["prompt_id"])), r)
    return list(first.values())


def _cmd_rerender() -> int:
    """Re-run the render check on every saved page. Free; the model output is unchanged."""
    path = OUT_DIR / "builds.jsonl"
    rows = _read_jsonl(path)
    for row in rows:
        if row.get("ok") and row.get("html_path"):
            row.update(render_fields(Path(str(row["html_path"]))))
            print(f"{row['prompt_id']} {ARM_LABEL[str(row['arm'])]} render_ok={row['render_ok']}")  # noqa: T201
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")
    return 0


def _cmd_bundle(force: bool = False) -> int:
    rows = _scored_builds()
    check_complete(rows, {}, {}, n_prompts=len(load_prompts()), need_scores=False)
    if (OUT_DIR / "scores.json").exists() and not force:
        raise IncompleteRunError(
            "scores.json exists: a new bundle would relabel the pages and make the scores stale. "
            "Pass --force only to start a new rating."
        )
    requests = {p.id: p.request for p in load_prompts()}
    entries = json.loads(LIB_MAP.read_text(encoding="utf-8"))["entries"]
    origin = settings.artifacts_public_base_url or ""
    key = write_rating_pack(
        rows,
        OUT_DIR / "rating",
        OUT_DIR / "key.json",
        requests,
        transform=lambda page: lib_to_cdn(page, origin, entries),
    )
    print(f"{len(key)} pages in {OUT_DIR / 'rating'}; key.json stays in {OUT_DIR}")  # noqa: T201
    return 0


def _cmd_report() -> int:
    rows = _scored_builds()
    key = json.loads((OUT_DIR / "key.json").read_text(encoding="utf-8"))
    scores = json.loads((OUT_DIR / "scores.json").read_text(encoding="utf-8"))
    check_complete(rows, key, scores, n_prompts=len(load_prompts()))
    text = _report(rows, key, scores)
    (OUT_DIR / "report.md").write_text(text, encoding="utf-8")
    print(text)  # noqa: T201
    return 0


def _cmd_prod_count(label: str) -> int:
    path = OUT_DIR / "prod-counts.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[label] = prod_counts()
    if label == "after":
        ids = [
            str(r["trace_id"]) for r in _read_jsonl(OUT_DIR / "builds.jsonl") if r.get("trace_id")
        ]
        ids += [
            str(r["trace_id"]) for r in _read_jsonl(OUT_DIR / "plans.jsonl") if r.get("trace_id")
        ]
        data["trace_ids_checked"] = len(ids)
        data["rows_for_these_trace_ids"] = {
            "production": trace_hits(PROD_PG_CONTAINER, ids),
            "eval": trace_hits(EVAL_PG_CONTAINER, ids),
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(json.dumps(data[label]))  # noqa: T201
    if label == "after":
        print(json.dumps(data["rows_for_these_trace_ids"]))  # noqa: T201
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one subcommand.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("plans", "build"):
        sub.add_parser(name).add_argument("--ids", help="comma-separated prompt ids (default: all)")
    sub.choices["plans"].add_argument(
        "--retry-failed", action="store_true", help="plan again a prompt whose plan call failed"
    )
    sub.choices["plans"].add_argument(
        "--usd-per-plan", type=Decimal, default=WORST_CASE_USD_PER_PLAN,
        help="projected cost of one plan call for the budget check (default: worst case)",
    )  # fmt: skip
    sub.choices["build"].add_argument(
        "--usd-per-build", type=Decimal, default=WORST_CASE_USD_PER_BUILD,
        help="projected cost of one build for the budget check (default: worst case)",
    )  # fmt: skip
    sub.choices["build"].add_argument(
        "--retry-failed",
        action="store_true",
        help="run again the builds with no successful attempt",
    )
    sub.add_parser("rerender")
    sub.add_parser("bundle").add_argument("--force", action="store_true")
    sub.add_parser("report")
    sub.add_parser("prod-count").add_argument("--label", choices=("before", "after"), required=True)
    args = parser.parse_args(argv)
    if args.cmd == "plans":
        return asyncio.run(_cmd_plans(args.ids, args.usd_per_plan, args.retry_failed))
    if args.cmd == "build":
        return asyncio.run(_cmd_build(args.ids, args.usd_per_build, args.retry_failed))
    if args.cmd == "rerender":
        return _cmd_rerender()
    if args.cmd == "bundle":
        return _cmd_bundle(args.force)
    if args.cmd == "report":
        return _cmd_report()
    return _cmd_prod_count(args.label)


if __name__ == "__main__":
    sys.exit(main())
