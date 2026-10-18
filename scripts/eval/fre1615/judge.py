"""FRE-1615 Block 1: blind judging of the arms' replies, the score per arm, and the owner's blind pairs.

The FRE-1598 method and code (``scripts/eval/fre1598/judge.py``): two judges label each reply PASS,
PARTIAL or FAIL against the task's ``expects.yaml`` line, blind to the arm; a third judge labels each
PASS/FAIL split, and its label replaces the FAIL. PASS is 2 points, PARTIAL 1, FAIL 0; a reply scores the
sum of its two final labels (0 to 4). The judges run on the ``study`` lane of the eval ledger.

A follow-up task's REQUEST shows the earlier turn as that arm's conversation held it (the first message
and the arm's own first reply), then the follow-up message.

    uv run python -m scripts.eval.fre1615.judge blind --run-dir <block> --rows <a/rows.jsonl> --rows <b/rows.jsonl>
    uv run python -m scripts.eval.fre1615.judge label --run-dir <block> --judge 1    (then 2, then 3)
    uv run python -m scripts.eval.fre1615.judge score --run-dir <block>
    uv run python -m scripts.eval.fre1615.judge owner --run-dir <block> --arms S,PL

``owner`` writes ``owner_blind.md`` (each task's two replies as A and B, in a seeded random order) and
``owner_key.json``. The owner scores the markdown without the key.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import cast

import yaml  # type: ignore[import-untyped]
from scripts.eval.fre1590 import help as part2
from scripts.eval.fre1598 import judge as j1598

HERE = Path(__file__).resolve().parent
EXPECTS = HERE / "expects.yaml"
OWNER_SEED = 20261018


def request_text(row: Mapping[str, object]) -> str:
    """Return the REQUEST a judge sees: the message, after the earlier turn of a follow-up task."""
    first = row.get("first_turn")
    if not isinstance(first, dict):
        return str(row["message"])
    history = str(row.get("history_user") or "")
    return (
        f"(Earlier in this conversation)\nUSER: {history}\nASSISTANT: {first.get('reply')}\n\n"
        f"(The message to grade the reply to)\nUSER: {row['message']}"
    )


def judge_rows(
    rows: Sequence[Mapping[str, object]],
    expects: Mapping[str, str],
    history: Mapping[str, str],
) -> list[dict[str, object]]:
    """Return the kept rows in the shape ``fre1598.judge.blind`` reads (``prompt``, ``expect``).

    Raises:
        KeyError: A task has no expect line.
    """
    out: list[dict[str, object]] = []
    for row in j1598.kept_rows(rows):
        task = str(row["id"])
        merged = {**row, "history_user": history.get(task, "")}
        out.append({**row, "prompt": request_text(merged), "expect": expects[task]})
    return out


def score(
    key: Mapping[str, Mapping[str, str]],
    labels: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    """Score each reply and sum the points per arm and per (task, arm).

    Returns:
        ``replies`` (labels, final pair, points), ``points`` (task → arm → points), ``totals`` and
        ``passes`` per arm (both final labels PASS), and ``missing`` (replies without a full label set).
    """

    def valid(judge: str, rid: str) -> str | None:
        label = labels.get(judge, {}).get(rid, {}).get("label")
        return str(label) if label in j1598.LABELS else None

    splits = set(j1598.split_rids(labels.get("1", {}), labels.get("2", {})))
    replies: list[dict[str, object]] = []
    points: dict[str, dict[str, int]] = {}
    missing: list[str] = []
    for rid in sorted(key):
        a, b = valid("1", rid), valid("2", rid)
        final = [a, b]
        if rid in splits:
            third = valid("3", rid)
            final = ["PASS", third]
        entry: dict[str, object] = {
            "rid": rid,
            **key[rid],
            "judge_1": a,
            "judge_2": b,
            "final": final,
        }
        if rid in splits:
            entry["judge_3"] = final[1]
        if any(f is None for f in final):
            missing.append(rid)
        else:
            entry["points"] = sum(j1598.POINTS[str(f)] for f in final)
            points.setdefault(key[rid]["id"], {})[key[rid]["arm"]] = cast(int, entry["points"])
        replies.append(entry)
    arms = sorted({k["arm"] for k in key.values()})
    return {
        "replies": replies,
        "points": points,
        "totals": {arm: sum(p.get(arm, 0) for p in points.values()) for arm in arms},
        "passes": {
            arm: sum(1 for r in replies if r["arm"] == arm and r["final"] == ["PASS", "PASS"])
            for arm in arms
        },
        "splits": sorted(splits),
        "missing": missing,
    }


def owner_pairs(
    rows: Sequence[Mapping[str, object]],
    arms: Sequence[str],
    history: Mapping[str, str],
    seed: int = OWNER_SEED,
) -> tuple[str, dict[str, dict[str, str]]]:
    """Return the owner's blind markdown (two replies per task as A and B) and its key.

    Raises:
        ValueError: ``arms`` is not two arms, or a task lacks one of them.
    """
    if len(arms) != 2:
        raise ValueError("the owner's pairs need exactly two arms")
    kept = {(str(r["id"]), str(r["arm"])): r for r in j1598.kept_rows(rows)}
    tasks: list[str] = []
    for r in rows:
        if str(r["id"]) not in tasks:
            tasks.append(str(r["id"]))
    rng = random.Random(seed)  # nosec B311 — a blinding order, not a secret
    # Each arm takes slot A on half the tasks (random which), so the slot says nothing about the arm.
    first_arm_a = set(rng.sample(tasks, len(tasks) // 2))
    lines = [
        "# FRE-1615 Block 1 — blind pairs",
        "",
        "For each task, score A and B (PASS, PARTIAL or FAIL), or pick the better one.",
        "",
    ]
    key: dict[str, dict[str, str]] = {}
    for task in tasks:
        if any((task, arm) not in kept for arm in arms):
            raise ValueError(f"task {task} lacks an arm of {list(arms)}")
        order = list(arms) if task in first_arm_a else list(reversed(arms))
        key[task] = {"A": order[0], "B": order[1]}
        first = kept[(task, order[0])]
        lines += [
            f"## {task}",
            "",
            "**Request**",
            "",
            request_text({**first, "history_user": history.get(task, "")}),
            "",
        ]
        for slot, arm in zip("AB", order, strict=True):
            lines += [f"### {slot}", "", str(kept[(task, arm)]["reply"]), ""]
    return "\n".join(lines) + "\n", key


def _history() -> dict[str, str]:
    from scripts.eval.fre1615.capture import load_suite

    return {str(t["id"]): str(t.get("history_user") or "") for t in load_suite()}


def _rows(paths: Sequence[str]) -> list[dict[str, object]]:
    return [r for p in paths for r in part2.read_rows(Path(p))]


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then blind, label, score, or write the owner's pairs.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("blind", "label", "score", "owner"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--run-dir", required=True)
        if name in ("blind", "owner"):
            cmd.add_argument("--rows", action="append", required=True)
        if name == "label":
            cmd.add_argument("--judge", required=True, choices=("1", "2", "3"))
        if name == "owner":
            cmd.add_argument("--arms", required=True)
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir)
    jdir = j1598.judging_dir(run_dir)
    if args.command == "blind":
        if (jdir / "items.jsonl").exists():
            raise SystemExit(f"{jdir}/items.jsonl exists: blinding runs once per run")
        expects = yaml.safe_load(EXPECTS.read_text())["expects"]
        items, key = j1598.blind(judge_rows(_rows(args.rows), expects, _history()))
        j1598.write_jsonl(jdir / "items.jsonl", items)
        (jdir / "key.json").write_text(json.dumps(key, indent=2) + "\n")
        print(f"{len(items)} replies blinded -> {jdir}")
        return 0
    if args.command == "label":
        return asyncio.run(j1598._label(run_dir, args.judge))
    if args.command == "score":
        key = json.loads((jdir / "key.json").read_text())
        labels = {n: j1598.read_labels(jdir / f"labels-{n}.jsonl") for n in j1598.JUDGES}
        report = score(key, labels)
        report["judge_cost_usd"] = round(
            sum(
                float(cast(float, r.get("cost_usd") or 0.0))
                for n in j1598.JUDGES
                for r in part2.read_rows(jdir / f"labels-{n}.jsonl")
            ),
            6,
        )
        (jdir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
        print(
            json.dumps(
                {k: report[k] for k in ("totals", "passes", "splits", "missing", "judge_cost_usd")},
                indent=2,
            )
        )
        return 1 if report["missing"] else 0
    text, key = owner_pairs(_rows(args.rows), [a.strip() for a in args.arms.split(",")], _history())
    (run_dir / "owner_blind.md").write_text(text)
    (run_dir / "owner_key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(key)} pairs -> {run_dir / 'owner_blind.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
