"""Record the fetch and search fixtures (ADR-0158 D5.2).

Both steps run the production code: ``download_text`` (``tools/fetch.py``) for pages and
``build_search_params`` (``tools/web.py``) for searches (D1.8). Each step appends one JSON line
per item to a gzip file under the run directory and skips items already recorded, so a rerun
resumes.
"""

from __future__ import annotations

import asyncio
import gzip
import inspect
import json
import time
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1641_anchor_retention.searxng import PORTS

from personal_agent.config import settings
from personal_agent.tools.fetch import FetchError, download_text
from personal_agent.tools.web import build_search_params, web_search_executor

#: D3.2's ranking bound: the fixture keeps up to this many characters of extracted text.
FIXTURE_MAX_CHARS = 200_000
#: The executor's own default, read from its signature rather than copied (D5.2).
DEFAULT_LANGUAGE = str(inspect.signature(web_search_executor).parameters["language"].default)
_TRACE_ID = "fre1641-harness"


def as_list(value: object) -> list[object]:
    """Return ``value`` if it is a JSON list, else an empty list."""
    return value if isinstance(value, list) else []


def read_jsonl_gz(path: Path) -> list[dict[str, object]]:
    """Read every row of a gzip JSONL file, or none if it does not exist."""
    if not path.exists():
        return []
    with gzip.open(path, "rt") as fh:
        return [json.loads(line) for line in fh if line.strip()]


def _append(path: Path, row: Mapping[str, object]) -> None:
    with gzip.open(path, "at") as fh:
        fh.write(json.dumps(row) + "\n")


async def fetch_one(url: str) -> dict[str, object]:
    """Fetch one fixture with the production extraction.

    Args:
        url: The finding's ``source_url``.

    Returns:
        ``url``, ``status``, ``chars_total``, ``text`` (cut at ``FIXTURE_MAX_CHARS``),
        ``wall_s`` and ``error`` (``None`` on success).
    """
    t0 = time.monotonic()
    row: dict[str, object] = {
        "url": url,
        "status": None,
        "chars_total": 0,
        "text": "",
        "error": None,
    }
    if not url.startswith(("http://", "https://")):
        row["error"] = "invalid_url"
    else:
        try:
            text, status = await download_text(url, trace_id=_TRACE_ID)
            row.update(status=status, chars_total=len(text), text=text[:FIXTURE_MAX_CHARS])
            if not text.strip():
                row["error"] = "no_readable_text"
        except FetchError as exc:
            row["error"] = f"{exc.code}: {str(exc)[:200]}"
        except Exception as exc:  # noqa: BLE001 — one bad page must not stop the recording
            row["error"] = f"{type(exc).__name__}: {str(exc)[:200]}"
    row["wall_s"] = round(time.monotonic() - t0, 3)
    return row


async def record_fetch(urls: Iterable[str], out: Path, concurrency: int = 8) -> int:
    """Record a fixture for each URL not yet recorded.

    Args:
        urls: The URLs.
        out: The gzip JSONL file.
        concurrency: Fetches in flight.

    Returns:
        The number of rows written.
    """
    done = {str(r["url"]) for r in read_jsonl_gz(out)}
    todo = sorted(set(urls) - done)
    sem = asyncio.Semaphore(concurrency)
    lock = asyncio.Lock()

    async def one(url: str) -> None:
        async with sem:
            row = await fetch_one(url)
        async with lock:
            _append(out, row)
        print(
            f"fetch {row['status']} {row['chars_total']:>7} {row['error'] or ''} {url[:90]}",
            flush=True,
        )

    await asyncio.gather(*(one(u) for u in todo))
    return len(todo)


async def search_one(
    client: httpx.AsyncClient, port: int, replay: Mapping[str, object]
) -> dict[str, object]:
    """Replay one search through one harness instance.

    Args:
        client: The HTTP client.
        port: The instance's host port.
        replay: The replay (``query``, ``categories``, ``engines``, ``time_range``).

    Returns:
        ``replay_id``, ``data`` (the raw SearXNG JSON, or ``None``), ``error``, ``attempts`` and
        ``wall_s``.
    """
    params = build_search_params(
        str(replay["query"]),
        str(replay["categories"]),
        str(replay["engines"]) if replay.get("engines") else None,
        DEFAULT_LANGUAGE,
        str(replay["time_range"]) if replay.get("time_range") else None,
    )
    t0 = time.monotonic()
    error: str | None = None
    for attempt in (1, 2):
        try:
            r = await client.get(
                f"http://127.0.0.1:{port}/search",
                params=params,
                headers={"X-Forwarded-For": "127.0.0.1"},
                timeout=settings.searxng_timeout_seconds,
            )
            r.raise_for_status()
            return {
                "replay_id": replay["replay_id"],
                "data": r.json(),
                "error": None,
                "attempts": attempt,
                "wall_s": round(time.monotonic() - t0, 3),
            }
        except (httpx.HTTPError, ValueError) as exc:
            error = f"{type(exc).__name__}: {str(exc)[:200]}"
            await asyncio.sleep(5.0)
    return {
        "replay_id": replay["replay_id"],
        "data": None,
        "error": error,
        "attempts": 2,
        "wall_s": round(time.monotonic() - t0, 3),
    }


async def record_search(
    replays: Sequence[Mapping[str, object]], run_dir: Path, per_instance: int = 2
) -> int:
    """Replay every search through every instance, skipping rows already recorded.

    Args:
        replays: The replays.
        run_dir: The run directory. Instance ``L`` writes ``search_<L>.jsonl.gz``.
        per_instance: Searches in flight per instance.

    Returns:
        The number of rows written.
    """
    written = 0
    lock = asyncio.Lock()
    async with httpx.AsyncClient() as client:

        async def instance(length: int, port: int) -> int:
            out = run_dir / f"search_{length}.jsonl.gz"
            done = {str(r["replay_id"]) for r in read_jsonl_gz(out) if r.get("data") is not None}
            todo = [r for r in replays if str(r["replay_id"]) not in done]
            sem = asyncio.Semaphore(per_instance)

            async def one(replay: Mapping[str, object]) -> None:
                async with sem:
                    row = await search_one(client, port, replay)
                async with lock:
                    _append(out, row)
                n = (
                    len((row["data"] or {}).get("results") or [])
                    if isinstance(row["data"], dict)
                    else 0
                )
                print(f"search {length} {row['replay_id']} {n} {row['error'] or ''}", flush=True)

            await asyncio.gather(*(one(r) for r in todo))
            return len(todo)

        counts = await asyncio.gather(*(instance(length, port) for length, port in PORTS.items()))
        written = sum(counts)
    return written
