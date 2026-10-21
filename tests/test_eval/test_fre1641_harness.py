"""FRE-1641 / ADR-0158 T1: the harness guards, the code identity rule, and one scored run."""

from __future__ import annotations

import ast
import gzip
import json
from pathlib import Path

import httpx
import pytest
import yaml
from personal_agent.tools.passages import split_passages
from scripts.eval.fre1641_anchor_retention import searxng
from scripts.eval.fre1641_anchor_retention.facts import ReadOnlyES, task_class
from scripts.eval.fre1641_anchor_retention.report import analyze, vendor_calls

REPO = Path(__file__).resolve().parents[2]
HARNESS = REPO / "scripts" / "eval" / "fre1641_anchor_retention"


# ── AC-6: production is untouched ─────────────────────────────────────────


def test_read_only_es_sends_only_search() -> None:
    """The ES client has one request shape: POST <index>/_search."""
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        return httpx.Response(
            200,
            json={
                "hits": {"total": {"value": 1, "relation": "eq"}, "hits": [{"_source": {"a": 1}}]}
            },
        )

    es = ReadOnlyES("http://es:9200", httpx.Client(transport=httpx.MockTransport(handler)))
    assert es.search("agent-logs-*", {"size": 10}) == [{"a": 1}]
    assert seen == [("POST", "/agent-logs-*/_search")]
    with pytest.raises(ValueError):
        es.search("agent-logs-*/_doc", {"size": 10})
    with pytest.raises(ValueError):
        es.search("_bulk", {"size": 10})
    assert not [m for m in dir(es) if not m.startswith("_") and m != "search"]


def test_read_only_es_refuses_a_filled_page() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"hits": {"total": {"value": 10, "relation": "eq"}, "hits": []}}
        )

    es = ReadOnlyES("http://es:9200", httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="fill size"):
        es.search("agent-logs-*", {"size": 10})


def test_check_plan_refuses_a_production_container() -> None:
    ok = "Container fre1641-searxng-500-1  Creating\nNetwork fre1641_default  Creating\n"
    assert searxng.check_plan(ok) == ["fre1641-searxng-500-1"]
    with pytest.raises(searxng.SearxngSetupError, match="outside"):
        searxng.check_plan(ok + "Container cloud-sim-searxng  Recreate\n")
    with pytest.raises(searxng.SearxngSetupError, match="no container"):
        searxng.check_plan("nothing\n")


def test_compose_file_defines_only_harness_instances() -> None:
    compose = yaml.safe_load((HARNESS / "docker-compose.fre1641.yml").read_text())
    services = compose["services"]
    assert sorted(services) == sorted(f"searxng-{n}" for n in searxng.PORTS)
    for svc in services.values():
        assert "container_name" not in svc
        assert "depends_on" not in svc
        assert "networks" not in svc
        assert all(p.startswith("127.0.0.1:") for p in svc["ports"])


# ── D5.2: each instance differs only in the Exa length ────────────────────


def _example() -> str:
    return (REPO / "docker" / "searxng" / "settings.yml.example").read_text()


def test_with_exa_length_changes_only_that_value() -> None:
    text = _example()
    changed = searxng.with_exa_length(text, 800)
    exa = [e for e in yaml.safe_load(changed)["engines"] if e["name"] == "exa"][0]
    assert exa["content_max_characters"] == 800
    assert sum(a != b for a, b in zip(text.splitlines(), changed.splitlines(), strict=True)) == 1
    assert searxng.with_exa_length(text, 2000) == text


def test_with_exa_length_refuses_a_non_full_form_production() -> None:
    text = _example().replace("content_max_characters: 2000", "content_max_characters: 1500")
    with pytest.raises(searxng.SearxngSetupError, match="full form"):
        searxng.with_exa_length(text, 800)


def test_settings_copies_are_private_and_removed(tmp_path: Path) -> None:
    prod = tmp_path / "prod"
    prod.mkdir()
    (prod / "settings.yml").write_text(_example())
    (prod / "limiter.toml").write_text("")
    root = tmp_path / "copies"
    searxng.write_settings(root, prod)
    assert root.stat().st_mode & 0o777 == 0o700
    assert sorted(p.name for p in root.iterdir()) == sorted(str(n) for n in searxng.PORTS)
    searxng.remove_settings(root)
    assert not root.exists()


# ── AC-7: the harness imports the shaping code, it holds no copy ──────────

_SHARED = {
    "build_search_params",
    "shape_search_response",
    "download_text",
    "_extract_text",
    "split_passages",
    "bm25_order",
    "rank_passages",
    "select_within_budget",
    "focus_matched",
    "tokenize",
    "render_content",
}


def test_the_harness_defines_no_shaping_function() -> None:
    defined: set[str] = set()
    imported: set[str] = set()
    for path in HARNESS.glob("*.py"):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
                defined.add(node.name)
            if (
                isinstance(node, ast.ImportFrom)
                and node.module
                and node.module.startswith("personal_agent.tools")
            ):
                imported.update(a.name for a in node.names)
    assert not defined & _SHARED
    assert {
        "build_search_params",
        "shape_search_response",
        "download_text",
        "split_passages",
        "rank_passages",
        "select_within_budget",
        "focus_matched",
        "render_content",
    } <= imported


# ── vendor accounting and classes ─────────────────────────────────────────


def test_vendor_calls_follow_engines_then_categories() -> None:
    assert vendor_calls({"categories": "general"}) == (True, True)
    assert vendor_calls({"categories": "exa"}) == (True, False)
    assert vendor_calls({"categories": "science"}) == (False, False)
    assert vendor_calls({"categories": "general", "engines": "exa"}) == (True, False)
    assert vendor_calls({"categories": "it", "engines": "braveapi"}) == (False, True)


def test_task_class() -> None:
    assert task_class("quick") == "quick"
    assert task_class("standard") == task_class("thorough") == "standard_thorough"
    assert task_class(None) == "unknown"


# ── AC-3, AC-4, AC-5, AC-8: one scored run on synthetic fixtures ─────────


def _gz(path: Path, rows: list[dict[str, object]]) -> None:
    with gzip.open(path, "wt") as fh:
        for r in rows:
            fh.write(json.dumps(r) + "\n")


def _write_run(run: Path) -> None:
    filler = "Plain words about nothing in particular here. " * 30
    page = filler + "\nThe Nemotron model reached 159.714 tok/s on the M4 Max laptop.\n" + filler
    url = "https://example.com/bench"
    facts = {
        "helpers": [
            {
                "task_id": "t1",
                "trace_id": "x",
                "klass": "standard_thorough",
                "spec_task": "Nemotron decode speed on M4 Max",
            }
        ],
        "findings": [
            {
                "fid": "t1#0",
                "task_id": "t1",
                "trace_id": "x",
                "source_url": url,
                "claim": "Nemotron decodes 159.714 tok/s on the M4 Max.",
                "anchors": [
                    ["name", "Nemotron"],
                    ["name", "M4"],
                    ["name", "Max"],
                    ["number", "159.714"],
                ],
            }
        ],
        "replays": [
            {
                "replay_id": "q0000",
                "query": "nemotron m4",
                "categories": "general",
                "engines": None,
                "time_range": None,
            }
        ],
        "searches": [{"span_id": "s", "task_id": "t1", "replay_id": "q0000"}],
        "counts": {},
    }
    (run / "facts.json").write_text(json.dumps(facts))
    _gz(
        run / "fetch.jsonl.gz",
        [{"url": url, "status": 200, "chars_total": len(page), "text": page, "error": None}],
    )
    snippet = {500: "Nemotron on M4", 800: "Nemotron reached 159.714 tok/s on the M4 Max"}
    for length in searxng.PORTS:
        text = snippet.get(
            length, "Long excerpt. " * 10 + "Nemotron reached 159.714 tok/s on the M4 Max laptop."
        )
        data = {"results": [{"title": "t", "url": url, "content": text, "engine": "exa"}]}
        _gz(
            run / f"search_{length}.jsonl.gz",
            [{"replay_id": "q0000", "data": data, "error": None, "attempts": 1}],
        )
    order = list(range(len(split_passages(page))))
    arms = {
        a: {"order": order, "tokens": 0, "calls": 0, "sent": False, "latency_s": 0.01}
        for a in ("R1", "R2", "R3", "R4")
    }
    _gz(run / "ranks.jsonl.gz", [{"task_id": "t1", "url": url, "error": None, "arms": arms}])


def test_a_scored_run_keeps_the_full_form_and_fails_the_negatives(tmp_path: Path) -> None:
    _write_run(tmp_path)
    doc = analyze(tmp_path, usd_per_token=5e-8)
    fetch, search = doc["fetch"], doc["search"]
    assert fetch["supported"] == 1 and search["supported"] == 1
    assert fetch["full_form"].share == 1.0  # AC-4
    assert search["full_form"].share == 1.0  # AC-4
    assert fetch["negative_200"].share == 0.0  # AC-3
    assert search["negative_100"].share == 0.0  # AC-3
    cells = {(c.length, c.count): c for c in search["cells"]}
    assert len(cells) == 8 and len(fetch["cells"]) == 12  # AC-2
    assert cells[(500, 5)].pooled.share == 0.0
    assert cells[(800, 5)].pooled.share == 1.0
    # AC-5: one finding is far below the minimums, so nothing is selected.
    assert "sample below D5.5" in str(doc["blocked"])
    assert doc["search_selection"].cell is None
    assert doc["fetch_selection"].cell is None
    assert doc["code"]["commit"]  # AC-7


def test_an_unranked_page_blocks_the_selection(tmp_path: Path) -> None:
    """A page that the rank step missed must not count as a silent loss."""
    _write_run(tmp_path)
    (tmp_path / "ranks.jsonl.gz").unlink()
    doc = analyze(tmp_path, usd_per_token=5e-8)
    assert doc["fetch"]["pages_unranked"] == 1
    assert "no ranking" in str(doc["blocked"])


def test_a_vendor_engine_down_makes_a_replay_unusable() -> None:
    """Run r1: Exa and Brave ran out of credit mid-run; those replays must not be scored."""
    from scripts.eval.fre1641_anchor_retention.record import usable, vendor_failures

    down = {"results": [], "unresponsive_engines": [["exa", "Suspended: access denied"]]}
    assert vendor_failures(down) == ["exa: Suspended: access denied"]
    assert not usable({"data": down})
    assert usable(
        {"data": {"results": [], "unresponsive_engines": [["pubmed", "too many requests"]]}}
    )
    assert not usable({"data": None})


def test_an_unusable_replay_blocks_the_selection(tmp_path: Path) -> None:
    _write_run(tmp_path)
    path = tmp_path / "search_800.jsonl.gz"
    data = {"results": [], "unresponsive_engines": [["braveapi", "Suspended: access denied"]]}
    _gz(path, [{"replay_id": "q0000", "data": data, "error": None, "attempts": 1}])
    doc = analyze(tmp_path, usd_per_token=5e-8)
    assert doc["search"]["replay_errors"][800] == 1
    assert "without a usable response" in str(doc["blocked"])
