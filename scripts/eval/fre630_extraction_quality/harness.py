r"""FRE-630 — pre-write extraction-quality harness (ADR-0087 measurement-first).

Drives the curated gold set through the **real extractor**
(``second_brain.entity_extraction.extract_entities_and_relationships``) and scores
its *output dict* against the gold labels with the pure metric core. Unlike the
FRE-435 recall harness it writes nothing to Neo4j — extraction quality is a property
of the extractor's output, so no graph substrate is needed. It does route through
LiteLLM + the ADR-0065 cost gate (the extractor always does), so it points the cost
substrate at the **test stack** (FRE-375: Postgres :5433) — benchmark runs never
touch prod cost records.

Determinism (codex P1.4): a stochastic LLM makes a single call per case unreliable
for A/B, so ``--samples N`` runs each case N times and the report carries mean±std
stability bands. Every run stamps the extractor model, provider, prompt hash, git
commit, and matcher/gold-schema versions so scores are never misread across a
different model/prompt/scoring revision.

Usage::

    make test-infra-up            # isolated cost substrate (Postgres :5433)
    # run as a MODULE (-m) so ``scripts`` resolves as a package:
    uv run python -m scripts.eval.fre630_extraction_quality.harness \\
        --run-id smoke-2026-07-03 --limit 1            # AC-3 smoke (1 case)
    uv run python -m scripts.eval.fre630_extraction_quality.harness \\
        --run-id baseline-2026-07-03 --samples 3       # AC-5 baseline (all cases)

Raw run dumps stay out of git; output lands in the gitignored
``telemetry/evaluation/fre630-extraction-quality/`` directory.
"""

from __future__ import annotations

import os

# FRE-375: point the cost substrate at the TEST stack BEFORE importing any
# personal_agent code (``settings`` is a cached import-time singleton). The
# extractor model is resolved from the cloud config (gpt-5.4-mini, the prod
# extractor). ``setdefault`` lets the caller pre-override any of these.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_NEO4J_URI": "bolt://localhost:7688",
    "AGENT_ELASTICSEARCH_URL": "http://localhost:9201",
    "AGENT_DATABASE_URL": (
        "postgresql+asyncpg://agent:agent_dev_password@localhost:5433/personal_agent"
    ),
    "AGENT_ELASTICSEARCH_INDEX_PREFIX": "agent-logs-test",
    "AGENT_CAPTAINS_LOG_INDEX_PREFIX": "agent-captains-test",
}
for _key, _value in _TEST_SUBSTRATE_ENV.items():
    os.environ.setdefault(_key, _value)

import argparse  # noqa: E402
import asyncio  # noqa: E402
import hashlib  # noqa: E402
import subprocess  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre630_extraction_quality.gold import (  # noqa: E402
    GOLD_SCHEMA_VERSION,
    GoldCase,
    load_gold_set,
)
from scripts.eval.fre630_extraction_quality.matching import (  # noqa: E402
    DEFAULT_FUZZY_THRESHOLD,
    MATCHER_VERSION,
)
from scripts.eval.fre630_extraction_quality.report import (  # noqa: E402
    CaseRun,
    RunMeta,
    RunReport,
    render_json,
    render_markdown,
)
from scripts.eval.fre630_extraction_quality.scoring import score_case  # noqa: E402

from personal_agent.config import load_model_config, resolve_role_model_key, settings  # noqa: E402
from personal_agent.config.model_loader import CATALOG_RELPATH  # noqa: E402
from personal_agent.second_brain import entity_extraction  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_GOLD_SET = "scripts/eval/fre630_extraction_quality/gold_extraction.yaml"
DEFAULT_OUT = "telemetry/evaluation/fre630-extraction-quality"


def _git_commit() -> str:
    """Return the short HEAD commit, or ``unknown`` when git is unavailable."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        return out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _prompt_hash() -> str:
    """Short hash of the flag-aware effective prompt material (FRE-759).

    Delegates to ``entity_extraction.prompt_material_for_hash()`` so a flag-ON run
    (few-shot exemplars spliced) and a flag-OFF run get distinct ``prompt_hash``
    stamps — an A/B never silently compares two different prompts.
    """
    payload = entity_extraction.prompt_material_for_hash().encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:12]


def _build_meta(run_id: str, gold_set: str, samples: int, fuzzy_threshold: float) -> RunMeta:
    """Resolve the run provenance stamp from the active model config."""
    cfg = load_model_config()
    role = resolve_role_model_key("entity_extraction")
    model_def = cfg.models.get(role)
    provider = (
        (model_def.provider if model_def and model_def.provider else "local")
        if model_def
        else "unknown"
    )
    model_id = model_def.id if model_def else role
    return RunMeta(
        run_id=run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        gold_set=gold_set,
        extractor_model=model_id,
        entity_extraction_role=role,
        provider=provider,
        model_config_path=CATALOG_RELPATH,
        git_commit=_git_commit(),
        prompt_hash=_prompt_hash(),
        matcher_version=MATCHER_VERSION,
        gold_schema_version=GOLD_SCHEMA_VERSION,
        samples=samples,
        fuzzy_threshold=fuzzy_threshold,
    )


async def _run_case(case: GoldCase, samples: int, fuzzy_threshold: float) -> CaseRun:
    """Run one gold case ``samples`` times through the real extractor and score each."""
    scores = []
    for i in range(samples):
        result = await entity_extraction.extract_entities_and_relationships(
            case.source_user, case.source_assistant
        )
        score = score_case(case, result, fuzzy_threshold=fuzzy_threshold, entity_type_field="v2")
        scores.append(score)
        log.info(
            "fre630_case_sampled",
            case_id=case.case_id,
            sample=i + 1,
            entity_f1=score.entity.f1,
            hallucination_rate=score.hallucination_rate,
            empty_fallback=score.is_empty_fallback,
        )
    return CaseRun(case_id=case.case_id, tags=case.tags, samples=tuple(scores))


async def _with_cost_gate() -> object:
    """Register a Postgres-backed CostGate (mirrors the FastAPI lifespan, ADR-0065).

    The extractor's cloud path requires a registered gate before any paid call; a
    standalone harness has no app startup, so it registers (and later tears down) its
    own gate against the configured — test-stack (FRE-375) — cost substrate.

    Returns:
        The connected gate, for teardown by the caller.
    """
    from personal_agent.cost_gate import CostGate, load_budget_config, set_default_gate

    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    return gate


async def _run(args: argparse.Namespace) -> RunReport:
    """Load the gold set, run every case, and assemble the report."""
    from personal_agent.cost_gate import set_default_gate

    cases = load_gold_set(args.gold_set)
    if args.limit:
        cases = cases[: args.limit]
    log.info(
        "fre630_run_started",
        run_id=args.run_id,
        cases=len(cases),
        samples=args.samples,
        gold_set=args.gold_set,
    )
    meta = _build_meta(args.run_id, args.gold_set, args.samples, args.fuzzy_threshold)
    gate = await _with_cost_gate()
    try:
        runs = [await _run_case(c, args.samples, args.fuzzy_threshold) for c in cases]
    finally:
        await gate.disconnect()  # type: ignore[attr-defined]
        set_default_gate(None)
    return RunReport(meta=meta, cases=tuple(runs))


def _write_outputs(report: RunReport, out_dir: str) -> tuple[Path, Path]:
    """Write the JSON + markdown renders; return their paths."""
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    json_path = out / f"{report.meta.run_id}.json"
    md_path = out / f"{report.meta.run_id}.md"
    json_path.write_text(render_json(report), encoding="utf-8")
    md_path.write_text(render_markdown(report), encoding="utf-8")
    return json_path, md_path


def _parse_args() -> argparse.Namespace:
    """Parse the CLI arguments."""
    parser = argparse.ArgumentParser(description="FRE-630 extraction-quality benchmark harness")
    parser.add_argument("--run-id", required=True, help="Run identifier (names the output files)")
    parser.add_argument("--gold-set", default=DEFAULT_GOLD_SET, help="Path to the gold-set YAML")
    parser.add_argument(
        "--samples", type=int, default=1, help="Extraction samples per case (stability band)"
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Run only the first N cases (0 = all; use 1 for a smoke)",
    )
    parser.add_argument(
        "--fuzzy-threshold",
        type=float,
        default=DEFAULT_FUZZY_THRESHOLD,
        help="Tier-3 entity matcher threshold",
    )
    parser.add_argument("--out", default=DEFAULT_OUT, help="Output directory (gitignored)")
    return parser.parse_args()


def main() -> None:
    """CLI entry point."""
    args = _parse_args()
    report = asyncio.run(_run(args))
    json_path, md_path = _write_outputs(report, args.out)
    log.info(
        "fre630_run_complete",
        run_id=report.meta.run_id,
        extractor_model=report.meta.extractor_model,
        provider=report.meta.provider,
        json=str(json_path),
        md=str(md_path),
    )


if __name__ == "__main__":
    main()
