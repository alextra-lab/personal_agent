"""FRE-1641 / ADR-0158 T1 — the anchor-retention harness CLI.

Steps, in order (the run directory is ``<data-dir>/<run-id>``, outside git):

    R="uv run python -m scripts.eval.fre1641_anchor_retention.run --run-id <id>"
    $R facts            # production ES, _search only → facts.json
    $R searxng-up       # 5 instances under project fre1641; records prod StartedAt (before)
    $R preflight        # 1-item trace + cost and time projection → preflight.md (AC-1)
    # ... post the preflight on the ticket and wait for the owner's go ...
    $R record-fetch     # production download_text → fetch.jsonl.gz
    $R record-search    # 5 instances × every replay → search_<L>.jsonl.gz
    $R rank             # 4 arms per supported page, Voyage rerank-2.5 → ranks.jsonl.gz
    $R searxng-down     # stop the instances, delete the settings copies; prod StartedAt (after)
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
import time
from collections.abc import Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1641_anchor_retention import searxng
from scripts.eval.fre1641_anchor_retention.anchors import Anchor, TextIndex
from scripts.eval.fre1641_anchor_retention.facts import ReadOnlyES, read_facts
from scripts.eval.fre1641_anchor_retention.rank import (
    VoyageRerank,
    rank_page,
    reranker_model,
    voyage_key,
)
from scripts.eval.fre1641_anchor_retention.record import (
    fetch_one,
    read_jsonl_gz,
    record_fetch,
    record_search,
    search_one,
)
from scripts.eval.fre1641_anchor_retention.render import dump_json, markdown
from scripts.eval.fre1641_anchor_retention.report import (
    BRAVE_USD_PER_SEARCH,
    EXA_USD_PER_SEARCH,
    _Fetch,
    analyze,
    rendered_chars,
    vendor_calls,
)
from scripts.eval.fre1641_anchor_retention.score import support_windows

from personal_agent.tools.passages import split_passages
from personal_agent.tools.web import shape_search_response

DEFAULT_DATA_DIR = Path.home() / ".cache" / "seshat-eval" / "fre1641"
PROD_ES = "http://localhost:9200"  # fre-375-allow: read-only _search through ReadOnlyES
FETCH_CONCURRENCY = 8
SEARCH_PER_INSTANCE = 2
RANK_CONCURRENCY = 4
BRAVE_SECRET = "seshat/AGENT_BRAVE_API_KEY"


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


def cmd_searxng_up(run_dir: Path) -> None:
    """Start the five harness instances and record the production container's state."""
    prod = searxng.prod_container()
    state = _state(run_dir)
    state.setdefault("prod_before", prod)
    _save_state(run_dir, state)
    root = run_dir / "searxng"
    searxng.write_settings(root)
    try:
        searxng.up(root, prod["image"])
    except Exception:
        searxng.down(root, prod["image"])
        raise
    after = searxng.prod_container()
    if after["started_at"] != prod["started_at"]:
        raise RuntimeError("production searxng restarted during bring-up")
    print(f"up: {sorted(searxng.PORTS)}; production searxng unchanged ({prod['started_at']})")


def cmd_searxng_down(run_dir: Path) -> None:
    """Stop the instances, delete the settings copies, and record the production state."""
    state = _state(run_dir)
    prod = searxng.prod_container()
    searxng.down(run_dir / "searxng", prod["image"])
    state["prod_after"] = searxng.prod_container()
    _save_state(run_dir, state)
    before = state.get("prod_before")
    print(f"down; production StartedAt before {before} after {state['prod_after']}")


def _brave_quota() -> dict[str, str]:
    """One direct Brave call that reads the plan's rate-limit headers. The key is never printed."""
    import subprocess  # noqa: PLC0415

    key = subprocess.run(
        ["pass", "show", BRAVE_SECRET], capture_output=True, text=True, check=True
    ).stdout.splitlines()[0]
    r = httpx.get(
        "https://api.search.brave.com/res/v1/web/search",
        params={"q": "seshat", "count": "1"},
        headers={"X-Subscription-Token": key, "Accept": "application/json"},
        timeout=20.0,
    )
    return {
        "status": str(r.status_code),
        **{k: v for k, v in r.headers.items() if k.lower().startswith("x-ratelimit")},
    }


async def cmd_preflight(run_dir: Path) -> None:
    """Trace one fetch, one rank and one search; project the full run's cost and time (AC-1)."""
    facts = _facts(run_dir)
    helpers = _helpers(facts)
    findings = _findings(facts)
    replays = {str(r["replay_id"]): r for r in facts["replays"]}  # type: ignore[attr-defined]
    counts: dict[str, object] = facts["counts"]  # type: ignore[assignment]
    urls = sorted({str(f["source_url"]) for f in findings})
    pairs = {(str(f["task_id"]), str(f["source_url"])) for f in findings}
    out: list[str] = ["# FRE-1641 preflight", "", "## Counts", ""]
    out += [f"- {k}: {v}" for k, v in counts.items()]
    out += [
        f"- fetches: {len(urls)} distinct URLs",
        f"- pages to rank, at most: {len(pairs)} (helper, URL) pairs × 4 arms",
        f"- SearXNG instances: {len(searxng.PORTS)} (Exa {sorted(searxng.PORTS)})",
    ]

    # The trace: the first finding whose page fetches and supports it.
    out += ["", "## Trace (1 item)", ""]
    model = reranker_model()
    trace_page: dict[str, object] = {}
    task = ""
    async with httpx.AsyncClient() as client:
        voyage = VoyageRerank(client, model, voyage_key(), concurrency=1)
        for f in findings:
            row = await fetch_one(str(f["source_url"]))
            if row["error"]:
                out.append(f"- fetch {f['source_url']}: {row['error']} (next finding)")
                continue
            text = str(row["text"])
            anchors = [Anchor(k, v) for k, v in f["anchors"]]  # type: ignore[attr-defined]
            windows = support_windows(anchors, TextIndex(text))
            if not windows:
                out.append(f"- fetch {f['source_url']}: {len(text)} chars, finding drifted (next)")
                continue
            passages = split_passages(text)
            t0 = time.monotonic()
            arms = await rank_page(str(helpers[str(f["task_id"])]["spec_task"]), passages, voyage)
            rank_s = time.monotonic() - t0
            trace_page = {"fetch_s": row["wall_s"], "rank_s": rank_s, "tokens": voyage.tokens}
            out += [
                f"- fetch `{f['source_url']}`: HTTP {row['status']}, {row['chars_total']:,} chars, "
                f"{row['wall_s']} s",
                f"- claim: {str(f['claim'])[:160]}",
                f"- anchors: {[a.value for a in anchors]}; support windows: {len(windows)}",
                f"- passages: {len(passages)}; rank all arms: {rank_s:.2f} s, "
                f"{voyage.calls} Voyage calls, {voyage.tokens} tokens",
                *[
                    f"  - {arm}: sent={a['sent']} tokens={a['tokens']} latency={a['latency_s']} s"
                    for arm, a in arms.items()
                ],
            ]
            task = str(f["task_id"])
            break
        mine = [str(s["replay_id"]) for s in facts["searches"] if s["task_id"] == task]  # type: ignore[attr-defined]
        replay = replays[mine[0]] if mine else next(iter(replays.values()))
        search_s = 0.0
        for length in (2000, 500):
            r = await search_one(client, searxng.PORTS[length], replay)
            search_s = max(search_s, float(str(r["wall_s"])))
            data = r["data"] if isinstance(r["data"], dict) else {}
            shaped = shape_search_response(
                data,
                query=str(replay["query"]),
                categories=str(replay["categories"]),
                engines=str(replay["engines"]) if replay.get("engines") else None,
                max_results=10,
            )
            out.append(
                f"- search on the {length} instance, `{str(replay['query'])[:80]}`: "
                f"{shaped['result_count']} results, {rendered_chars(shaped):,} rendered chars, "
                f"{r['wall_s']} s, error {r['error']}"
            )
    quota = _brave_quota()
    out.append(f"- Brave plan headers (one direct call): {quota}")

    exa = sum(vendor_calls(r)[0] for r in replays.values()) * len(searxng.PORTS)
    brave = sum(vendor_calls(r)[1] for r in replays.values()) * len(searxng.PORTS)
    page_tokens = float(str(trace_page.get("tokens", 0)))
    rerank_usd = page_tokens * len(pairs) * model.usd_per_token
    search_usd = exa * EXA_USD_PER_SEARCH + brave * BRAVE_USD_PER_SEARCH
    search_min = len(replays) * search_s / SEARCH_PER_INSTANCE / 60
    fetch_min = len(urls) * float(str(trace_page.get("fetch_s", 3))) / FETCH_CONCURRENCY / 60
    rank_min = len(pairs) * float(str(trace_page.get("rank_s", 2))) / RANK_CONCURRENCY / 60
    out += [
        "",
        "## Projection",
        "",
        f"- Exa calls {exa} × ${EXA_USD_PER_SEARCH} = ${exa * EXA_USD_PER_SEARCH:.2f}",
        f"- Brave calls {brave} × ${BRAVE_USD_PER_SEARCH} = ${brave * BRAVE_USD_PER_SEARCH:.2f}",
        f"- rerank: {page_tokens:.0f} tokens per page (trace) × {len(pairs)} pages at "
        f"${model.usd_per_token * 1e6:.2f}/M = ${rerank_usd:.2f}",
        f"- total vendor cost ≈ ${search_usd + rerank_usd:.2f}",
        f"- wall time ≈ fetch {fetch_min:.0f} min + search {search_min:.0f} min (instances in "
        f"parallel, {SEARCH_PER_INSTANCE} per instance) + rank {rank_min:.0f} min",
    ]
    text = "\n".join(out) + "\n"
    (run_dir / "preflight.md").write_text(text)
    sys.stdout.write(text)


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
    state = _state(run_dir)
    state["prod_at_score"] = searxng.prod_container()
    _save_state(run_dir, state)
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
            "searxng-up",
            "preflight",
            "record-fetch",
            "record-search",
            "rank",
            "searxng-down",
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
        case "searxng-up":
            cmd_searxng_up(run_dir)
        case "preflight":
            asyncio.run(cmd_preflight(run_dir))
        case "record-fetch":
            urls = {str(f["source_url"]) for f in _findings(_facts(run_dir))}
            n = asyncio.run(record_fetch(urls, run_dir / "fetch.jsonl.gz", FETCH_CONCURRENCY))
            print(f"fetched {n}")
        case "record-search":
            replays = _facts(run_dir)["replays"]
            n = asyncio.run(record_search(replays, run_dir, SEARCH_PER_INSTANCE))  # type: ignore[arg-type]
            print(f"searched {n}")
        case "rank":
            asyncio.run(cmd_rank(run_dir))
        case "searxng-down":
            cmd_searxng_down(run_dir)
        case "score":
            cmd_score(run_dir)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
