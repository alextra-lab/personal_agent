"""FRE-1597 / ADR-0156 A5 (T-D1): FRE-1590 Part 1 with a second step, on the final build (AC-1).

The items are the ``evals.yaml`` scenarios of the seven area skills (A4) and the 20 no-skill
prompts of FRE-1581.

    capture  one probe-gateway turn per item. A scenario item scripts the stub to load its expected
             skill, so the gateway also sends call 2 (``calls.py``). No model is called.
                                                     -> <run>/routing/captured/<safe id>.json
    replay   per draw and item: call 1 to llama.cpp; then call 2 only when call 1 is exactly one
             ``read_skill`` of the expected skill. No tool runs.
                                                     -> <run>/routing/rows.jsonl
    score    the AC-1 verdict and the second-step table   -> <run>/routing/report.json

Run from the repo root. See README.md beside this file.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537.common import append_jsonl, done_pair_keys, read_jsonl, utc_now
from scripts.eval.fre1581.g2_skill_choice import Item, load_items
from scripts.eval.fre1590.triggering import classify, safe_name
from scripts.eval.fre1594 import spike
from scripts.eval.fre1594.transport import LlamaTransport, Reply, Transport
from scripts.eval.fre1597 import common
from scripts.eval.fre1597.calls import FidelityError, TwoCalls, skip_reason

from personal_agent.llm_client.types import ToolCall

# ADR-0156 A4: the skills of the telemetry area, and graph-inspection.
AREA_SKILLS: tuple[str, ...] = (
    "graph-inspection",
    "interaction-outcomes",
    "log-search",
    "model-latency",
    "service-health",
    "spend-and-tokens",
    "turn-explanation",
)
# AC-F4 routing: 10 of 12 draws (4 scenarios x 3 draws). A skill with another number of scenarios
# keeps the share, rounded up (owner decision D-1 of the FRE-1597 preflight).
SHARE = (10, 12)


@dataclass(frozen=True)
class Captured:
    """One item and its captured calls."""

    item: Item
    calls: TwoCalls


@dataclass(frozen=True)
class RoutingPaths:
    """The routing files of a run directory."""

    run_dir: Path

    @property
    def root(self) -> Path:
        """Return the routing directory."""
        return self.run_dir / "routing"

    @property
    def captured(self) -> Path:
        """Return the directory of the captured items."""
        return self.root / "captured"

    @property
    def rows(self) -> Path:
        """Return the rows file."""
        return self.root / "rows.jsonl"

    @property
    def fingerprint(self) -> Path:
        """Return the fingerprint file."""
        return self.root / "fingerprint.json"

    @property
    def warmup(self) -> Path:
        """Return the warm-up rows file."""
        return self.root / "warmup.jsonl"

    @property
    def report(self) -> Path:
        """Return the report file."""
        return self.root / "report.json"

    def captured_file(self, item_id: str) -> Path:
        """Return the capture file of one item."""
        return self.captured / f"{safe_name(item_id)}.json"


def load_area_items() -> list[Item]:
    """Return the scenarios of the area skills, then the no-skill prompts."""
    return [i for i in load_items() if i.kind == "no_skill" or i.expected_skill in AREA_SKILLS]


def need(draws: int) -> int:
    """Return the ``first`` count that a skill with ``draws`` draws needs."""
    return math.ceil(draws * SHARE[0] / SHARE[1])


# --------------------------------------------------------------------------- capture


def run_capture(
    client: httpx.Client, chat_url: str, paths: RoutingPaths, items: Sequence[Item], model_key: str
) -> int:
    """Capture every item that has no capture file yet.

    Returns:
        The number of items that failed. A failed item has no file, so a rerun retries it.
    """
    build = common.gateway_build(chat_url)
    failed = 0
    for item in items:
        target = paths.captured_file(item.id)
        if target.exists():
            continue
        skill = item.expected_skill if item.kind == "scenario" else None
        try:
            two = common.capture_turn(
                client, chat_url, paths.run_dir, item.prompt, model_key=model_key, skill=skill
            )
        except (httpx.HTTPError, FidelityError, ValueError) as exc:
            failed += 1
            print(item.id, "capture failed:", repr(exc)[:300], file=sys.stderr, flush=True)
            continue
        meta = {
            "id": item.id,
            "kind": item.kind,
            "expected_skill": item.expected_skill,
            "prompt": item.prompt,
            "model_key": model_key,
            "gateway_build": build,
            "captured_at": utc_now(),
        }
        common.save_capture(target, two, meta)
        print(item.id, "captured", list(two.added_tools), flush=True)
    return failed


def load_captured(paths: RoutingPaths, items: Sequence[Item]) -> list[Captured]:
    """Load the captures of the items that have one, in item order."""
    out: list[Captured] = []
    for item in items:
        path = paths.captured_file(item.id)
        if path.exists():
            out.append(Captured(item, common.load_capture(path)[1]))
    return out


# --------------------------------------------------------------------------- replay


def _arguments(call: ToolCall) -> object:
    try:
        return json.loads(call["arguments"])
    except (json.JSONDecodeError, TypeError):
        return None


def skill_loads(tool_calls: Iterable[ToolCall]) -> tuple[list[str], list[str]]:
    """Return the skill names of the ``read_skill`` calls, and the raw arguments without one."""
    names: list[str] = []
    malformed: list[str] = []
    for call in tool_calls:
        if call["name"] != "read_skill":
            continue
        args = _arguments(call)
        name = args.get("name") if isinstance(args, Mapping) else None
        if isinstance(name, str) and name:
            names.append(name)
        else:
            malformed.append(str(call["arguments"]))
    return names, malformed


def first_row(item: Item, draw: int, reply: Reply) -> dict[str, object]:
    """Return the row of call 1 of one draw: the Part 1 fields and the full reply record."""
    names, malformed = skill_loads(reply.tool_calls)
    row: dict[str, object] = {
        "id": item.id,
        "kind": item.kind,
        "expected_skill": item.expected_skill,
        "draw": draw,
        "ts": utc_now(),
        "tool_names": [c["name"] for c in reply.tool_calls],
        "read_skill_names": names,
        "read_skill_malformed": malformed,
        "first": reply.record(),
    }
    if not reply.ok:
        row["error"] = reply.error or "call 1 failed"
    return row


def second_record(two: TwoCalls, reply: Reply) -> dict[str, object]:
    """Return the row field of call 2."""
    names = [c["name"] for c in reply.tool_calls]
    return {
        "revealed": list(two.added_tools),
        "tool_names": names,
        "called_revealed": any(n in two.added_tools for n in names),
        **reply.record(),
    }


def run_replay(
    sender: Transport, captured: Sequence[Captured], rows_path: Path, *, draws: int
) -> int:
    """Send call 1 of each item once per draw, and call 2 where it applies.

    A rerun skips each ``(id, draw)`` that has a row without an error.

    Returns:
        The number of error rows that this run wrote.
    """
    done = done_pair_keys(rows_path, "id")
    errors = 0
    for draw in range(draws):
        for entry in captured:
            if (entry.item.id, draw) in done:
                continue
            reply = sender.send(entry.calls.first)
            row = first_row(entry.item, draw, reply)
            if "error" in row:
                errors += 1
            else:
                reason = skip_reason(entry.calls, reply)
                if reason is None:
                    second = sender.send(entry.calls.second_request(reply))
                    row["second"] = second_record(entry.calls, second)
                    if not second.ok:
                        # A failed call 2 is not a text reply: the row is an error, so a rerun
                        # sends the draw again.
                        row["error"] = f"call 2 failed: {second.error}"
                        errors += 1
                else:
                    row["second"] = {"skipped": reason}
            append_jsonl(rows_path, row)
            second_field = row.get("second")
            print(
                "draw",
                draw,
                entry.item.id,
                row["tool_names"],
                second_field.get("tool_names") if isinstance(second_field, Mapping) else "-",
                str(row.get("error", ""))[:80],
                flush=True,
            )
    return errors


# --------------------------------------------------------------------------- score


def _calls(row: Mapping[str, object]) -> list[ToolCall]:
    first = row.get("first")
    calls = first.get("tool_calls") if isinstance(first, Mapping) else None
    return list(calls) if isinstance(calls, list) else []


@dataclass
class _Tally:
    """The counts of one area skill while the rows are read."""

    scenarios: int = 0
    draws: int = 0
    first: int = 0
    right: int = 0
    second_sent: int = 0
    second_called_revealed: int = 0
    outside_loads: Counter[str] = field(default_factory=Counter)
    second_first_tool: Counter[str] = field(default_factory=Counter)
    second_skipped: Counter[str] = field(default_factory=Counter)

    def add_second(self, second: object) -> None:
        """Count the second step of one draw."""
        if not isinstance(second, Mapping):
            return
        if "skipped" in second:
            self.second_skipped[str(second["skipped"])] += 1
            return
        self.second_sent += 1
        self.second_called_revealed += bool(second.get("called_revealed"))
        tools = second.get("tool_names")
        self.second_first_tool[
            str(tools[0]) if isinstance(tools, list) and tools else "(text)"
        ] += 1

    def report(self, draws: int) -> dict[str, object]:
        """Return the report entry, with the bound for this skill's number of scenarios."""
        bound = need(self.scenarios * draws)
        return {
            "scenarios": self.scenarios,
            "draws": self.draws,
            "first": self.first,
            "right": self.right,
            "need": bound,
            "pass": self.first >= bound,
            "literal_ten": self.first >= SHARE[0],
            "outside_loads": dict(sorted(self.outside_loads.items())),
            "second_sent": self.second_sent,
            "second_called_revealed": self.second_called_revealed,
            "second_first_tool": dict(sorted(self.second_first_tool.items())),
            "second_skipped": dict(sorted(self.second_skipped.items())),
        }


def is_first(calls: Sequence[ToolCall], expected: str) -> bool:
    """Return True when the first tool call loads ``expected`` and no other skill loads."""
    names, malformed = skill_loads(calls)
    if not calls or calls[0]["name"] != "read_skill" or malformed:
        return False
    return bool(names) and names[0] == expected and set(names) == {expected}


def score(
    items: Sequence[Item], rows: Sequence[Mapping[str, object]], draws: int
) -> dict[str, object]:
    """Build the AC-1 report.

    Args:
        items: Every item.
        rows: The routing rows.
        draws: Draws per item.

    Returns:
        ``skills`` (per area skill: scenarios, draws, ``first``, the Part 1 ``right``, ``need``,
        ``pass``, the skills outside the area that loaded, the second-step counts),
        ``area_confusion``, ``no_skill_draws``, ``no_skill_loads``, ``incomplete``,
        ``llama_builds`` and ``verdict`` (``pass``, ``fail`` or ``incomplete``).
    """
    kept: dict[str, dict[int, Mapping[str, object]]] = {}
    for row in rows:
        draw = int(str(row["draw"]))
        if "error" not in row and draw < draws:
            kept.setdefault(str(row["id"]), {})[draw] = row

    tallies: dict[str, _Tally] = {}
    for item in items:
        if item.kind == "scenario":
            tallies.setdefault(str(item.expected_skill), _Tally()).scenarios += 1
    confusion: list[dict[str, object]] = []
    incomplete: list[str] = []
    no_skill_draws = no_skill_loads = 0
    builds: set[str] = set()
    for item in items:
        done = kept.get(item.id, {})
        if len(done) < draws:
            incomplete.append(f"{item.id}: {len(done)} of {draws} draws")
        for draw, row in sorted(done.items()):
            for call_record in (row.get("first"), row.get("second")):
                if isinstance(call_record, Mapping) and call_record.get("system_fingerprint"):
                    builds.add(str(call_record["system_fingerprint"]))
            calls = _calls(row)
            names, malformed = skill_loads(calls)
            if item.kind == "no_skill":
                no_skill_draws += 1
                no_skill_loads += bool(names or malformed)
                continue
            expected = str(item.expected_skill)
            tally = tallies[expected]
            tally.draws += 1
            tally.first += is_first(calls, expected)
            tally.right += classify(item.kind, expected, names, malformed) == "right"
            others = [n for n in names if n != expected]
            if wrong_area := [n for n in others if n in AREA_SKILLS]:
                confusion.append({"id": item.id, "draw": draw, "loaded": wrong_area})
            tally.outside_loads.update(n for n in others if n not in AREA_SKILLS)
            tally.add_second(row.get("second"))

    skills = {name: tallies[name].report(draws) for name in sorted(tallies)}
    if incomplete:
        verdict = "incomplete"
    elif (
        all(s["pass"] for s in skills.values())
        and not confusion
        and no_skill_loads == 0
        and len(builds) == 1
    ):
        verdict = "pass"
    else:
        verdict = "fail"
    return {
        "draws": draws,
        "share": f"{SHARE[0]} of {SHARE[1]}, rounded up",
        "skills": skills,
        "area_confusion": confusion,
        "no_skill_draws": no_skill_draws,
        "no_skill_loads": no_skill_loads,
        "incomplete": incomplete,
        "llama_builds": sorted(builds),
        "verdict": verdict,
    }


def render(report: Mapping[str, object]) -> str:
    """Render the AC-1 report as text."""
    lines = [f"FRE-1597 AC-1 (routing): {report['verdict']}", ""]
    lines.append(
        f"  {'skill':<22} {'draws':>5} {'first':>5} {'need':>4} {'pass':>5} {'right':>5} "
        f"{'call2':>5} {'revealed':>8}  outside loads"
    )
    skills = report["skills"]
    assert isinstance(skills, Mapping)
    for name, e in skills.items():
        assert isinstance(e, Mapping)
        outside = e.get("outside_loads") or {}
        assert isinstance(outside, Mapping)
        lines.append(
            f"  {name:<22} {e.get('draws', 0):>5} {e['first']:>5} {e['need']:>4} "
            f"{e['pass']!s:>5} {e.get('right', 0):>5} {e.get('second_sent', 0):>5} "
            f"{e.get('second_called_revealed', 0):>8}  "
            + (", ".join(f"{k} x{v}" for k, v in outside.items()) or "-")
        )
    lines += [
        "",
        f"  area confusion: {report['area_confusion'] or 'none'}",
        f"  no-skill loads: {report['no_skill_loads']} of {report['no_skill_draws']} draws",
        f"  llama.cpp builds: {report['llama_builds']}",
        f"  incomplete: {report['incomplete'] or 'none'}",
    ]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def _select(items: Sequence[Item], limit: int) -> Sequence[Item]:
    return items[:limit] if limit else items


def _cmd_capture(args: argparse.Namespace) -> int:
    paths = RoutingPaths(args.run_dir)
    items = _select(load_area_items(), args.limit)
    with httpx.Client() as client:
        failed = run_capture(client, args.chat_url, paths, items, args.model)
    print(f"captured {len(load_captured(paths, items))} of {len(items)} items in {paths.captured}")
    return 1 if failed else 0


def _cmd_replay(args: argparse.Namespace) -> int:
    paths = RoutingPaths(args.run_dir)
    captured = load_captured(paths, _select(load_area_items(), args.limit))
    if not captured:
        raise SystemExit(f"no captures in {paths.captured}: run capture first")
    client = httpx.Client(timeout=httpx.Timeout(900.0, connect=10.0))
    try:
        records = [common.load_capture(paths.captured_file(c.item.id))[0] for c in captured]
        fingerprint = {
            **common.local_pins(args, client),
            **common.request_pins([c.calls.first for c in captured]),
            "items": {c.item.id: common.sha256(c.item.prompt.encode()) for c in captured},
            "gateway_builds": sorted({str(r.get("gateway_build")) for r in records}),
            "draws": args.draws,
            "warmup": common.WARMUP,
            "git_head": spike._git_head(),
        }
        spike.ensure_fingerprint(paths.fingerprint, fingerprint)
        sender = common.RetryOnce(LlamaTransport(client, args.url, args.llama_model))
        common.warm_up(sender, captured[0].calls.first, paths.warmup)
        errors = run_replay(sender, captured, paths.rows, draws=args.draws)
    finally:
        client.close()
    print(f"rows in {paths.rows}; {errors} error rows this run. Next: score")
    return 1 if errors else 0


def _cmd_score(args: argparse.Namespace) -> int:
    paths = RoutingPaths(args.run_dir)
    if not paths.fingerprint.exists():
        raise SystemExit(f"{paths.fingerprint} is missing: run replay first")
    draws = int(json.loads(paths.fingerprint.read_text())["draws"])
    report = score(load_area_items(), read_jsonl(paths.rows), draws)
    paths.report.write_text(json.dumps(report, indent=2))
    sys.stdout.write(render(report))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step: capture, replay or score."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "replay", "score"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        if name == "score":
            continue
        p.add_argument("--limit", type=int, default=0, help="first N items only (a dry run)")
        if name == "capture":
            p.add_argument("--chat-url", default=capture.DEFAULT_CHAT_URL)
            p.add_argument("--model", default=common.DEFAULT_MODEL_KEY, help="the catalog key")
        else:
            common.add_local_args(p)
            p.add_argument("--draws", type=int, default=common.DEFAULT_DRAWS)
    args = parser.parse_args(argv)
    handlers = {"capture": _cmd_capture, "replay": _cmd_replay, "score": _cmd_score}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
