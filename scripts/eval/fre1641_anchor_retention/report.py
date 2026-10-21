"""Score the recorded fixtures and write the report (ADR-0158 D5, D6.1, D4; FRE-1641 AC-2 to AC-8).

Offline: reads only the run directory. The search form is the production shaping
(``shape_search_response``) rendered by the production renderer (``render_content``). The fetch
form is the production passage module (``split_passages``, ``select_within_budget``,
``focus_matched``), with D3.3's fallback: when no returned passage holds a focus term, the tool
returns the cut from the top at the default ``max_chars``.
"""

from __future__ import annotations

import json
import subprocess
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from scripts.eval.fre1641_anchor_retention.anchors import Anchor, TextIndex
from scripts.eval.fre1641_anchor_retention.rank import ARMS
from scripts.eval.fre1641_anchor_retention.record import as_list, read_jsonl_gz
from scripts.eval.fre1641_anchor_retention.score import (
    MIN_CLASS_SUPPORTED,
    MIN_FETCH_SUPPORTED,
    MIN_SEARCH_SUPPORTED,
    FetchCell,
    Retention,
    SearchCell,
    Window,
    fetch_kept,
    median,
    retention,
    search_kept,
    select_fetch,
    select_search,
    snippet_support,
    support_windows,
    url_key,
)
from scripts.eval.fre1641_anchor_retention.searxng import FULL_FORM_LENGTH, PORTS

from personal_agent.tools.contract import render_content
from personal_agent.tools.fetch import _DEFAULT_MAX_CHARS
from personal_agent.tools.passages import (
    Passage,
    focus_matched,
    select_within_budget,
    split_passages,
)
from personal_agent.tools.web import shape_search_response, web_search_tool

GRID_LENGTHS = (500, 800, 1200, 1600)
GRID_COUNTS = (5, 10)
FULL_FORM_COUNT = 10
BUDGETS = (2000, 3000, 4000)
#: D5.8 seeded negatives.
NEGATIVE_FETCH_CHARS = 200
NEGATIVE_SNIPPET_CHARS = 100
#: Vendor prices for the cost line. Exa: ADR-0158 D2.6 (measured 2026-10-11). Brave: ADR-0034.
EXA_USD_PER_SEARCH = 0.007
BRAVE_USD_PER_SEARCH = 0.005
#: The code whose identity the report records (D1.8, AC-7).
SHAPING_PATHS = (
    "src/personal_agent/tools/web.py",
    "src/personal_agent/tools/fetch.py",
    "src/personal_agent/tools/passages.py",
    "src/personal_agent/tools/contract.py",
    "scripts/eval/fre1641_anchor_retention",
)
REPO_ROOT = Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class FetchForm:
    """The text one fetch cell returns for one page.

    Attributes:
        spans: The ``(start, end)`` offsets of the returned text.
        chars: The returned characters.
        fallback: Whether the cut from the top replaced the passages (D3.3).
    """

    spans: list[tuple[int, int]]
    chars: int
    fallback: bool


def rendered_chars(shaped: Mapping[str, object]) -> int:
    """The length of the tool message the dispatch renders for one ``web_search`` result.

    Args:
        shaped: The shaped result.

    Returns:
        The rendered characters (D1.7's ``result_chars``).
    """
    return len(
        render_content(
            output=shaped,
            error_detail=None,
            max_result_tokens=web_search_tool.max_result_tokens or 20_000,
            truncation_next_action=web_search_tool.truncation_next_action or "",
        )
    )


def vendor_calls(replay: Mapping[str, object]) -> tuple[bool, bool]:
    """Whether one search reaches Exa and Brave, from its categories and engines.

    ``engines`` overrides ``categories`` when given (``tools/web.py``). The ``general`` category
    runs both; the ``exa`` category runs Exa.

    Args:
        replay: The replay.

    Returns:
        ``(exa, brave)``.
    """
    if replay.get("engines"):
        names = {e.strip().lower() for e in str(replay["engines"]).split(",")}
        return "exa" in names, "braveapi" in names
    cats = {c.strip() for c in str(replay.get("categories") or "general").split(",")}
    return bool(cats & {"general", "exa"}), "general" in cats


def code_identity() -> dict[str, object]:
    """Return the git commit and any uncommitted change to the scored code."""

    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=True
        ).stdout.strip()

    return {
        "commit": git("rev-parse", "HEAD"),
        "dirty": git("status", "--porcelain", "--", *SHAPING_PATHS).splitlines(),
    }


def _anchors(raw: Sequence[Sequence[str]]) -> list[Anchor]:
    return [Anchor(kind, value) for kind, value in raw]  # type: ignore[arg-type]


class _Fetch:
    """The fetch side of the score."""

    def __init__(
        self,
        run_dir: Path,
        findings: Sequence[Mapping[str, object]],
        helpers: Mapping[str, Mapping[str, object]],
    ) -> None:
        self.helpers = helpers
        rows = {str(r["url"]): r for r in read_jsonl_gz(run_dir / "fetch.jsonl.gz")}
        self.texts = {u: str(r["text"]) for u, r in rows.items() if not r.get("error")}
        self.fetch_errors = sum(1 for r in rows.values() if r.get("error"))
        self.ranks = {
            (str(r["task_id"]), str(r["url"])): r for r in read_jsonl_gz(run_dir / "ranks.jsonl.gz")
        }
        self.passages: dict[str, list[Passage]] = {}
        self.missing = 0
        self.with_fixture = 0
        self.supported: list[tuple[Mapping[str, object], list[Window]]] = []
        indexes: dict[str, TextIndex] = {}
        for f in findings:
            url = str(f["source_url"])
            if url not in self.texts:
                self.missing += 1
                continue
            self.with_fixture += 1
            index = indexes.setdefault(url, TextIndex(self.texts[url]))
            windows = support_windows(_anchors(f["anchors"]), index)  # type: ignore[arg-type]
            if windows:
                self.supported.append((f, windows))

    def pages(self, url: str) -> list[Passage]:
        if url not in self.passages:
            self.passages[url] = split_passages(self.texts[url])
        return self.passages[url]

    def form(self, f: Mapping[str, object], arm: str, budget: int) -> FetchForm | None:
        """The text one cell returns for one finding's page, or ``None`` if the page is unranked.

        With no focus term in the chosen passages, the tool returns the cut from the top at the
        default ``max_chars`` (D3.3), and the form says so.
        """
        url, task = str(f["source_url"]), str(f["task_id"])
        rank = self.ranks.get((task, url))
        if rank is None or rank.get("error"):
            return None
        order = rank["arms"][arm]["order"]  # type: ignore[index]
        chosen = select_within_budget(order, self.pages(url), budget)
        focus = str(self.helpers[task]["spec_task"])
        if focus_matched(focus, chosen):
            return FetchForm([(p.start, p.end) for p in chosen], sum(len(p.text) for p in chosen), False)
        cut = min(len(self.texts[url]), _DEFAULT_MAX_CHARS)
        return FetchForm([(0, cut)], cut, True)

    def score(self, price: float) -> dict[str, object]:
        kept_full = retention(
            fetch_kept(
                w, [(0, len(self.texts[str(f["source_url"])]))], self.texts[str(f["source_url"])]
            )
            for f, w in self.supported
        )
        negative = retention(
            fetch_kept(w, [(0, NEGATIVE_FETCH_CHARS)], self.texts[str(f["source_url"])])
            for f, w in self.supported
        )
        pairs = sorted({(str(f["task_id"]), str(f["source_url"])) for f, _ in self.supported})
        cells: list[FetchCell] = []
        by_class: dict[str, dict[str, object]] = {}
        unranked = 0
        fallbacks: dict[str, int] = defaultdict(int)
        for arm in ARMS:
            ranked = [
                arms[arm]
                for p in pairs
                if p in self.ranks
                and not self.ranks[p].get("error")
                and isinstance(arms := self.ranks[p]["arms"], dict)
            ]
            unranked = len(pairs) - len(ranked)
            cost = sum(float(r["tokens"]) * price for r in ranked) / len(ranked) if ranked else 0.0
            latency = median([float(r["latency_s"]) for r in ranked])
            sent = sum(1 for r in ranked if r["sent"]) / len(ranked) if ranked else 0.0
            for budget in BUDGETS:
                flags: list[bool] = []
                klass: dict[str, list[bool]] = defaultdict(list)
                for f, w in self.supported:
                    form = self.form(f, arm, budget)
                    kept = form is not None and fetch_kept(
                        w, form.spans, self.texts[str(f["source_url"])]
                    )
                    flags.append(kept)
                    klass[str(self.helpers[str(f["task_id"])]["klass"])].append(kept)
                chars: list[float] = []
                for task, url in pairs:
                    form = self.form({"task_id": task, "source_url": url}, arm, budget)
                    if form is not None:
                        chars.append(form.chars)
                        fallbacks[f"{arm}×{budget}"] += form.fallback
                cells.append(
                    FetchCell(arm, budget, retention(flags), cost, latency, sent, median(chars))
                )
                by_class[f"{arm}×{budget}"] = {
                    k: _ret(retention(v)) for k, v in sorted(klass.items())
                }
        return {
            "findings_with_fixture": self.with_fixture,
            "fixture_missing": self.missing,
            "fetch_errors": self.fetch_errors,
            "supported": len(self.supported),
            "drifted": self.with_fixture - len(self.supported),
            "drift_rate": 1 - len(self.supported) / self.with_fixture if self.with_fixture else 0.0,
            "pages_ranked": len(pairs) - unranked,
            "pages_unranked": unranked,
            "full_form": kept_full,
            "negative_200": negative,
            "cells": cells,
            "by_class": by_class,
            "fallback_cut_used": dict(fallbacks),
            "median_fixture_chars": median([len(self.texts[u]) for _, u in pairs]),
        }


def _ret(r: Retention) -> dict[str, object]:
    return {"kept": r.kept, "supported": r.supported, "share": round(r.share, 4)}


class _Search:
    """The search side of the score."""

    def __init__(
        self,
        run_dir: Path,
        facts: Mapping[str, object],
        findings: Sequence[Mapping[str, object]],
        helpers: Mapping[str, Mapping[str, object]],
    ) -> None:
        self.helpers = helpers
        self.replays = {
            str(r["replay_id"]): r for r in as_list(facts["replays"]) if isinstance(r, dict)
        }
        self.by_helper: dict[str, list[str]] = defaultdict(list)
        for s in as_list(facts["searches"]):
            assert isinstance(s, dict)
            self.by_helper[str(s["task_id"])].append(str(s["replay_id"]))
        self.data: dict[int, dict[str, Mapping[str, object]]] = {}
        self.attempts: dict[int, int] = {}
        self.errors: dict[int, int] = {}
        for length in PORTS:
            rows = read_jsonl_gz(run_dir / f"search_{length}.jsonl.gz")
            self.attempts[length] = sum(int(str(r.get("attempts", 1))) for r in rows)
            ok = {str(r["replay_id"]): r["data"] for r in rows if isinstance(r.get("data"), dict)}
            self.data[length] = ok  # type: ignore[assignment]
            self.errors[length] = len(self.replays) - len(ok)
        self._shaped: dict[tuple[int, int, str], dict[str, object]] = {}
        self.eligible = [f for f in findings if self.by_helper.get(str(f["task_id"]))]
        self.supported: list[tuple[Mapping[str, object], list[frozenset[Anchor]]]] = []
        for f in self.eligible:
            anchors = _anchors(f["anchors"])  # type: ignore[arg-type]
            sets = [
                s
                for snippet in self.snippets(f, FULL_FORM_LENGTH, FULL_FORM_COUNT)
                if (s := snippet_support(anchors, snippet)) is not None
            ]
            if sets:
                self.supported.append((f, sets))

    def shaped(self, length: int, count: int, replay_id: str) -> dict[str, object] | None:
        key = (length, count, replay_id)
        if key not in self._shaped:
            data = self.data[length].get(replay_id)
            if data is None:
                return None
            r = self.replays[replay_id]
            self._shaped[key] = shape_search_response(
                dict(data),
                query=str(r["query"]),
                categories=str(r["categories"]),
                engines=str(r["engines"]) if r.get("engines") else None,
                max_results=count,
            )
        return self._shaped[key]

    def snippets(
        self, f: Mapping[str, object], length: int, count: int, cut: int | None = None
    ) -> list[str]:
        key = url_key(str(f["source_url"]))
        out: list[str] = []
        for replay_id in self.by_helper[str(f["task_id"])]:
            shaped = self.shaped(length, count, replay_id)
            for res in as_list((shaped or {}).get("results")):
                assert isinstance(res, dict)
                if url_key(str(res["url"])) == key:
                    out.append(str(res["snippet"])[:cut] if cut else str(res["snippet"]))
        return out

    def score(self) -> dict[str, object]:
        def kept_at(
            length: int, count: int, cut: int | None = None
        ) -> tuple[Retention, dict[str, Retention]]:
            flags: list[bool] = []
            klass: dict[str, list[bool]] = defaultdict(list)
            for f, sets in self.supported:
                k = search_kept(sets, self.snippets(f, length, count, cut))
                flags.append(k)
                klass[str(self.helpers[str(f["task_id"])]["klass"])].append(k)
            return retention(flags), {c: retention(v) for c, v in klass.items()}

        replay_ids = sorted({r for ids in self.by_helper.values() for r in ids})
        cells: list[SearchCell] = []
        for length in GRID_LENGTHS:
            for count in GRID_COUNTS:
                pooled, by_class = kept_at(length, count)
                sizes = [
                    rendered_chars(s)
                    for rid in replay_ids
                    if (s := self.shaped(length, count, rid)) is not None
                ]
                cells.append(SearchCell(length, count, pooled, by_class, median(sizes)))
        full, _ = kept_at(FULL_FORM_LENGTH, FULL_FORM_COUNT)
        negative, _ = kept_at(FULL_FORM_LENGTH, FULL_FORM_COUNT, NEGATIVE_SNIPPET_CHARS)
        full_sizes = [
            rendered_chars(s)
            for rid in replay_ids
            if (s := self.shaped(FULL_FORM_LENGTH, FULL_FORM_COUNT, rid)) is not None
        ]
        return {
            "eligible": len(self.eligible),
            "supported": len(self.supported),
            "drifted": len(self.eligible) - len(self.supported),
            "drift_rate": 1 - len(self.supported) / len(self.eligible) if self.eligible else 0.0,
            "replays": len(replay_ids),
            "replay_errors": self.errors,
            "full_form": full,
            "full_form_median_chars": median(full_sizes),
            "negative_100": negative,
            "cells": cells,
        }


def analyze(run_dir: Path, usd_per_token: float) -> dict[str, object]:
    """Score one run.

    Args:
        run_dir: The run directory.
        usd_per_token: The reranker price.

    Returns:
        The report document.
    """
    facts = json.loads((run_dir / "facts.json").read_text())
    state = (
        json.loads((run_dir / "state.json").read_text())
        if (run_dir / "state.json").exists()
        else {}
    )
    helpers = {str(h["task_id"]): h for h in facts["helpers"]}
    findings = [f for f in facts["findings"] if f["anchors"]]
    fetch = _Fetch(run_dir, findings, helpers).score(usd_per_token)
    search = _Search(run_dir, facts, findings, helpers).score()

    blocked: list[str] = []
    if (
        int(str(fetch["supported"])) < MIN_FETCH_SUPPORTED
        or int(str(search["supported"])) < MIN_SEARCH_SUPPORTED
    ):
        blocked.append(
            f"sample below D5.5 (fetch {fetch['supported']} < {MIN_FETCH_SUPPORTED} or search "
            f"{search['supported']} < {MIN_SEARCH_SUPPORTED})"
        )
    neg_f: Retention = fetch["negative_200"]  # type: ignore[assignment]
    neg_s: Retention = search["negative_100"]  # type: ignore[assignment]
    if neg_f.passes or neg_s.passes:
        blocked.append("a seeded negative reached the bar (D5.8): the instrument is broken")
    full_f: Retention = fetch["full_form"]  # type: ignore[assignment]
    full_s: Retention = search["full_form"]  # type: ignore[assignment]
    if full_f.share < 1.0 or full_s.share < 1.0:
        blocked.append("a full form kept less than 100%: a scoring bug")
    reason = "; ".join(blocked) or None

    replays = {str(r["replay_id"]): r for r in facts["replays"]}
    exa = brave = 0
    search_rows = {length: read_jsonl_gz(run_dir / f"search_{length}.jsonl.gz") for length in PORTS}
    for rows in search_rows.values():
        for r in rows:
            e, b = vendor_calls(replays[str(r["replay_id"])])
            n = int(str(r.get("attempts", 1)))
            exa += n * e
            brave += n * b
    rerank_tokens = sum(int(s.get("tokens", 0)) for s in state.get("rank_sessions", []))
    return {
        "code": code_identity(),
        "prod_searxng": {"before": state.get("prod_before"), "after": state.get("prod_after")},
        "counts": facts["counts"],
        "fetch": fetch,
        "search": search,
        "blocked": reason,
        "search_selection": select_search(search["cells"], reason),  # type: ignore[arg-type]
        "fetch_selection": select_fetch(fetch["cells"], reason),  # type: ignore[arg-type]
        "cost": {
            "exa_calls": exa,
            "brave_calls": brave,
            "rerank_tokens": rerank_tokens,
            "usd": round(
                exa * EXA_USD_PER_SEARCH
                + brave * BRAVE_USD_PER_SEARCH
                + rerank_tokens * usd_per_token,
                2,
            ),
        },
        "minimums": {
            "fetch": MIN_FETCH_SUPPORTED,
            "search": MIN_SEARCH_SUPPORTED,
            "class": MIN_CLASS_SUPPORTED,
        },
    }
