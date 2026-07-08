"""FRE-655 — A/B + floor calibration for ADR-0100 relevance-bounded recall.

Drives the FRE-489 probe set through **both** prod recall paths, flag off vs on,
on the isolated test substrate, and proposes a ``recall_similarity_floor`` from
the observed cosine distributions. Measurement only — it changes no production
config and never deploys (the rollout is master-owned, per the ticket).

Reuses the FRE-435 harness seeding (``seed_replay`` / ``load_distractors`` /
``wipe_substrate``) so the write path is identical to the baseline run; adds:
  * **fidelity** — drives ``query_memory`` with prod-style entity hints
    (``_capitalized_entity_hints``), so the FRE-653 ``entity_recall`` branch is
    actually exercised (the baseline harness drove query_text only);
  * **dual path** — also drives ``recall_broad`` (the MEMORY_RECALL path);
  * **calibration capture** — the expected-entity cosine (positive) and the top
    distractor cosine (negative) per case, fed to the floor sweep.

Run (test substrate up + embedder reachable):

    uv run python scripts/eval/fre435_memory_recall/ab_relevance_bounded.py \
        --run-id ab-$(date +%Y%m%d) --distractor-background 40

``--distractor-background`` (default 40) reads live production Neo4j via
``harness.fetch_live_distractors``, which requires ``FRE435_LIVE_NEO4J_PASSWORD``
to be set explicitly (FRE-778 — it must never fall back to the test-substrate
password). Pass ``--distractor-background 0`` to skip the live-corpus read.
"""

from __future__ import annotations

import os

# Pin the TEST substrate before importing personal_agent (settings is a cached
# import-time singleton). Mirrors harness.py exactly.
_TEST_SUBSTRATE_ENV = {
    "APP_ENV": "test",
    "AGENT_MODEL_CONFIG_PATH": "config/models.cloud.yaml",
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
import json  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from dataclasses import asdict, dataclass, field  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402
from scripts.eval.fre435_memory_recall.calibration import propose_floor, sweep_floor  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import (  # noqa: E402
    fetch_live_distractors,
    load_distractors,
    seed_replay,
    wipe_substrate,
)
from scripts.eval.fre435_memory_recall.keyword_baseline import (  # noqa: E402
    fractional_recall_at_k,
)
from scripts.eval.fre435_memory_recall.metrics import recall_at_k  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.eval.fre435_memory_recall.scoring import flatten_recall  # noqa: E402
from scripts.eval.fre435_memory_recall.semantic_report import (  # noqa: E402
    aggregate_by_register,
    control_abstention,
    register_delta,
)

from personal_agent.config import settings  # noqa: E402
from personal_agent.memory.embeddings import generate_embedding  # noqa: E402
from personal_agent.memory.protocol import MemoryRecallQuery  # noqa: E402
from personal_agent.memory.protocol_adapter import MemoryServiceAdapter  # noqa: E402
from personal_agent.memory.service import MemoryService  # noqa: E402
from personal_agent.request_gateway.context import _capitalized_entity_hints  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_PROBE_SET = "scripts/eval/fre435_memory_recall/bespoke_probe.yaml"
DEFAULT_OUT = "telemetry/evaluation/fre435-memory-recall"
#: Recall cut-offs for the FRE-670 three-arm comparison (matches keyword_baseline.py).
_SEMANTIC_K = (1, 5)


@dataclass
class CaseAB:
    """Per-case A/B outcome across both paths and both flag states."""

    case_id: str
    tags: tuple[str, ...]
    expected: tuple[str, ...]
    hints: tuple[str, ...]
    entity_recall_off: float | None
    entity_recall_on: float | None
    broad_hit_off: bool
    broad_hit_on: bool
    positive_cosine: float | None
    top_distractor_cosine: float | None
    recovered: bool = False  # denied off, surfaced on (entity path)


@dataclass
class ABReport:
    """The full A/B run."""

    run_id: str
    timestamp: str
    probe_set: str
    prod_k: int
    distractor_background_n: int
    vector_top_k: int
    cosine_range: tuple[float, float]
    cases: list[CaseAB] = field(default_factory=list)
    proposed_floor: float = 0.0
    proposed_floor_recall: float = 0.0
    proposed_floor_fpr: float = 0.0


async def _capture_cosines(
    service: MemoryService, case: ProbeCase, top_k: int
) -> tuple[float | None, float | None]:
    """Return (expected-entity cosine, top non-expected cosine) for the case query.

    The positive is the best cosine among the case's expected entities; the
    negative is the best cosine among everything else the vector index returns
    (the strongest distractor the floor must exclude).
    """
    embedding = await generate_embedding(case.query, mode="query")
    if not any(x != 0.0 for x in embedding):
        return None, None
    async with service.driver.session() as session:  # type: ignore[union-attr]
        rows = await service._query_entity_vector_candidates(session, embedding, top_k)
    expected = {e.name for e in case.seed_entities} | set(case.expected.entity_names)
    pos = [float(r["score"]) for r in rows if r.get("name") in expected]
    neg = [float(r["score"]) for r in rows if r.get("name") not in expected]
    return (max(pos) if pos else None, max(neg) if neg else None)


async def _entity_recall(
    adapter: MemoryServiceAdapter, case: ProbeCase, k: int, trace_id: str
) -> float | None:
    """Prod-faithful entity-path recall@k: hints from the query + query_text.

    Reuses the harness ``flatten_recall`` (which maps a recalled Turn's
    ``key_entities`` into the entity namespace) and ``case.relevant_ids`` so the
    scoring is identical to the FRE-491 baseline.
    """
    hints = _capitalized_entity_hints(case.query)
    query = MemoryRecallQuery(entity_names=hints[:5], query_text=case.query, limit=k)
    result = await adapter.recall(query, trace_id=trace_id)
    retrieved = flatten_recall(result.episodes, result.entities, result.relevance_scores)
    relevant = set(case.relevant_ids)
    if not relevant:
        return None
    return recall_at_k(retrieved, relevant, k)


async def _broad_hit(adapter: MemoryServiceAdapter, case: ProbeCase, trace_id: str) -> bool:
    """Broad-path AC-1b check: does an expected entity surface in recall_broad?"""
    broad = await adapter.recall_broad(
        entity_types=None,
        recency_days=90,
        limit=20,
        trace_id=trace_id,
        query_text=case.query,
    )
    names = {
        str(e.get("name")).strip().lower()
        for group in broad.entities_by_type.values()
        for e in group
        if e.get("name")
    }
    expected = {n.strip().lower() for n in case.expected.entity_names}
    return bool(expected & names)


def _set_flag(enabled: bool, floor: float) -> None:
    """Toggle the relevance-bounded flag + floor on the cached settings singleton."""
    settings.relevance_bounded_recall_enabled = enabled
    settings.recall_similarity_floor = floor


async def run(args: argparse.Namespace) -> int:
    """Drive the A/B and write the report."""
    cases = load_probe_set(Path(args.probe_set))
    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect():
        log.error("memory_service_unreachable", uri=os.environ.get("AGENT_NEO4J_URI"))
        return 2
    if not await service.ensure_vector_index():
        log.error("vector_index_unavailable")
        await service.disconnect()
        return 3
    adapter = MemoryServiceAdapter(service)
    distractors = await fetch_live_distractors(args.distractor_background)
    top_k = settings.proactive_memory_vector_top_k
    log.info(
        "ab_start",
        run_id=args.run_id,
        cases=len(cases),
        distractor_background=len(distractors),
        vector_top_k=top_k,
    )

    results: list[CaseAB] = []
    cos_lo, cos_hi = 1.0, 0.0
    try:
        for case in cases:
            trace_id = str(uuid.uuid4())
            session_id = f"fre655-{args.run_id}-{case.case_id}"
            await wipe_substrate(service, trace_id)
            case_time = datetime.now(timezone.utc)
            await seed_replay(service, case, trace_id, session_id)
            if distractors:
                await load_distractors(service, distractors, case_time, trace_id)

            pos, neg = await _capture_cosines(service, case, top_k)
            for v in (pos, neg):
                if v is not None:
                    cos_lo, cos_hi = min(cos_lo, v), max(cos_hi, v)

            _set_flag(False, 0.0)
            e_off = await _entity_recall(adapter, case, args.prod_k, trace_id)
            b_off = await _broad_hit(adapter, case, trace_id)
            _set_flag(True, 0.0)
            e_on = await _entity_recall(adapter, case, args.prod_k, trace_id)
            b_on = await _broad_hit(adapter, case, trace_id)

            recovered = bool(e_off is not None and e_off == 0.0 and e_on and e_on > 0.0)
            results.append(
                CaseAB(
                    case_id=case.case_id,
                    tags=case.tags,
                    expected=case.expected.entity_names,
                    hints=tuple(_capitalized_entity_hints(case.query)[:5]),
                    entity_recall_off=e_off,
                    entity_recall_on=e_on,
                    broad_hit_off=b_off,
                    broad_hit_on=b_on,
                    positive_cosine=pos,
                    top_distractor_cosine=neg,
                    recovered=recovered,
                )
            )
            log.info(
                "ab_case",
                case=case.case_id,
                e_off=e_off,
                e_on=e_on,
                b_off=b_off,
                b_on=b_on,
                recovered=recovered,
            )
    finally:
        _set_flag(False, 0.0)
        await service.disconnect()

    positives = [c.positive_cosine for c in results if c.positive_cosine is not None]
    negatives = [c.top_distractor_cosine for c in results if c.top_distractor_cosine is not None]
    proposal = propose_floor(positives, negatives)

    report = ABReport(
        run_id=args.run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        probe_set=args.probe_set,
        prod_k=args.prod_k,
        distractor_background_n=len(distractors),
        vector_top_k=top_k,
        cosine_range=(round(cos_lo, 4), round(cos_hi, 4)) if cos_hi >= cos_lo else (0.0, 0.0),
        cases=results,
        proposed_floor=proposal.floor,
        proposed_floor_recall=proposal.recall,
        proposed_floor_fpr=proposal.false_positive_rate,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ab-{args.run_id}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, default=str))
    _print_summary(report, positives, negatives)
    log.info("ab_done", out=str(out_path))
    return 0


async def calibrate(args: argparse.Namespace) -> int:
    """Floor-calibration pass: co-seed all cases' entities, capture cross-case scores.

    The A/B run wipes between cases, so the only entities in the index at query
    time are the case's own — there is no *negative* (a wrong-but-embedded entity)
    to calibrate the floor against. This pass seeds **all** cases' entities into
    one KG (no wipe), then per case query records the positive (the case's own
    expected entity cosine) and the negative (the strongest OTHER-case entity
    cosine — a co-resident, embedded, semantically-unrelated entity the floor must
    exclude). That negative distribution is what AC-4 turns on.
    """
    cases = load_probe_set(Path(args.probe_set))
    service = MemoryService()  # fre-375-allow: test stack pinned module-top (:7688)
    if not await service.connect() or not await service.ensure_vector_index():
        log.error("calibrate_substrate_unavailable")
        return 2
    top_k = max(settings.proactive_memory_vector_top_k, len(cases) * 2)
    try:
        await wipe_substrate(service, str(uuid.uuid4()))
        for case in cases:  # co-seed every case (no wipe between)
            trace_id = str(uuid.uuid4())
            await seed_replay(service, case, trace_id, f"fre655-cal-{case.case_id}")

        positives: list[float] = []
        negatives: list[float] = []
        rows_out: list[dict[str, float | str]] = []
        # FRE-670 — co-resident vector recall, the vector arm of the three-arm
        # comparison. `_query_entity_vector_candidates` ranks the query against
        # ALL co-seeded entities (the same 54-note corpus BM25 ranks over in
        # keyword_baseline.py), so recall here is apples-to-apples with the BM25
        # column (codex review #1: a wipe-per-case `ab` run would let the vector
        # path face only one note and win trivially).
        per_case_recall: list[tuple[str, dict[int, float]]] = []
        control_cosines: list[float] = []
        for case in cases:
            embedding = await generate_embedding(case.query, mode="query")
            if not any(x != 0.0 for x in embedding):
                continue
            async with service.driver.session() as session:  # type: ignore[union-attr]
                rows = await service._query_entity_vector_candidates(session, embedding, top_k)
            expected = {n.strip().lower() for n in case.expected.entity_names} | {
                e.name.strip().lower() for e in case.seed_entities
            }
            pos = [
                float(r["score"])
                for r in rows
                if str(r.get("name", "")).strip().lower() in expected
            ]
            neg = [
                float(r["score"])
                for r in rows
                if str(r.get("name", "")).strip().lower() not in expected
            ]
            if pos:
                positives.append(max(pos))
            if neg:
                negatives.append(max(neg))
            rows_out.append(
                {
                    "case_id": case.case_id,
                    "positive_cosine": max(pos) if pos else -1.0,
                    "top_negative_cosine": max(neg) if neg else -1.0,
                }
            )
            # Per-case recall (positives) / abstention cosine (controls).
            ranked = [str(r.get("name", "")).strip().lower() for r in rows]
            register = next(
                (t.split(":", 1)[1] for t in case.tags if t.startswith("register:")), "unknown"
            )
            recall_expected = {n.strip().lower() for n in case.expected.entity_names if n.strip()}
            if "type:control" in case.tags:
                control_cosines.append(float(rows[0]["score"]) if rows else 0.0)
            elif recall_expected:
                recall_by_k = {
                    k: fractional_recall_at_k(ranked, recall_expected, k) for k in _SEMANTIC_K
                }
                per_case_recall.append((register, recall_by_k))
            log.info(
                "calibrate_case",
                case=case.case_id,
                positive=max(pos) if pos else None,
                top_negative=max(neg) if neg else None,
            )
    finally:
        await service.disconnect()

    proposal = propose_floor(positives, negatives)
    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "run_id": args.run_id,
        "mode": "calibrate",
        "cases_scored": len(rows_out),
        "positives": sorted(positives),
        "negatives": sorted(negatives),
        "proposed_floor": proposal.floor,
        "proposed_floor_recall": proposal.recall,
        "proposed_floor_fpr": proposal.false_positive_rate,
        "rows": rows_out,
    }
    # FRE-670 three-arm vector report: per-register recall + control abstention.
    semantic = aggregate_by_register(per_case_recall, _SEMANTIC_K)
    abstained, controls_total = control_abstention(control_cosines, proposal.floor)
    payload["semantic_recall"] = {
        "overall": semantic["overall"],
        "by_register": semantic["by_register"],
        "register_delta_at_5": register_delta(semantic["by_register"], 5),
        "control_abstention": {
            "abstained": abstained,
            "total": controls_total,
            "floor": proposal.floor,
        },
    }
    (out_dir / f"calibrate-{args.run_id}.json").write_text(json.dumps(payload, indent=2))

    _print_semantic_recall(semantic, abstained, controls_total, proposal.floor)
    print("\n=== FRE-655 floor calibration (co-resident, cross-case negatives) ===")
    print(f"cases_scored={len(rows_out)}")
    print(
        f"positives n={len(positives)}  min={round(min(positives), 4) if positives else None}  "
        f"median={round(sorted(positives)[len(positives) // 2], 4) if positives else None}"
    )
    print(
        f"negatives n={len(negatives)}  min={round(min(negatives), 4) if negatives else None}  "
        f"median={round(sorted(negatives)[len(negatives) // 2], 4) if negatives else None}  "
        f"max={round(max(negatives), 4) if negatives else None}"
    )
    print(
        f">>> proposed floor={proposal.floor}  recall={proposal.recall}  fpr={proposal.false_positive_rate}"
    )
    print("\n  sweep (floor: recall / fpr):")
    for p in sweep_floor(positives, negatives):
        print(f"    {p.floor:.2f}: {p.recall:.2f} / {p.false_positive_rate:.2f}")
    log.info("calibrate_done")
    return 0


def _print_semantic_recall(
    semantic: dict[str, dict[str, dict[int, float]]],
    abstained: int,
    controls_total: int,
    floor: float,
) -> None:
    """Emit the FRE-670 vector arm: per-register recall@k + control abstention."""
    overall = semantic["overall"]
    by_register = semantic["by_register"]
    ks = " ".join(f"@{k}={overall[k]:.3f}" for k in sorted(overall))
    print("\n=== FRE-670 VECTOR ARM (co-resident recall over the 54-note corpus) ===")
    print(f"positives recall {ks}")
    for register in sorted(by_register):
        reg = by_register[register]
        rk = " ".join(f"@{k}={reg[k]:.3f}" for k in sorted(reg))
        print(f"  register:{register:8s} recall {rk}")
    delta = register_delta(by_register, 5)
    if delta is not None:
        print(f"  register delta @5 (natural − imagery): {delta:+.3f}")
    if controls_total:
        print(f"controls: abstained (top cosine < floor {floor:.3f}) {abstained}/{controls_total}")
    print(
        "AC2 (FRE-670): this vector recall@5 must land MATERIALLY ABOVE the BM25 keyword "
        "recall@5 (keyword_baseline.py) on the positives."
    )


def _print_summary(report: ABReport, positives: list[float], negatives: list[float]) -> None:
    """Emit a human-readable A/B + calibration summary to stdout."""
    n = len(report.cases)
    e_off = [c.entity_recall_off for c in report.cases if c.entity_recall_off is not None]
    e_on = [c.entity_recall_on for c in report.cases if c.entity_recall_on is not None]
    recovered = sum(1 for c in report.cases if c.recovered)
    broad_off = sum(1 for c in report.cases if c.broad_hit_off)
    broad_on = sum(1 for c in report.cases if c.broad_hit_on)
    empty_hint = sum(1 for c in report.cases if not c.hints)

    def _mean(xs: list[float]) -> float:
        return round(sum(xs) / len(xs), 4) if xs else 0.0

    print("\n=== FRE-655 A/B (ADR-0100 relevance-bounded recall) ===")
    print(
        f"cases={n}  distractors={report.distractor_background_n}  vector_top_k={report.vector_top_k}"
    )
    print(f"observed cosine range: {report.cosine_range}")
    print(
        f"\nENTITY PATH  recall@{report.prod_k}: off={_mean(e_off)}  on={_mean(e_on)}  (n_scored={len(e_off)})"
    )
    print(f"  recovered (denied off -> surfaced on): {recovered}/{n}")
    print(f"  empty-hint cases (broad-path only): {empty_hint}/{n}")
    print(f"BROAD PATH   expected-entity hit: off={broad_off}/{n}  on={broad_on}/{n}")
    print("\nFLOOR CALIBRATION (cosine):")
    print(
        f"  positives n={len(positives)}  min={min(positives) if positives else None}  "
        f"median={sorted(positives)[len(positives) // 2] if positives else None}"
    )
    print(f"  negatives n={len(negatives)}  max={max(negatives) if negatives else None}")
    print(
        f"  >>> proposed floor={report.proposed_floor}  recall={report.proposed_floor_recall}  fpr={report.proposed_floor_fpr}"
    )
    print("\n  sweep (floor: recall / fpr):")
    for p in sweep_floor(positives, negatives):
        print(f"    {p.floor:.2f}: {p.recall:.2f} / {p.false_positive_rate:.2f}")


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="FRE-655 relevance-bounded recall A/B")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--probe-set", default=DEFAULT_PROBE_SET)
    parser.add_argument("--prod-k", type=int, default=5)
    parser.add_argument("--distractor-background", type=int, default=40)
    parser.add_argument("--out", default=DEFAULT_OUT)
    parser.add_argument(
        "--mode",
        choices=("ab", "calibrate"),
        default="ab",
        help="ab = recall A/B (wipe per case); calibrate = co-seed all cases for floor negatives.",
    )
    args = parser.parse_args()
    return asyncio.run(calibrate(args) if args.mode == "calibrate" else run(args))


if __name__ == "__main__":
    sys.exit(main())
