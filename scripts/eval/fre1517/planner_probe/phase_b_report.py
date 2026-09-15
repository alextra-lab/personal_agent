"""FRE-1521 Phase B report — three Flash-Next production runs, s1_trip turns 1-8. Read-only.

Runs: prod-arm3a (workers, current planner), prod-flash-po (no workers), prod-flash-brief (workers,
briefing planner). Reads the runner's JSONL rows only.

(1) per turn: delivered, wall, workers, each worker's report_kind / stop_reason / tool_iterations /
    thoroughness.
(2) brief carry-through on turns 2, 5, 8: in each worker's task text (sub_agent_start.task, or the
    capture's spec_task), does a task that needs a rule carry it. Needs: dining -> shellfish;
    prices -> euros; driving -> Sóller.
(3) answer compliance, turns 1-8, from the delivered reply: a reply that names a dish or a place to
    eat must state a shellfish status; a reply that states a price must use euros; a reply that
    gives drive times must start from Sóller.
(4) place-like names in worker tasks, for manual invented-place review.
Keyword scoring; spot-check before relying on it.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "out"
RUNS = [
    ("Flash workers (arm 3)", OUT / "prod-arm3a" / "mtplx_flash_next.s1_trip.r1.jsonl"),
    ("Flash no workers", OUT / "prod-flash-po" / "mtplx_flash_next.s1_trip.r1.jsonl"),
    ("Flash briefing", OUT / "prod-flash-brief" / "mtplx_flash_next.s1_trip.r1.jsonl"),
]

DINING_TASK = re.compile(
    r"(?i)\b(lunch|dinner|restaurants?|eat|dine|dining|menu|dessert|cuisine)\b"
)
PRICE_TASK = re.compile(r"(?i)\b(price|prices|cost|costs|fee|fees|ticket|admission|entry)\b")
DRIVE_TASK = re.compile(r"(?i)\b(driv\w*|distance|route|by car)\b")
SHELL = re.compile(r"(?i)(shellfish|seafood|crustacean|prawn|mollus)")
EURO = re.compile(r"(?i)(euro|\bEUR\b|€)")
SOLLER = re.compile(r"(?i)s[oó]ller")
PRICE_IN_REPLY = re.compile(
    r"(?i)(€\s?\d|\d\s?€|\d+\s?(?:euros?|EUR)\b|\$\s?\d|\d+\s?(?:USD|GBP|£))"
)
FOREIGN_PRICE = re.compile(r"(?i)(\$\s?\d|\d+\s?(?:USD|GBP)|£\s?\d)")
DINING_REPLY = re.compile(
    r"(?i)\b(restaurant|lunch|dinner|dish|menu|eat at|tapas|paella|ensa[iï]mada|dessert)\b"
)
DRIVE_REPLY = re.compile(
    r"(?i)\b(\d+\s?(?:min|minutes|h|hours?)\b.{0,40}\b(drive|driving)|drive\b.{0,60}\d+\s?(?:min|minutes))"
)
PLACE = re.compile(
    r"\b(?:[A-Z][\w'’àáèéíïòóúüç]+(?:\s(?:de|des|d'|del|sa|ses|es|s')?\s?[A-Z][\w'’àáèéíïòóúüç]+)+)"
)


def load(path: Path) -> dict[int, dict]:
    if not path.exists():
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {r["turn"]: r for r in rows if r["turn"] <= 8 and not r.get("http_error")}


def worker_tasks(row: dict) -> list[str]:
    tr = row.get("trace_reads") or {}
    starts = tr.get("sub_agent_starts") or []
    if starts:
        return [s.get("task") or "" for s in starts]
    return [c.get("spec_task") or "" for c in tr.get("sub_agent_captures") or []]


def carry(tasks: list[str]) -> dict[str, list[int]]:
    counts: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    for t in tasks:
        for rule, need, mark in (
            ("shellfish", DINING_TASK, SHELL),
            ("euros", PRICE_TASK, EURO),
            ("soller", DRIVE_TASK, SOLLER),
        ):
            if need.search(t):
                counts[rule][1] += 1
                counts[rule][0] += bool(mark.search(t))
    return counts


#: An explicit shellfish status, not a bare mention of the word.
SHELL_STATUS = re.compile(
    r"(?i)(shellfish[- ]free|no shellfish|without shellfish|shellfish[- ]safe|contains? shellfish|"
    r"shellfish[^.\n]{0,40}(not (?:verified|confirmed|certified)|unverified|unconfirmed|check|ask)|"
    r"(not|un)(?:verified|confirmed)[^.\n]{0,40}shellfish|shellfish\s*[:—-])"
)


def compliance(reply: str) -> dict[str, str]:
    """Score each rule per paragraph: "yes" only when every applicable paragraph follows it.

    A dining paragraph must state an explicit shellfish status; a paragraph with a price must not
    use another currency; a paragraph with drive times must name Sóller. Otherwise the value is
    "NO (followed/applicable)".
    """
    paragraphs = [p for p in re.split(r"\n\s*\n", reply) if p.strip()]
    checks = {
        "shellfish": [
            (p, bool(SHELL_STATUS.search(p))) for p in paragraphs if DINING_REPLY.search(p)
        ],
        "euros": [(p, not FOREIGN_PRICE.search(p)) for p in paragraphs if PRICE_IN_REPLY.search(p)],
        "soller": [(p, bool(SOLLER.search(p))) for p in paragraphs if DRIVE_REPLY.search(p)],
    }
    result = {}
    for rule, items in checks.items():
        if not items:
            continue
        held = sum(ok for _, ok in items)
        result[rule] = "yes" if held == len(items) else f"NO ({held}/{len(items)})"
    return result


def main() -> int:
    data = [(name, load(path)) for name, path in RUNS]
    print("## (1) Per turn\n")
    print("| Turn | " + " | ".join(n for n, _ in data) + " |")
    print("|---|" + "---|" * len(data))
    for n in range(1, 9):
        cells = []
        for _, rows in data:
            r = rows.get(n)
            if not r:
                cells.append("—")
                continue
            caps = (r.get("trace_reads") or {}).get("sub_agent_captures") or []
            workers = "; ".join(
                f"{c.get('report_kind')}/{c.get('stop_reason')}/{c.get('tool_iterations')}/{c.get('thoroughness', '?')}"
                for c in caps
            )
            cells.append(
                f"{'yes' if (r.get('outcome') or {}).get('delivered') else 'NO'}; {r.get('http_wall_s'):.0f} s; w {len(caps)}{' [' + workers + ']' if workers else ''}"
            )
        print(f"| {n} | " + " | ".join(cells) + " |")

    print("\n## (2) Brief carry-through, turns 2, 5, 8 (carried / tasks needing it)\n")
    for name, rows in data:
        total = defaultdict(lambda: [0, 0])
        per = []
        for n in (2, 5, 8):
            r = rows.get(n)
            if not r:
                continue
            c = carry(worker_tasks(r))
            for k, (a, b) in c.items():
                total[k][0] += a
                total[k][1] += b
            per.append(
                f"t{n}: "
                + (", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(c.items())) or "no workers")
            )
        tot = sum(a for a, _ in total.values()), sum(b for _, b in total.values())
        print(f"- {name}: {' | '.join(per)} → total {tot[0]}/{tot[1]}")

    print("\n## (3) Answer compliance, turns 1-8 (yes = rule followed where it applies)\n")
    print("| Turn | " + " | ".join(n for n, _ in data) + " |")
    print("|---|" + "---|" * len(data))
    tallies = {name: defaultdict(lambda: [0, 0]) for name, _ in data}
    for n in range(1, 9):
        cells = []
        for name, rows in data:
            r = rows.get(n)
            if not r:
                cells.append("—")
                continue
            comp = compliance(r.get("reply") or "")
            for k, v in comp.items():
                tallies[name][k][1] += 1
                tallies[name][k][0] += v == "yes"
            cells.append(", ".join(f"{k} {v}" for k, v in sorted(comp.items())) or "n/a")
        print(f"| {n} | " + " | ".join(cells) + " |")
    for name, t in tallies.items():
        print(f"- {name}: " + ", ".join(f"{k} {a}/{b}" for k, (a, b) in sorted(t.items())))

    print("\n## (4) Place-like names in worker tasks (manual review)\n")
    for name, rows in data:
        names = set()
        for n in (2, 5, 8):
            for t in worker_tasks(rows.get(n) or {}):
                names.update(PLACE.findall(t))
        print(f"- {name}: {sorted(names)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
