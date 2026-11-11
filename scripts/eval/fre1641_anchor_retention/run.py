"""FRE-1641 / ADR-0158 T1 — the anchor-retention harness CLI. Retired (FRE-1767).

Retired: the harness scored ``web_search`` in its old SearXNG form, and FRE-1763 removed SearXNG.
The steps that ran SearXNG instances (``searxng-up``, ``preflight``, ``record-search``,
``searxng-down``) are gone, so no new run can be recorded. The remaining steps re-score a run
recorded before (the run directory is ``<data-dir>/<run-id>``, outside git):

    R="uv run python -m scripts.eval.fre1641_anchor_retention.run --run-id <id>"
    $R facts            # production ES, _search only → facts.json
    $R record-fetch     # production download_text → fetch.jsonl.gz
    $R rank             # 4 arms per supported page, the serving Voyage reranker → ranks.jsonl.gz
    $R score            # offline → report.json, report.md

Run from the repo root with ``PYTHONDONTWRITEBYTECODE=1``. Nothing here writes to ES, Postgres or
Neo4j (FRE-375): ES is read through ``ReadOnlyES``, the rerank client records no cost, and the
fetch path is ``download_text``, which skips the executor's novelty record.
"""

from __future__ import annotations

import argparse
import asyncio
import gzip
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1641_anchor_retention.facts import ReadOnlyES, read_facts
from scripts.eval.fre1641_anchor_retention.rank import (
    VoyageRerank,
    rank_page,
    reranker_model,
    voyage_key,
)
from scripts.eval.fre1641_anchor_retention.record import (
    read_jsonl_gz,
    record_fetch,
)
from scripts.eval.fre1641_anchor_retention.render import dump_json, markdown
from scripts.eval.fre1641_anchor_retention.report import (
    _Fetch,
    analyze,
)

DEFAULT_DATA_DIR = Path.home() / ".cache" / "seshat-eval" / "fre1641"
PROD_ES = "http://localhost:9200"  # fre-375-allow: read-only _search through ReadOnlyES
FETCH_CONCURRENCY = 8
RANK_CONCURRENCY = 4


def _state(run_dir: Path) -> dict[str, object]:
    path = run_dir / "state.json"
    return json.loads(path.read_text()) if path.exists() else {}


def _save_state(run_dir: Path, state: dict[str, object]) -> None:
    (run_dir / "state.json").write_text(json.dumps(state, indent=2))


def _facts(run_dir: Path) -> dict[str, object]:
    facts: dict[str, object] = json.loads((run_dir / "facts.json").read_text())
    return facts


def _helpers(facts: dict[str, object]) -> dict[str, dict[str, object]]:
    return {str(h["task_id"]): h for h in facts["helpers"]}  # type: ignore[attr-defined]


def _findings(facts: dict[str, object]) -> list[dict[str, object]]:
    return [f for f in facts["findings"] if f["anchors"]]  # type: ignore[attr-defined]


def cmd_facts(run_dir: Path) -> None:
    """Read the facts from production ES (read only)."""
    with httpx.Client() as client:
        facts = read_facts(ReadOnlyES(PROD_ES, client))
    (run_dir / "facts.json").write_text(json.dumps(facts, indent=1))
    print(json.dumps(facts["counts"], indent=1))


async def cmd_rank(run_dir: Path) -> None:
    """Rank every supported (helper, page) pair with the four arms."""
    facts = _facts(run_dir)
    helpers = _helpers(facts)
    fetch = _Fetch(run_dir, _findings(facts), helpers)
    pairs = sorted({(str(f["task_id"]), str(f["source_url"])) for f, _ in fetch.supported})
    out = run_dir / "ranks.jsonl.gz"
    done = {(str(r["task_id"]), str(r["url"])) for r in read_jsonl_gz(out) if not r.get("error")}
    todo = [p for p in pairs if p not in done]
    model = reranker_model()
    sem = asyncio.Semaphore(RANK_CONCURRENCY)
    lock = asyncio.Lock()
    async with httpx.AsyncClient() as client:
        voyage = VoyageRerank(client, model, voyage_key(), concurrency=RANK_CONCURRENCY)

        async def one(task: str, url: str) -> None:
            row: dict[str, object] = {"task_id": task, "url": url, "error": None}
            async with sem:
                try:
                    passages = fetch.pages(url)
                    row["passages_total"] = len(passages)
                    row["arms"] = await rank_page(str(helpers[task]["spec_task"]), passages, voyage)
                except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
                    row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
            async with lock:
                with gzip.open(out, "at") as fh:
                    fh.write(json.dumps(row) + "\n")
            print(f"rank {row.get('passages_total')} {row['error'] or ''} {url[:90]}", flush=True)

        try:
            await asyncio.gather(*(one(t, u) for t, u in todo))
        finally:
            # The billed tokens are saved even when the step fails, so the cost line stays whole.
            state = _state(run_dir)
            sessions = list(state.get("rank_sessions") or [])  # type: ignore[call-overload]
            sessions.append({"calls": voyage.calls, "tokens": voyage.tokens, "pages": len(todo)})
            state["rank_sessions"] = sessions
            _save_state(run_dir, state)
    print(f"ranked {len(todo)} pages: {voyage.calls} Voyage calls, {voyage.tokens} tokens")


def cmd_score(run_dir: Path) -> None:
    """Score offline and write the report."""
    doc = analyze(run_dir, reranker_model().usd_per_token)
    (run_dir / "report.json").write_text(dump_json(doc))
    text = markdown(doc)
    (run_dir / "report.md").write_text(text)
    sys.stdout.write(text)


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the step and run it.

    Args:
        argv: Arguments, or ``None`` for ``sys.argv``.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "step",
        choices=[
            "facts",
            "record-fetch",
            "rank",
            "score",
        ],
    )
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--data-dir", type=Path, default=DEFAULT_DATA_DIR)
    args = parser.parse_args(argv)
    run_dir = args.data_dir / args.run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    match args.step:
        case "facts":
            cmd_facts(run_dir)
        case "record-fetch":
            urls = {str(f["source_url"]) for f in _findings(_facts(run_dir))}
            n = asyncio.run(record_fetch(urls, run_dir / "fetch.jsonl.gz", FETCH_CONCURRENCY))
            print(f"fetched {n}")
        case "rank":
            asyncio.run(cmd_rank(run_dir))
        case "score":
            cmd_score(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
