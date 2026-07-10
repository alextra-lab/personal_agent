r"""ADR-0114 D7/D8 CLI — run the baseline harness over a cue set and score it (FRE-840).

Reproduces production multipath recall (ADR-0104, arm A) against the frozen
study sandbox for each cue in the given set, scores Recall@20/nDCG@20 per
cue via ``scoring_rig.score_cue``, and emits "the scored baseline table"
(JSON + a printed summary).

**Default cue set is a SMOKE fixture, not AC-4 evidence.** ``baseline_cues_smoke.yaml``
proves the harness+rig run end-to-end; the real AC-4 pre-registered
>=30-cue/>=4-domain frozen set is FRE-841's deliverable (a separate,
concurrently-tracked ticket) — pass it via ``--cues`` once it lands, and
FRE-843's v0-synthesis seam is what assembles the full AC-4 verdict.

Run (study infra up, embedder reachable):

    make study-infra-up
    docker start cloud-sim-embeddings   # stop again when done -- the live
                                         # default profile is the managed
                                         # OVH embedder
    uv run python -m scripts.study.run_baseline
"""

from __future__ import annotations

import os

from scripts.study.config import StudySettings, study_substrate_env

# Hard-pin the study substrate before importing anything that pulls in
# personal_agent (settings is a cached import-time singleton). Hard
# assignment, NOT setdefault -- an ambient .env value must not win over the
# study target (ab_multipath.py's own codex-reviewed choice).
for _key, _value in study_substrate_env(StudySettings()).items():
    os.environ[_key] = _value

import argparse  # noqa: E402
import asyncio  # noqa: E402
import json  # noqa: E402
import sys  # noqa: E402
import uuid  # noqa: E402
from dataclasses import asdict, dataclass  # noqa: E402
from datetime import datetime, timezone  # noqa: E402
from pathlib import Path  # noqa: E402

import structlog  # noqa: E402

from personal_agent.memory.protocol_adapter import MemoryServiceAdapter  # noqa: E402
from scripts.eval.fre435_memory_recall.harness import detect_embedding_backend  # noqa: E402
from scripts.eval.fre435_memory_recall.probes import ProbeCase, load_probe_set  # noqa: E402
from scripts.study.baseline_harness import (  # noqa: E402
    connect_baseline_service,
    run_baseline_recall,
)
from scripts.study.scoring_rig import CueScore, score_cue  # noqa: E402

log = structlog.get_logger(__name__)

DEFAULT_CUES = "scripts/study/baseline_cues_smoke.yaml"
DEFAULT_OUT_DIR = "scripts/study/snapshots"
DEFAULT_K = 20


@dataclass
class BaselineReport:
    """The scored baseline table for one run."""

    run_id: str
    timestamp: str
    cues_path: str
    k: int
    cues: list[CueScore]


async def _score_case(adapter: MemoryServiceAdapter, case: ProbeCase, k: int) -> CueScore:
    """Run one cue through the baseline harness and score it."""
    trace_id = str(uuid.uuid4())
    retrieved = await run_baseline_recall(adapter, case.query, k, trace_id)
    return score_cue(case.case_id, retrieved, set(case.relevant_ids), k)


def _print_table(cues: list[CueScore]) -> None:
    """Print the scored baseline table to stdout."""
    print("\n=== FRE-840 baseline (ADR-0104 arm A) scored table ===")
    print(f"{'cue_id':<32}{'recall@k':>10}{'ndcg@k':>10}")
    for cue in cues:
        recall = f"{cue.recall_at_k:.3f}" if cue.recall_at_k is not None else "n/a"
        ndcg = f"{cue.ndcg_at_k:.3f}" if cue.ndcg_at_k is not None else "n/a"
        print(f"{cue.cue_id:<32}{recall:>10}{ndcg:>10}")


async def run(args: argparse.Namespace) -> int:
    """Drive the baseline harness across every cue in the given set."""
    try:
        cases = load_probe_set(Path(args.cues))
    except Exception:
        log.error("load_probe_set_failed", cues=args.cues, exc_info=True)
        return 1

    embedding_backend = await detect_embedding_backend()
    if embedding_backend != "real":
        log.error(
            "embedder_unreachable",
            hint=(
                "the local embedder is unreachable (zero-vector probe) -- "
                "`docker start cloud-sim-embeddings` then retry; "
                "`docker stop cloud-sim-embeddings` when done"
            ),
        )
        return 2

    try:
        service = await connect_baseline_service()
    except Exception:
        log.error("connect_baseline_service_failed", exc_info=True)
        return 3

    adapter = MemoryServiceAdapter(service)
    try:
        cues = [await _score_case(adapter, case, args.k) for case in cases]
    finally:
        await service.disconnect()

    report = BaselineReport(
        run_id=args.run_id,
        timestamp=datetime.now(timezone.utc).isoformat(),
        cues_path=args.cues,
        k=args.k,
        cues=cues,
    )

    out_dir = Path(args.out)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"baseline-{args.run_id}.json"
    out_path.write_text(json.dumps(asdict(report), indent=2, default=str))
    _print_table(cues)
    log.info("baseline_run_done", out=str(out_path))
    return 0


def main() -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description="FRE-840 ADR-0104 baseline harness")
    parser.add_argument("--run-id", default=datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S"))
    parser.add_argument("--cues", default=DEFAULT_CUES)
    parser.add_argument("--k", type=int, default=DEFAULT_K)
    parser.add_argument("--out", default=DEFAULT_OUT_DIR)
    args = parser.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
