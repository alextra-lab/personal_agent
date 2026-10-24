"""FRE-1668: artifact builder side-by-side, claude_sonnet against claude_sonnet_5_5 and gpt-6.1-sol.

The owner asked on 2026-10-24 for a test of the artifact building of Sol and Sonnet 5.5. The
ticket asks one question: must the ``artifact_builder`` role move from ``claude_sonnet`` to
``claude_sonnet_5_5`` or to ``gpt-6.1-sol``? This harness reports a result. It changes no binding.

Arms: ``A`` claude_sonnet (baseline) · ``A2`` claude_sonnet again (noise) · ``B`` claude_sonnet_5_5 ·
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
import shutil
import statistics
import subprocess  # nosec B404 — fixed argv, no shell
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import cast
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
SUITE = HERE.parent / "fre1615" / "suite.yaml"

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
EVAL_DB = (
    "127.0.0.1",
    5434,
)  # fre-375-allow: the eval Postgres, the only database this harness may use
DB_BINDINGS = ("settings", "cost_tracker", "engine")
PROD_PG_CONTAINER = "cloud-sim-postgres"
PLAN_MAX_TOKENS = 6000
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
    """Read the 8 prompts. Entries with ``from_fre1615`` take their text from that suite.

    Returns:
        The prompts in file order.
    """
    spec = yaml.safe_load((HERE / "prompts.yaml").read_text(encoding="utf-8"))["prompts"]
    suite = {
        t["id"]: t["message"] for t in yaml.safe_load(SUITE.read_text(encoding="utf-8"))["tasks"]
    }
    return [
        Prompt(
            id=p["id"],
            category=p["category"],
            request=suite[p["from_fre1615"]] if "from_fre1615" in p else p["request"].strip(),
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


def prod_counts() -> dict[str, dict[str, object]]:
    """Read production row counts. Read only; the credentials stay inside the container.

    Returns:
        Table name to ``{"count": n, "newest": timestamp}``.
    """
    # fre-375-allow: a read-only SELECT on production is the AC-6 proof
    proc = subprocess.run(  # nosec B603 B607 — fixed argv, no user input
        [
            "docker", "exec", "-i", PROD_PG_CONTAINER, "sh", "-c",
            'psql -U "$POSTGRES_USER" -d "$POSTGRES_DB" -At -F "|"',
        ],
        input=_PROD_SQL, capture_output=True, text=True, timeout=60, check=True,
    )  # fmt: skip
    out: dict[str, dict[str, object]] = {}
    for line in proc.stdout.splitlines():
        table, count, newest = line.split("|")
        out[table] = {"count": int(count), "newest": newest}
    return out


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


async def _gate_and_headroom(n_plans: int, n_builds: int) -> object:
    """Connect the cost gate to the eval Postgres and check the caps. Returns the gate."""
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
    from datetime import datetime, timezone  # noqa: PLC0415

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
    projected = {
        "artifact_builder": WORST_CASE_USD_PER_BUILD * n_builds,
        "study": WORST_CASE_USD_PER_PLAN * n_plans,
    }
    try:
        check_headroom(caps, spent, projected)
    except BudgetShortfallError:
        await gate.disconnect()
        set_default_gate(None)
        raise
    return gate


# ── plans ─────────────────────────────────────────────────────────────────────

_PLAN_SYSTEM = (
    "You are the planning model of a personal assistant. The user asked for a web page. Call the "
    "artifact_draft tool once. Give it a slug, a title, a one-sentence summary and a structured plan "
    "(sections, all data and content, style guidance). Do not write HTML."
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
    resp = await client.respond(
        role=ModelRole.PRIMARY,
        messages=[{"role": "user", "content": user}],
        system_prompt=_PLAN_SYSTEM,
        tools=[draft_tool_schema()],
        max_tokens=PLAN_MAX_TOKENS,
        trace_ctx=SystemTraceContext.new("fre1668_plan"),
    )
    calls = [c for c in resp.get("tool_calls") or [] if c["name"] == "artifact_draft"]
    if not calls:
        raise RuntimeError(f"{prompt.id}: the planning model did not call artifact_draft")
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
            ["node", str(RENDER_JS), str(path), origin],
            capture_output=True, text=True, timeout=120, check=False,
        )  # fmt: skip
        result = cast(JSONDict, json.loads(proc.stdout.strip().splitlines()[-1]))
    except (subprocess.TimeoutExpired, OSError, IndexError, json.JSONDecodeError) as exc:
        result = {"page_errors": [f"render check crashed: {type(exc).__name__}: {exc}"[:300]]}
    for field in ("page_errors", "console_errors", "failed_requests", "blocked_requests"):
        result.setdefault(field, [])
    result.setdefault("blank", False)
    result["render_ok"] = not (
        result["page_errors"] or result["console_errors"] or result["failed_requests"]
        or result["blocked_requests"] or result["blank"]
    )  # fmt: skip
    return result


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
                ctx=SystemTraceContext.new("fre1668_build"),
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
    ok = not failure
    row: JSONDict = {
        "arm": arm,
        "prompt_id": plan.prompt_id,
        "requested_key": key,
        "resolved_key": resolved,
        "served_model": served,
        "effort": str(done[0].get("mode", "")) if done else "",
        "attempt": attempt,
        "ok": ok,
        "failure_reason": failure,
        "render_ok": None,
        "latency_s": round(latency, 2),
        "input_tokens": done[0].get("input_tokens") if done else None,
        "output_tokens": done[0].get("output_tokens") if done else None,
        "cost_usd": float(cast(float, done[0].get("cost_usd") or 0.0)) if done else 0.0,
        "html_bytes": saved.get("bytes", 0),
        "output_cap_hit": any(e.get("event") == "artifact_draft_output_cap_hit" for e in logs),
        "html_path": str(html_path) if "bytes" in saved else "",
    }
    if ok and not skip_render:
        check = render_html(html_path)
        row.update(
            render_ok=check["render_ok"],
            console_errors=len(cast(list[str], check["console_errors"])),
            page_errors=len(cast(list[str], check["page_errors"])),
            failed_requests=len(cast(list[str], check["failed_requests"])),
            blocked_requests=len(cast(list[str], check["blocked_requests"])),
            blank=check["blank"],
        )
    elif ok:
        row["render_ok"] = True
    return row


# ── rating pack and report ────────────────────────────────────────────────────


def write_rating_pack(
    rows: Sequence[Mapping[str, object]],
    rating_dir: Path,
    key_path: Path,
    requests: Mapping[str, str] | None = None,
) -> dict[str, JSONDict]:
    """Write the blind pack for the owner and the key that stays with the run.

    Args:
        rows: Build rows. Only rows with ``ok`` and an ``html_path`` are rated.
        rating_dir: The directory handed to the owner. It receives ``index.html`` only.
        key_path: Where the label key goes. Must lie outside ``rating_dir``.
        requests: Prompt id to request text, shown beside each page.

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
        page = Path(str(row["html_path"])).read_text(encoding="utf-8")
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
        "iframe{width:100%;height:36rem;border:1px solid #888;background:#fff}"
        ".ask{color:#555}section{margin-bottom:2.5rem}</style></head><body>"
        "<h1>Rate each page from 1 (poor) to 5 (excellent)</h1>"
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
        scores: The owner's score per label.
        n_prompts: Prompts in the set.
        need_scores: Require one valid 1 to 5 score per label.

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
        bad = [
            k for k in labels
            if not isinstance(scores.get(k), int) or isinstance(scores.get(k), bool)
            or not 1 <= cast(int, scores[k]) <= 5
        ]  # fmt: skip
        if bad or set(scores) != set(labels):
            raise IncompleteRunError(f"scores missing or invalid for labels: {sorted(bad)}")


def _measure(rows: Sequence[Mapping[str, object]], arm: str, measure: str) -> float | None:
    mine = [r for r in rows if r["arm"] == arm]
    ok = [r for r in mine if r.get("ok")]
    if measure == "success rate":
        return sum(1 for r in mine if r.get("ok")) / len(mine) if mine else None
    if measure == "render pass rate":
        return sum(1 for r in mine if r.get("render_ok")) / len(mine) if mine else None
    column = {
        "median latency (s)": "latency_s",
        "median output tokens": "output_tokens",
        "mean cost (usd)": "cost_usd",
        "mean owner score": "score",
    }[measure]
    values = [float(cast(float, r[column])) for r in ok if r.get(column) is not None]
    if not values:
        return None
    return statistics.median(values) if measure.startswith("median") else statistics.fmean(values)


MEASURES = (
    "success rate",
    "render pass rate",
    "median latency (s)",
    "median output tokens",
    "mean cost (usd)",
    "mean owner score",
)


def decision_table(rows: Sequence[Mapping[str, object]]) -> list[JSONDict]:
    """Compare B and C with A, against the noise band ``abs(A' - A)`` (AC-2).

    Args:
        rows: Build rows. A ``score`` field is optional.

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


def recommend(table: Sequence[Mapping[str, object]]) -> str:
    """Return the one recommendation line, from the owner score first.

    Args:
        table: The output of ``decision_table``.

    Returns:
        A line that names an arm only when its owner score beat the noise band without a loss on
        success or render. Otherwise the line says "no decision".
    """
    best: tuple[float, str] | None = None
    for arm in ("B", "C"):
        mine = {str(r["measure"]): r for r in table if r["arm"] == arm}
        score = mine.get("mean owner score")
        if not score or score["verdict"] != "higher":
            continue
        if any(
            mine.get(m, {}).get("verdict") == "lower" for m in ("success rate", "render pass rate")
        ):
            continue
        if best is None or cast(float, score["diff"]) > best[0]:
            best = (cast(float, score["diff"]), arm)
    if best is None:
        return "Recommendation: no decision. No arm beat the baseline by more than the noise band without losing on success or render."
    return f"Recommendation: arm {best[1]} ({ARMS[best[1]]}) beat the baseline on owner score by more than the noise band. The owner decides any swap."


def _fmt(value: object) -> str:
    return f"{value:.3f}" if isinstance(value, float) else str(value)


def _report(rows: list[JSONDict], key: Mapping[str, JSONDict], scores: Mapping[str, int]) -> str:
    for label, meta in key.items():
        for r in rows:
            if (r["arm"], r["prompt_id"]) == (meta["arm"], meta["prompt_id"]):
                r["score"] = scores[label]
    table = decision_table(rows)
    lines = [
        "# FRE-1668 artifact builder side-by-side",
        "",
        "Confound: the arms differ in mode as well as in model. Sonnet 5 and Sonnet 5.5 run adaptive "
        "effort `high`. Sol runs reasoning effort `medium`. Each is its catalog default today.",
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
            "| prompt | ok | reason | render | latency s | in tok | out tok | cost usd | score |",
            "|---|---|---|---|---|---|---|---|---|",
        ]
        lines += [
            f"| {r['prompt_id']} | {r['ok']} | {r['failure_reason']} | {r['render_ok']} | {r['latency_s']} "
            f"| {r['input_tokens']} | {r['output_tokens']} | {r['cost_usd']:.4f} | {r.get('score', '')} |"
            for r in mine
        ]
        cost = [float(cast(float, r["cost_usd"])) for r in mine]
        lines += [
            "",
            f"Cost per build: mean {statistics.fmean(cost):.4f} usd, total {sum(cost):.4f} usd.",
            "",
        ]
    lines += [
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
    lines += ["", recommend(table), ""]
    return "\n".join(lines)


# ── commands ──────────────────────────────────────────────────────────────────


def _select(ids: str | None) -> list[Prompt]:
    wanted = set(ids.split(",")) if ids else None
    return [p for p in load_prompts() if wanted is None or p.id in wanted]


async def _cmd_plans(ids: str | None) -> int:
    assert_isolated()
    done = {str(r["prompt_id"]) for r in _read_jsonl(OUT_DIR / "plans.jsonl")}
    todo = [p for p in _select(ids) if p.id not in done]
    gate = await _gate_and_headroom(len(todo), 0)
    try:
        for prompt in todo:
            plan = await make_plan(prompt)
            _append_jsonl(OUT_DIR / "plans.jsonl", plan.__dict__)
            print(f"{prompt.id} plan: {len(plan.plan)} chars, {plan.cost_usd:.4f} usd")  # noqa: T201
    finally:
        await gate.disconnect()  # type: ignore[attr-defined]
    return 0


async def _cmd_build(ids: str | None, retry_failed: bool = False) -> int:
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
    gate = await _gate_and_headroom(0, len(jobs))
    try:
        for plan, arm, attempt in jobs:
            row = await build_one(plan, arm, attempt=attempt)
            _append_jsonl(OUT_DIR / "builds.jsonl", row)
            print(  # noqa: T201
                f"{plan.prompt_id} {ARM_LABEL[arm]} ok={row['ok']} render={row['render_ok']} "
                f"{row['latency_s']}s {row['cost_usd']:.4f} usd {row['failure_reason']}"
            )
    finally:
        await gate.disconnect()  # type: ignore[attr-defined]
    return 0


def _assert_render_tools() -> None:
    """Stop before the first paid call if the render check cannot run."""
    if shutil.which("node") is None or not (REPO / "seshat-pwa/node_modules/playwright").exists():
        raise IncompleteRunError(
            "the render check needs node and seshat-pwa/node_modules/playwright (run npm ci there)"
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


def _cmd_bundle(force: bool = False) -> int:
    rows = _scored_builds()
    check_complete(rows, {}, {}, n_prompts=len(load_prompts()), need_scores=False)
    if (OUT_DIR / "scores.json").exists() and not force:
        raise IncompleteRunError(
            "scores.json exists: a new bundle would relabel the pages and make the scores stale. "
            "Pass --force only to start a new rating."
        )
    requests = {p.id: p.request for p in load_prompts()}
    key = write_rating_pack(rows, OUT_DIR / "rating", OUT_DIR / "key.json", requests)
    print(f"{len(key)} pages in {OUT_DIR / 'rating'}; key.json stays in {OUT_DIR}")  # noqa: T201
    return 0


def _cmd_report() -> int:
    rows = _scored_builds()
    key = json.loads((OUT_DIR / "key.json").read_text(encoding="utf-8"))
    scores = json.loads((OUT_DIR / "scores.json").read_text(encoding="utf-8"))
    check_complete(rows, key, scores, n_prompts=len(load_prompts()))
    text = _report(rows, key, scores)
    every = _read_jsonl(OUT_DIR / "builds.jsonl")
    retries = [r for r in every if int(cast(int, r.get("attempt", 1))) > 1]
    spend = sum(float(cast(float, r["cost_usd"])) for r in every)
    text += f"\nTotal spend over all attempts: {spend:.4f} usd. Retried attempts: {len(retries)}.\n"
    text += "".join(
        f"- retry {r['arm']}/{r['prompt_id']} attempt {r['attempt']}: ok={r['ok']} {r['failure_reason']}\n"
        for r in retries
    )
    (OUT_DIR / "report.md").write_text(text, encoding="utf-8")
    print(text)  # noqa: T201
    return 0


def _cmd_prod_count(label: str) -> int:
    path = OUT_DIR / "prod-counts.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data[label] = prod_counts()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=1), encoding="utf-8")
    print(json.dumps(data[label]))  # noqa: T201
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
    sub.choices["build"].add_argument(
        "--retry-failed",
        action="store_true",
        help="run again the builds with no successful attempt",
    )
    sub.add_parser("bundle").add_argument("--force", action="store_true")
    sub.add_parser("report")
    sub.add_parser("prod-count").add_argument("--label", choices=("before", "after"), required=True)
    args = parser.parse_args(argv)
    if args.cmd == "plans":
        return asyncio.run(_cmd_plans(args.ids))
    if args.cmd == "build":
        return asyncio.run(_cmd_build(args.ids, args.retry_failed))
    if args.cmd == "bundle":
        return _cmd_bundle(args.force)
    if args.cmd == "report":
        return _cmd_report()
    return _cmd_prod_count(args.label)


if __name__ == "__main__":
    sys.exit(main())
