"""FRE-1598 (ADR-0156 AC-F4 benefit): blind judging of the benefit run's replies, and the score.

The ticket's method (as FRE-1590 F3):

- Two judges, blind to the arm, label each reply PASS, PARTIAL or FAIL against the scenario's
  ``expect`` text. PASS is 2 points, PARTIAL 1, FAIL 0. An arm's score on a scenario is the sum of
  both judges' points.
- When the two judges give one reply PASS and FAIL, a third judge, also blind, labels it, and its
  label replaces the lower label (the FAIL).
- AC-1: with skills, each scenario scores at least what it scores in the control arm.
- AC-2: every reply has a label from both judges (and every PASS/FAIL split one third label).

Blinding: each call sees one reply alone, with the user prompt and the ``expect`` text. It never
sees the arm, the tools, the trace or the other reply. The replies are shuffled with a fixed seed and
named by an opaque id; the id → (scenario, arm) key is a separate file.

The judges are called through the agent's LLM client on the ``study`` budget lane (ADR-0141 AC-6
confines model dispatch to ``llm_client/``). Each label row records the cost that the client returns.

    uv run python -m scripts.eval.fre1598.judge blind --run-dir <run>
    uv run python -m scripts.eval.fre1598.judge label --run-dir <run> --judge 1
    uv run python -m scripts.eval.fre1598.judge label --run-dir <run> --judge 2
    uv run python -m scripts.eval.fre1598.judge third --run-dir <run>
    uv run python -m scripts.eval.fre1598.judge score --run-dir <run>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, cast, get_args

from scripts.eval.fre1590 import help as part2
from scripts.eval.fre1598.run import MODEL_KEY, catalog_entry
from scripts.eval.fre1598.seed import EVAL_DB_PORT

Label = Literal["PASS", "PARTIAL", "FAIL"]
LABELS: tuple[Label, ...] = get_args(Label)
POINTS: dict[str, int] = {"PASS": 2, "PARTIAL": 1, "FAIL": 0}

#: Judge number → catalog key (config/models.yaml). The owner confirms them in the preflight.
JUDGES: dict[str, str] = {"1": "claude_sonnet", "2": "gpt-5.4-mini", "3": "claude_haiku"}
BLIND_SEED = 20261013
MAX_TOKENS = 4000  # room for a reasoning model's hidden tokens before the short JSON answer

JUDGE_SYSTEM = """\
You grade one reply of an AI assistant. You get the user's REQUEST, the EXPECTED behaviour, and the \
REPLY.

Labels:
- PASS: the reply does what EXPECTED describes, in full.
- PARTIAL: the reply does part of what EXPECTED describes, or does it with a mistake that the user \
would notice.
- FAIL: the reply does not do what EXPECTED describes, or does what EXPECTED rules out.

Rules:
- Grade against EXPECTED only. Do not grade style, length or tone.
- The reply is your only evidence. A step that the reply does not show or state counts as not done.
- A reply that reports a failed command, and states the error plainly, can still do what EXPECTED \
describes, if EXPECTED is about the method and not about the result.

Answer with JSON only, no other text: {"label": "PASS" | "PARTIAL" | "FAIL", "reason": "<one sentence>"}
"""

JUDGE_USER = "REQUEST:\n{prompt}\n\nEXPECTED:\n{expect}\n\nREPLY:\n{reply}\n"


@dataclass(frozen=True)
class JudgeReply:
    """One judge call's text and usage."""

    text: str
    input_tokens: int
    output_tokens: int
    cost_usd: float


#: A judge: (system prompt, user message) → reply.
JudgeFn = Callable[[str, str], Awaitable[JudgeReply]]


def judging_dir(run_dir: Path) -> Path:
    """Return the run's judging directory."""
    return run_dir / "judging"


def write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """Write rows as JSONL, replacing the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in rows))


def kept_rows(rows: Sequence[Mapping[str, object]]) -> list[Mapping[str, object]]:
    """Return the last row without an error for each (id, arm), in first-seen order."""
    kept: dict[tuple[str, str], Mapping[str, object]] = {}
    for r in rows:
        if "error" not in r:
            kept[(str(r["id"]), str(r["arm"]))] = r
    return list(kept.values())


def blind(
    rows: Sequence[Mapping[str, object]], seed: int = BLIND_SEED
) -> tuple[list[dict[str, str]], dict[str, dict[str, str]]]:
    """Shuffle the replies and give each an opaque id.

    Args:
        rows: The run's rows (``part2.jsonl``).
        seed: The shuffle seed.

    Returns:
        ``items`` (``rid``, ``prompt``, ``expect``, ``reply``: nothing that names the arm) and the
        ``key`` (``rid`` → ``id``, ``arm``, ``trace_id``).
    """
    kept = kept_rows(rows)
    order = list(range(len(kept)))
    random.Random(seed).shuffle(order)  # nosec B311 — a blinding order, not a secret
    items: list[dict[str, str]] = []
    key: dict[str, dict[str, str]] = {}
    for n, index in enumerate(order, start=1):
        row = kept[index]
        rid = f"r{n:02d}"
        items.append(
            {
                "rid": rid,
                "prompt": str(row["prompt"]),
                "expect": str(row["expect"]),
                "reply": str(row["reply"]),
            }
        )
        key[rid] = {
            "id": str(row["id"]),
            "arm": str(row["arm"]),
            "trace_id": str(row.get("trace_id")),
        }
    return items, key


def parse_label(text: str) -> tuple[Label, str]:
    """Read the judge's JSON answer.

    Args:
        text: The judge's reply text.

    Returns:
        The label and the reason.

    Raises:
        ValueError: No JSON object, or no valid label.
    """
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match is None:
        raise ValueError(f"no JSON object in {text[:120]!r}")
    data = json.loads(match.group(0))
    label = str(data.get("label", "")).strip().upper() if isinstance(data, dict) else ""
    if label not in LABELS:
        raise ValueError(f"no valid label in {text[:120]!r}")
    return cast(Label, label), str(data.get("reason", ""))


def judge_pins(keys: Mapping[str, str] = JUDGES) -> dict[str, object]:
    """Return each judge's catalog key and resolved model, and the prompt's hash."""
    judges: dict[str, object] = {}
    for number, key in keys.items():
        entry = catalog_entry(key)
        judges[number] = {"key": key, "provider": entry["provider"], "model_id": entry["id"]}
    return {
        "judges": judges,
        "system_sha256": part2.sha256_text(JUDGE_SYSTEM),
        "user_template_sha256": part2.sha256_text(JUDGE_USER),
        "blind_seed": BLIND_SEED,
        "max_tokens": MAX_TOKENS,
    }


def client_judge(key: str) -> JudgeFn:
    """Build a judge that calls the catalog deployment ``key`` through the agent's LLM client.

    The call bills the ``study`` budget lane (FRE-839), as the FRE-994 judge does, so a cap
    denial stops the run loudly. No temperature is sent: each judge runs at its catalog defaults.

    Args:
        key: The catalog key.

    Returns:
        The judge function.
    """
    from personal_agent.llm_client import ModelRole  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415
    from personal_agent.telemetry.trace import SystemTraceContext  # noqa: PLC0415

    client = get_llm_client_for_key(key, budget_role="study")
    # One session id per judge process, so the client writes each call's api_costs row.
    session_id = str(uuid.uuid4())

    async def call(system: str, user: str) -> JudgeReply:
        resp = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": user}],
            system_prompt=system,
            max_tokens=MAX_TOKENS,
            trace_ctx=SystemTraceContext.new("fre1598_judge", session_id=session_id),
        )
        usage = resp.get("usage") or {}
        return JudgeReply(
            text=str(resp.get("content") or ""),
            input_tokens=int(usage.get("prompt_tokens") or 0),
            output_tokens=int(usage.get("completion_tokens") or 0),
            cost_usd=float(resp.get("cost_usd") or 0.0),
        )

    return call


def read_labels(path: Path) -> dict[str, dict[str, object]]:
    """Read a judge's labels: the last row of each ``rid``."""
    return {str(r["rid"]): r for r in part2.read_rows(path)}


async def label_items(
    items: Sequence[Mapping[str, str]], judge: str, call: JudgeFn, out: Path
) -> list[dict[str, object]]:
    """Label each item that has no valid label yet; append each label as it lands.

    A reply that does not parse is asked once more. A second failure writes an ``error`` row,
    which AC-2 counts as a missing label.

    Args:
        items: The blinded items.
        judge: The judge number.
        call: The judge function.
        out: The judge's labels file.

    Returns:
        The rows written by this call.
    """
    done = {rid for rid, r in read_labels(out).items() if "label" in r}
    key = JUDGES[judge]
    written: list[dict[str, object]] = []
    out.parent.mkdir(parents=True, exist_ok=True)
    for item in items:
        if item["rid"] in done:
            continue
        user = JUDGE_USER.format(prompt=item["prompt"], expect=item["expect"], reply=item["reply"])
        row: dict[str, object] = {"rid": item["rid"], "judge": judge, "key": key, "cost_usd": 0.0}
        for attempt in (1, 2):
            reply = await call(JUDGE_SYSTEM, user)
            row["cost_usd"] = round(cast(float, row["cost_usd"]) + reply.cost_usd, 6)
            row["attempts"] = attempt
            try:
                row["label"], row["reason"] = parse_label(reply.text)
                row.pop("error", None)
                break
            except (ValueError, json.JSONDecodeError) as exc:
                row["error"] = f"{exc}"[:300]
        with out.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        written.append(row)
        print(item["rid"], judge, row.get("label"), str(row.get("error", ""))[:60], flush=True)
    return written


def split_rids(
    labels_1: Mapping[str, Mapping[str, object]], labels_2: Mapping[str, Mapping[str, object]]
) -> list[str]:
    """Return the replies that one judge labelled PASS and the other FAIL."""
    return sorted(
        rid
        for rid in set(labels_1) & set(labels_2)
        if {labels_1[rid].get("label"), labels_2[rid].get("label")} == {"PASS", "FAIL"}
    )


def score(
    key: Mapping[str, Mapping[str, str]],
    labels: Mapping[str, Mapping[str, Mapping[str, object]]],
) -> dict[str, object]:
    """Score both arms of each scenario and decide AC-1 and AC-2.

    Args:
        key: ``rid`` → ``id``, ``arm``, ``trace_id``.
        labels: Judge number (``"1"``, ``"2"``, ``"3"``) → ``rid`` → label row.

    Returns:
        ``replies`` (each judge's label, the third label, the final pair and the points),
        ``scenarios`` (the points of each arm), the AC-2 gaps, ``ac2`` and ``ac1``. ``ac1`` is
        ``incomplete`` unless AC-2 passes.
    """

    def valid(judge: str, rid: str) -> str | None:
        label = labels.get(judge, {}).get(rid, {}).get("label")
        return str(label) if label in LABELS else None

    splits = set(split_rids(labels.get("1", {}), labels.get("2", {})))
    replies: list[dict[str, object]] = []
    missing: list[str] = []
    missing_third: list[str] = []
    unexpected_third = sorted(rid for rid in labels.get("3", {}) if rid not in splits)
    points: dict[str, dict[str, int]] = {}
    for rid in sorted(key):
        j1, j2, j3 = valid("1", rid), valid("2", rid), valid("3", rid)
        sid, arm = key[rid]["id"], key[rid]["arm"]
        reply: dict[str, object] = {"rid": rid, "id": sid, "arm": arm, "judge_1": j1, "judge_2": j2}
        if j1 is None or j2 is None:
            missing.append(rid)
        final = [j1, j2]
        if rid in splits:
            reply["judge_3"] = j3
            if j3 is None:
                missing_third.append(rid)
            else:
                final = ["PASS", j3]
        reply["final"] = final
        if all(f is not None for f in final):
            reply["points"] = sum(POINTS[str(f)] for f in final)
            points.setdefault(sid, {})[arm] = cast(int, reply["points"])
        replies.append(reply)
    arms_by_id: dict[str, set[str]] = {}
    for entry in key.values():
        arms_by_id.setdefault(entry["id"], set()).add(entry["arm"])
    one_arm = sorted(sid for sid, arms in arms_by_id.items() if arms != set(part2.ARMS))
    ac2 = not (missing or missing_third or unexpected_third or one_arm)
    scenarios = [
        {
            "id": sid,
            "control": points.get(sid, {}).get("control"),
            "treatment": points.get(sid, {}).get("treatment"),
        }
        for sid in sorted(arms_by_id)
    ]
    lower = [
        s["id"] for s in scenarios if ac2 and cast(int, s["treatment"]) < cast(int, s["control"])
    ]
    return {
        "replies": replies,
        "scenarios": scenarios,
        "splits": sorted(splits),
        "missing_labels": missing,
        "missing_third": missing_third,
        "unexpected_third": unexpected_third,
        "one_arm": one_arm,
        "ac2": "PASS" if ac2 else "FAIL",
        "ac1": ("FAIL" if lower else "PASS") if ac2 else "incomplete",
        "lower_with_skills": lower,
        "totals": {
            arm: sum(cast(int, s[arm]) for s in scenarios if s[arm] is not None)
            for arm in part2.ARMS
        },
    }


def render(report: Mapping[str, object], cost: float) -> str:
    """Render the score as text."""
    lines = ["FRE-1598 benefit run — blind judging", "scenario | control | treatment"]
    for s in cast(list[dict[str, object]], report["scenarios"]):
        lines.append(f"{s['id']} | {s['control']} | {s['treatment']}")
    lines.append("reply | id | arm | judge 1 | judge 2 | judge 3 | points")
    for r in cast(list[dict[str, object]], report["replies"]):
        lines.append(
            f"{r['rid']} | {r['id']} | {r['arm']} | {r['judge_1']} | {r['judge_2']} | "
            f"{r.get('judge_3', '-')} | {r.get('points')}"
        )
    for field in (
        "splits",
        "missing_labels",
        "missing_third",
        "unexpected_third",
        "one_arm",
        "lower_with_skills",
    ):
        lines.append(f"{field}: {report[field] or 'none'}")
    lines += [
        f"totals: {report['totals']}",
        f"judge cost: ${cost:.4f}",
        f"AC-2: {report['ac2']}",
        f"AC-1: {report['ac1']}",
    ]
    return "\n".join(lines) + "\n"


def _pin_judging(jdir: Path) -> None:
    path = jdir / "judges.json"
    pins = judge_pins()
    if path.exists() and json.loads(path.read_text()) != pins:
        raise SystemExit(f"{path} holds other judge pins: use a new run directory")
    jdir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pins, indent=2) + "\n")


def _items(jdir: Path) -> list[dict[str, str]]:
    return [cast(dict[str, str], r) for r in part2.read_rows(jdir / "items.jsonl")]


async def _label(run_dir: Path, judge: str) -> int:
    jdir = judging_dir(run_dir)
    _pin_judging(jdir)
    items = _items(jdir)
    if judge == "3":
        splits = set(
            split_rids(read_labels(jdir / "labels-1.jsonl"), read_labels(jdir / "labels-2.jsonl"))
        )
        items = [i for i in items if i["rid"] in splits]
    if not items:
        print(f"judge {judge}: nothing to label")
        return 0
    # A standalone script has no application startup, so it registers the cost gate itself;
    # without one every paid call refuses. The gate is what makes the `study` cap bind.
    from personal_agent.config.settings import get_settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )

    # The gate's reservations and the client's api_costs row both go to settings.database_url.
    # FRE-375: an eval script writes no production Postgres row, so the judge process must run
    # with AGENT_DATABASE_URL on postgres-eval. The `study` cap then binds on the eval ledger.
    db_url = get_settings().database_url
    if f":{EVAL_DB_PORT}/" not in db_url:
        raise SystemExit(
            f"AGENT_DATABASE_URL must point at postgres-eval (port {EVAL_DB_PORT}) for the judges"
        )
    gate = CostGate(config=load_budget_config(), db_url=db_url)
    await gate.connect()
    set_default_gate(gate)
    try:
        rows = await label_items(
            items, judge, client_judge(JUDGES[judge]), jdir / f"labels-{judge}.jsonl"
        )
    finally:
        set_default_gate(None)
        # One-shot script, no reaper task: reap once so a crash leaves no reservation behind.
        await gate.reap_stale()
        await gate.disconnect()
    return 1 if any("error" in r for r in rows) else 0


def _blind(run_dir: Path) -> int:
    jdir = judging_dir(run_dir)
    if (jdir / "items.jsonl").exists():
        raise SystemExit(f"{jdir}/items.jsonl exists: blinding runs once per run")
    items, key = blind(part2.read_rows(part2.rows_path(run_dir, MODEL_KEY)))
    write_jsonl(jdir / "items.jsonl", items)
    (jdir / "key.json").write_text(json.dumps(key, indent=2) + "\n")
    print(f"{len(items)} replies blinded -> {jdir}")
    return 0


def _score(run_dir: Path) -> int:
    jdir = judging_dir(run_dir)
    key = json.loads((jdir / "key.json").read_text())
    labels = {j: read_labels(jdir / f"labels-{j}.jsonl") for j in JUDGES}
    report = score(key, labels)
    cost = sum(
        float(cast(float, r.get("cost_usd") or 0.0))
        for j in JUDGES
        for r in part2.read_rows(jdir / f"labels-{j}.jsonl")
    )
    report["judge_cost_usd"] = round(cost, 6)
    (jdir / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(render(report, cost))
    return 0 if report["ac2"] == "PASS" and report["ac1"] == "PASS" else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then blind, label, run the third judge, or score.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success; ``score`` returns 0 only when AC-1 and AC-2 pass.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("blind", "label", "third", "score"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--run-dir", required=True)
        if name == "label":
            cmd.add_argument("--judge", required=True, choices=("1", "2"))
    args = p.parse_args(argv)
    run_dir = Path(args.run_dir)
    if args.command == "blind":
        return _blind(run_dir)
    if args.command == "label":
        return asyncio.run(_label(run_dir, args.judge))
    if args.command == "third":
        return asyncio.run(_label(run_dir, "3"))
    return _score(run_dir)


if __name__ == "__main__":
    sys.exit(main())
