"""FRE-1521 Phase A scoring — from the plan text only, before any dispatch.

Per plan: parse, task count, types, thoroughness, and per task:
- needs/carries shellfish: a task about food, lunch, restaurants or dishes needs the rule;
  it carries it when goal or constraints mention shellfish/seafood/crustacean/allerg.
- needs/carries euros: a task that asks for prices, cost or entry fees needs it; carries it
  when the text says euro, EUR or the euro sign.
- needs/carries origin Sóller: a task asking for drive time, distance or route needs it; carries
  it when the text names Sóller/Soller.
- needs/carries turn-3 sites (turn 8 only): the site task needs it; carries it when it names a site
  from the session's turn-3 reply.
- bounded: the text states a count or an end point, and does not ask for all/every/each viable.
Hallucination review is manual: the script prints every capitalised place-like token per plan.
"""

import json
import re
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).parent
SITES_T3 = ["Ses Païsses", "Capocorb Vell", "Son Fornés", "Claper des Gegant", "Hospitalet Vell"]

FOOD = re.compile(
    # Only a task that recommends somewhere or something to eat needs the shellfish rule. Generic
    # "food" (e.g. "food/farmers markets" in an events search) does not make it a dining task.
    r"(?i)\b(lunch|dinner|restaurants?|eat|dine|dining|menu|dessert|cuisine)\b"
)
SHELL = re.compile(r"(?i)(shellfish|seafood|crustacean|allerg|mollus|prawn)")
PRICE = re.compile(r"(?i)\b(price|prices|cost|costs|fee|fees|ticket|admission|entry|budget)\b")
EURO = re.compile(r"(?i)(euro|\bEUR\b|€)")
DRIVE = re.compile(r"(?i)\b(driv\w*|distance|route|travel time|reachable|minutes? from|by car)\b")
SOLLER = re.compile(r"(?i)s[oó]ller")
SITE_TASK = re.compile(r"(?i)(archaeolog|talai|talay|site)")
UNBOUNDED = re.compile(r"(?i)\b(all|every|each viable|any relevant|as many)\b")
BOUNDED = re.compile(
    r"(?i)(\b\d+\s*(?:-|–|to)\s*\d+\b|\b(one|two|three|four|five|single|top \d+|up to \d+|at most \d+|\d+ (?:options|candidates|restaurants|sites|items|events))\b)"
)


def parse_plan(content: str) -> dict | None:
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.S)
    for candidate in (
        text,
        (re.search(r"\{.*\}", text, flags=re.S) or [None])[0]
        if re.search(r"\{.*\}", text, flags=re.S)
        else None,
    ):
        if not candidate:
            continue
        try:
            data = json.loads(candidate)
            if isinstance(data, dict):
                return data
        except json.JSONDecodeError:
            continue
    return None


def task_text(task: dict) -> str:
    constraints = task.get("constraints") or []
    if isinstance(constraints, str):
        constraints = [constraints]
    return f"{task.get('goal') or ''} {' '.join(str(c) for c in constraints)}"


def score(row: dict) -> dict:
    plan = parse_plan(row.get("content") or "")
    out = {"parsed": plan is not None, "tasks": 0, "types": [], "thoroughness": [], "places": set()}
    counts = defaultdict(lambda: [0, 0])  # rule -> [carried, needed]
    bounded = 0
    if not plan:
        return {**out, "counts": counts, "bounded": 0}
    tasks = [t for t in plan.get("tasks") or [] if isinstance(t, dict)]
    out["tasks"] = len(tasks)
    out["types"] = [t.get("type") for t in tasks]
    out["thoroughness"] = [t.get("thoroughness") for t in tasks]
    for t in tasks:
        text = task_text(t)
        goal = t.get("goal") or ""
        if FOOD.search(goal):
            counts["shellfish"][1] += 1
            counts["shellfish"][0] += bool(SHELL.search(text))
        if PRICE.search(goal):
            counts["euros"][1] += 1
            counts["euros"][0] += bool(EURO.search(text))
        if DRIVE.search(goal):
            counts["soller"][1] += 1
            counts["soller"][0] += bool(SOLLER.search(text))
        if row["turn"] == 8 and SITE_TASK.search(goal):
            counts["turn3_sites"][1] += 1
            counts["turn3_sites"][0] += any(s.lower() in text.lower() for s in SITES_T3)
        if BOUNDED.search(text) and not UNBOUNDED.search(goal):
            bounded += 1
        out["places"].update(
            re.findall(
                r"\b(?:[A-Z][\w'’àáèéíïòóúüç]+(?:\s(?:de|des|d'|del|sa|ses|es|s')?\s?[A-Z][\w'’àáèéíïòóúüç]+)+)",
                text,
            )
        )
    return {**out, "counts": counts, "bounded": bounded}


def main() -> int:
    rows = [
        json.loads(line)
        for line in (HERE / "probe_rows.jsonl").read_text().splitlines()
        if line.strip()
    ]
    agg = defaultdict(
        lambda: {
            "plans": 0,
            "parsed": 0,
            "tasks": 0,
            "bounded": 0,
            "wall": [],
            "rules": defaultdict(lambda: [0, 0]),
            "thor": defaultdict(int),
        }
    )
    places = defaultdict(set)
    for r in rows:
        key = (r["variant"], r["turn"])
        s = score(r)
        a = agg[key]
        a["plans"] += 1
        a["parsed"] += s["parsed"]
        a["tasks"] += s["tasks"]
        a["bounded"] += s["bounded"]
        a["wall"].append(r.get("wall_s") or 0)
        for rule, (c, n) in s["counts"].items():
            a["rules"][rule][0] += c
            a["rules"][rule][1] += n
        for th in s["thoroughness"]:
            a["thor"][th or "default"] += 1
        places[key].update(s["places"])
    print(
        "| Variant | Turn | Plans parsed | Tasks | Rule carry-through (carried/needed) | Bounded tasks | Thoroughness | Planner wall (s, per draw) |"
    )
    print("|---|---|---|---|---|---|---|---|")
    for key in sorted(agg):
        a = agg[key]
        rules = ", ".join(f"{k} {c}/{n}" for k, (c, n) in sorted(a["rules"].items())) or "—"
        thor = ", ".join(f"{k}×{v}" for k, v in sorted(a["thor"].items())) or "—"
        print(
            f"| {key[0]} | {key[1]} | {a['parsed']}/{a['plans']} | {a['tasks']} | {rules} | {a['bounded']}/{a['tasks']} | {thor} | {', '.join(str(w) for w in a['wall'])} |"
        )
    print()
    for variant in ("V0", "V1", "V2"):
        c = sum(agg[k]["rules"][r][0] for k in agg if k[0] == variant for r in agg[k]["rules"])
        n = sum(agg[k]["rules"][r][1] for k in agg if k[0] == variant for r in agg[k]["rules"])
        b = sum(agg[k]["bounded"] for k in agg if k[0] == variant)
        t = sum(agg[k]["tasks"] for k in agg if k[0] == variant)
        print(f"{variant}: carry-through {c}/{n}, bounded {b}/{t}")
    print("\nPlace-like names per variant/turn (manual hallucination review):")
    for key in sorted(places):
        print(f"  {key}: {sorted(places[key])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
