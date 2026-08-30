r"""CLI orchestrator — runs arms 1+2 (always) and arm 3 (optional) across the fixture set.

Usage::

    # Arms 1+2 only (deterministic + probe) — no infra needed beyond network access to
    # the three model deployments and the FRE-375 TEST substrate's cost ledger.
    uv run python -m scripts.eval.fre1337_intent_probe.harness --run-id 2026-08-30

    # + arm 3 (behavioral), against the isolated eval gateway — start it first:
    #   make eval-infra-up
    uv run python -m scripts.eval.fre1337_intent_probe.harness --run-id 2026-08-30 \\
        --behavioral

Fails loudly (non-zero exit) rather than silently under-reporting:
- AC-1: any of the 3 models erroring on any fixture aborts the run.
- AC-5: if no model's confusion matrix has a diagonal cell (deterministic == model
  classification) anywhere, the seeded-agreement claim has no live evidence and the run
  aborts.
"""

from __future__ import annotations

import os

# FRE-375: point the cost substrate at the TEST stack BEFORE importing any personal_agent
# code — `settings` is a cached import-time singleton, so assigning later is too late
# (fre1286_entailment/harness.py's pattern). `probe.py`'s import below reaches
# `personal_agent.config` transitively, so this must run first. Harmless when pytest
# already set these (tests/conftest.py sets the identical values before collection —
# `setdefault` is then a no-op).
os.environ.setdefault("APP_ENV", "test")
os.environ.setdefault(
    "AGENT_DATABASE_URL",
    "postgresql+asyncpg://seshat_app:seshat_app_dev_password@localhost:5433/personal_agent",
)
os.environ.setdefault(
    "AGENT_DATABASE_ADMIN_URL",
    "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent",
)
os.environ.setdefault(
    "AGENT_SYSGRAPH_DATABASE_URL",
    "postgresql+asyncpg://sysgraph_role:sysgraph_dev_password@localhost:5433/personal_agent",
)
# AppConfig's TEST-environment validator (settings.py) refuses ANY prod/dev-default
# substrate URI, not just Postgres — this arm never touches Neo4j/ES, but the values
# must still resolve to the test stack for the process to start at all.
os.environ.setdefault("AGENT_NEO4J_URI", "bolt://localhost:7688")
os.environ.setdefault("AGENT_ELASTICSEARCH_URL", "http://localhost:9201")

import json  # noqa: E402
from collections import Counter  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402
from typing import TYPE_CHECKING, Any  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre1337_intent_probe.fixtures import Fixture, load_fixtures  # noqa: E402
from scripts.eval.fre1337_intent_probe.probe import MODEL_KEYS, ModelClassification  # noqa: E402

if TYPE_CHECKING:
    import argparse

    from personal_agent.request_gateway.types import IntentResult

log = structlog.get_logger(__name__)


@dataclass(frozen=True)
class ProbeRow:
    """One (fixture, model) evidence row — arms 1+2 for a single pair.

    Attributes:
        fixture_label: The fixture's label.
        model_key: The catalog deployment key probed.
        deterministic_task_type: Stage 4's answer (arm 1).
        deterministic_confidence: Stage 4's confidence.
        deterministic_signals: Stage 4's matched pattern names.
        model_task_type: The model's answer (arm 2).
        model_reason: The model's stated reason.
        prompt: The verbatim probe prompt sent (AC-2).
        resolved_model_id: The backend-echoed model id, for the identity check.
        requested_model_id: The catalog id that was requested.
    """

    fixture_label: str
    model_key: str
    deterministic_task_type: str
    deterministic_confidence: float
    deterministic_signals: list[str]
    model_task_type: str
    model_reason: str
    prompt: str
    resolved_model_id: str | None
    requested_model_id: str


def build_confusion_matrix(rows: list[ProbeRow]) -> dict[str, Counter[tuple[str, str]]]:
    """Build a per-model confusion matrix (AC-1: "populated from real runs").

    Args:
        rows: Evidence rows, arms 1+2.

    Returns:
        ``{model_key: Counter({(deterministic_type, model_type): count})}``.
    """
    matrix: dict[str, Counter[tuple[str, str]]] = {}
    for row in rows:
        matrix.setdefault(row.model_key, Counter())[
            (row.deterministic_task_type, row.model_task_type)
        ] += 1
    return matrix


def render_confusion_markdown(matrix: dict[str, Counter[tuple[str, str]]]) -> str:
    """Render the confusion matrix as one markdown table per model."""
    lines: list[str] = []
    for model_key, counts in matrix.items():
        lines.append(f"## {model_key}")
        lines.append("")
        lines.append("| deterministic | model | count |")
        lines.append("|---|---|---|")
        for (det, model), count in sorted(counts.items()):
            marker = " (agree)" if det == model else ""
            lines.append(f"| {det} | {model}{marker} | {count} |")
        lines.append("")
    return "\n".join(lines)


def has_seeded_agreement(matrix: dict[str, Counter[tuple[str, str]]]) -> bool:
    """AC-5's live gate: does any model have a real diagonal cell?"""
    return any(det == model for counts in matrix.values() for (det, model) in counts)


def _classify_deterministic(message: str) -> "IntentResult":
    from personal_agent.request_gateway.intent import classify_intent

    return classify_intent(message)


async def _run_probe_arm(fixtures: list[Fixture], model_keys: tuple[str, ...]) -> list[ProbeRow]:
    """Arms 1+2 for every (fixture, model) pair. Raises on any model error (AC-1)."""
    from scripts.eval.fre1337_intent_probe.probe import classify_with_model

    rows: list[ProbeRow] = []
    for fixture in fixtures:
        deterministic = _classify_deterministic(fixture.message)
        for model_key in model_keys:
            classification: ModelClassification = await classify_with_model(
                model_key, fixture.message
            )
            rows.append(
                ProbeRow(
                    fixture_label=fixture.label,
                    model_key=model_key,
                    deterministic_task_type=deterministic.task_type.value,
                    deterministic_confidence=deterministic.confidence,
                    deterministic_signals=list(deterministic.signals),
                    model_task_type=classification.task_type,
                    model_reason=classification.reason,
                    prompt=classification.prompt,
                    resolved_model_id=classification.resolved_model_id,
                    requested_model_id=classification.requested_model_id,
                )
            )
            log.info(
                "fre1337_row",
                fixture=fixture.label,
                model=model_key,
                deterministic=deterministic.task_type.value,
                model_answer=classification.task_type,
            )
    return rows


async def amain(args: "argparse.Namespace") -> int:
    """Drive the fixture set through arms 1+2 (+ optional arm 3) and write the report."""
    fixtures = load_fixtures()
    model_keys = tuple(args.models.split(",")) if args.models else MODEL_KEYS

    rows = await _run_probe_arm(fixtures, model_keys)

    missing_models = set(model_keys) - {r.model_key for r in rows}
    if missing_models:
        log.error("fre1337_ac1_violation_missing_models", missing=sorted(missing_models))
        return 2

    matrix = build_confusion_matrix(rows)
    if not has_seeded_agreement(matrix):
        log.error("fre1337_ac5_violation_no_diagonal_cell")
        return 3

    behavioral_reports: list[dict[str, Any]] = []
    if args.behavioral:
        from scripts.eval.fre1337_intent_probe.behavioral import run_behavioral_arm

        behavioral_reports = await run_behavioral_arm(fixtures)

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    run_meta = {
        "run_id": args.run_id,
        "models": list(model_keys),
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "behavioral_arm_run": args.behavioral,
    }
    stem = f"{args.run_id}"
    (out_dir / f"{stem}.json").write_text(
        json.dumps(
            {
                "meta": run_meta,
                "rows": [asdict(r) for r in rows],
                "behavioral": behavioral_reports,
            },
            indent=2,
        )
    )
    (out_dir / f"{stem}.md").write_text(
        f"# FRE-1337 intent-classification probe — {args.run_id}\n\n"
        + render_confusion_markdown(matrix)
    )
    log.info("fre1337_pass_written", out=str(out_dir / f"{stem}.md"), rows=len(rows))
    return 0


def main() -> int:
    """CLI entry point.

    Registers a CostGate against the FRE-375 TEST substrate (redirected at module import
    time, above) — a standalone script has no application startup to have registered one,
    so every paid probe call would otherwise refuse (fre1286 pattern).
    """
    import argparse
    import asyncio

    p = argparse.ArgumentParser(description="FRE-1337 intent-classification probe harness")
    p.add_argument("--run-id", required=True, help="Run identifier (tag in output).")
    p.add_argument(
        "--models",
        default=None,
        help="Comma-separated model keys (default: all 3 — qwen local, qwen ovh, sonnet).",
    )
    p.add_argument(
        "--behavioral",
        action="store_true",
        help="Also run arm 3 against the isolated eval gateway (needs `make eval-infra-up`).",
    )
    p.add_argument(
        "--out", default="telemetry/evaluation/fre1337-intent-probe", help="Output directory."
    )
    args = p.parse_args()

    async def _amain_with_gate() -> int:
        from personal_agent.config import settings
        from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

        gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
        await gate.connect()
        set_default_gate(gate)
        return await amain(args)

    return asyncio.run(_amain_with_gate())


if __name__ == "__main__":
    raise SystemExit(main())
