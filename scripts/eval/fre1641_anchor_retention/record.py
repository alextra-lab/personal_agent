"""Record the fetch fixtures and read the recorded ones (ADR-0158 D5.2).

The fetch step runs the production ``download_text`` (``tools/fetch.py``). It appends one JSON line
per page to a gzip file under the run directory and skips pages already recorded, so a rerun
resumes. The search step replayed searches through SearXNG instances; FRE-1767 removed it with
SearXNG (FRE-1763), and the recorded ``search_<L>.jsonl.gz`` files are only read here.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
from collections.abc import Iterable, Mapping
from pathlib import Path

from personal_agent.tools.fetch import FetchError, download_text

#: D3.2's ranking bound: the fixture keeps up to this many characters of extracted text.
FIXTURE_MAX_CHARS = 200_000
_TRACE_ID = "fre1641-harness"


#: The keyed vendor engines. One of them unresponsive means the replay did not see what production
#: sees: on 2026-10-21 run r1, both ran out of credit mid-run and SearXNG suspended them.
VENDOR_ENGINES = frozenset({"exa", "braveapi"})


def vendor_failures(data: object) -> list[str]:
    """Return ``"engine: reason"`` for each keyed vendor engine that SearXNG reports unresponsive.

    Args:
        data: A raw SearXNG response, or ``None``.

    Returns:
        The failures. Empty for a healthy response.
    """
    if not isinstance(data, dict):
        return []
    return [
        f"{e[0]}: {e[1]}"
        for e in as_list(data.get("unresponsive_engines"))
        if isinstance(e, list) and len(e) >= 2 and e[0] in VENDOR_ENGINES
    ]


def usable(row: Mapping[str, object]) -> bool:
    """Whether a recorded search row holds a response with every vendor engine healthy."""
    return isinstance(row.get("data"), dict) and not vendor_failures(row["data"])


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
    # A failed fetch is tried again on a rerun; the later row wins when the report reads them.
    done = {str(r["url"]) for r in read_jsonl_gz(out) if not r.get("error")}
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
