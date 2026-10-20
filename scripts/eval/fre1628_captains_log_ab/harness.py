"""FRE-1628, FRE-1630: Captain's Log reflection, claude_sonnet against one candidate, blind.

Master approved this side-by-side on the FRE-1628 ticket (2026-10-20): stored real
reflection inputs, both models through the production ``generate_reflection_entry``,
blind pairs labelled by the FRE-1598 judge panel with the run date in the prompt.
FRE-1630 added the ``luna`` arm and made ``judge`` and ``score`` compare ``sonnet``
against one named candidate arm (``--candidate``, default ``haiku``).

Four steps, each a subcommand:

    # 1. Copy N recent real turns from production ES. Read only; no personal_agent import.
    uv run python -m scripts.eval.fre1628_captains_log_ab.harness pull --count 10
    # 2. Generate one reflection per turn and arm. Run steps 2 and 3 under the FRE-375
    #    test wrapper: both write cost-gate rows to settings.database_url.
    uv run python -m scripts.eval.fre1628_captains_log_ab.harness generate --arm sonnet
    uv run python -m scripts.eval.fre1628_captains_log_ab.harness generate --arm luna
    # 3. Label each blind pair with every judge, 4. score against the preregistered bar.
    uv run python -m scripts.eval.fre1628_captains_log_ab.harness judge --candidate luna
    uv run python -m scripts.eval.fre1628_captains_log_ab.harness score --candidate luna

``generate`` changes no configuration: it patches ``resolve_role_model_key`` for the
``captains_log`` role only, and it feeds the stored events through
``_fetch_trace_events`` in place of the ES read (which also skips its 5.5 s wait).

Bar (preregistered on the ticket): zero candidate reflections off the DSPy path, and the
candidate not worse (a judge majority for it, or no majority for Sonnet) on at least 8 of
10 pairs. All output lands in ``telemetry/evaluation/fre1630-captains-log-ab/``, which is
gitignored: the inputs hold real conversation text. Labels and report are per candidate:
``labels-<candidate>.jsonl`` and ``report-<candidate>.json``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

import httpx

OUT_DIR = Path("telemetry/evaluation/fre1630-captains-log-ab")
INPUTS = OUT_DIR / "inputs.jsonl"
ARMS: dict[str, str] = {
    "sonnet": "claude_sonnet",
    "haiku": "claude_haiku_5_5",
    "luna": "gpt-6-luna",
}
CANDIDATES = sorted(a for a in ARMS if a != "sonnet")
BLIND_SEED = 20261020
MAX_TOKENS = 4000
BAR_NOT_WORSE = 8
ES_URL = "http://localhost:9200"  # fre-375-allow: pull reads production captures, read only

JSONDict = dict[str, object]

JUDGE_SYSTEM = """\
You compare two reflections written about one turn of an AI assistant. An engineer reads \
these reflections to find problems and improvements in the assistant.

Today's date is {run_date}. Facts dated after your training data are not evidence of invention.

Judge which reflection serves that engineer better:
- Accurate to the TELEMETRY: it states no metric, event or failure that the telemetry does not show.
- It proposes a change only when the telemetry supports one, and the change is specific and \
actionable. A reflection that correctly proposes nothing for a clean turn is good.
- It notices the problems that the telemetry does show.
Do not judge length or style.

Answer with JSON only, no other text: {{"verdict": "A" | "B" | "tie", "reason": "<one sentence>"}}
"""

JUDGE_USER = (
    "USER MESSAGE:\n{user_message}\n\nFINAL STATE: {final_state} · STEPS: {steps} · "
    "REPLY LENGTH: {reply_length}\n\nTELEMETRY:\n{telemetry}\n\n"
    "REFLECTION A:\n{a}\n\nREFLECTION B:\n{b}\n"
)


def _read_jsonl(path: Path) -> list[JSONDict]:
    """Read one JSON object per line."""
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, object]]) -> None:
    """Write one JSON object per line, replacing the file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, default=str) + "\n" for r in rows), encoding="utf-8")


def _outputs_path(arm: str) -> Path:
    return OUT_DIR / f"outputs-{arm}.jsonl"


def _labels_path(candidate: str) -> Path:
    return OUT_DIR / f"labels-{candidate}.jsonl"


# ── 1. pull ───────────────────────────────────────────────────────────────────


def pull(count: int, days: int) -> int:
    """Copy the most recent real turns, one per session, with their trace events.

    Args:
        count: Turns to keep.
        days: How far back to look.

    Returns:
        Process exit code.
    """
    with httpx.Client(base_url=ES_URL, timeout=30.0) as es:
        captures = es.post(
            "/agent-captains-captures-*/_search",
            json={
                "size": 300,
                "sort": [{"timestamp": "desc"}],
                "query": {
                    "bool": {
                        "filter": [{"range": {"timestamp": {"gte": f"now-{days}d"}}}],
                        "must_not": [{"term": {"eval_mode": True}}],
                    }
                },
            },
        ).json()["hits"]["hits"]
        rows: list[JSONDict] = []
        sessions: set[str] = set()
        for hit in captures:
            src = hit["_source"]
            session_id, user_message = src.get("session_id"), src.get("user_message")
            if not session_id or not user_message or session_id in sessions:
                continue
            trace_id = str(src["trace_id"])
            events: list[JSONDict] = []
            for form in (trace_id, trace_id.replace("-", "")):
                events = [
                    h["_source"]
                    for h in es.post(
                        "/agent-logs-*/_search",
                        json={
                            "size": 2000,
                            "sort": [{"@timestamp": "asc"}],
                            "query": {"term": {"trace_id": form}},
                        },
                    ).json()["hits"]["hits"]
                ]
                if events:
                    break
            if not events:
                continue
            sessions.add(session_id)
            rows.append(
                {
                    "rid": f"c{len(rows) + 1:02d}",
                    "trace_id": trace_id,
                    "session_id": session_id,
                    "timestamp": src.get("timestamp"),
                    "user_message": user_message,
                    "steps_count": len(src.get("steps") or []),
                    "reply_length": len(src.get("assistant_response") or ""),
                    "metrics_summary": src.get("metrics_summary"),
                    "events": events,
                }
            )
            if len(rows) == count:
                break
    _write_jsonl(INPUTS, rows)
    print(
        f"pulled {len(rows)} turns, {sum(len(cast(list[JSONDict], r['events'])) for r in rows)} events"
    )  # noqa: T201
    return 0 if len(rows) == count else 1


# ── 2. generate ───────────────────────────────────────────────────────────────


def _translated_events(row: Mapping[str, object]) -> list[JSONDict]:
    from personal_agent.telemetry.queries import (  # noqa: PLC0415
        _translate_es_source_to_log_entry,
    )

    return [_translate_es_source_to_log_entry(e) for e in cast(list[JSONDict], row["events"])]


def _path_of(events: Sequence[Mapping[str, object]]) -> str:
    """Which branch of ``generate_reflection_entry`` produced the entry."""
    names = {e.get("event") for e in events}
    if "dspy_reflection_succeeded" in names:
        return "dspy"
    if "manual_reflection_generated" in names:
        return "manual"
    return "basic"


async def generate(arm: str, limit: int | None = None) -> int:
    """Run every stored turn through the production reflection producer on one arm.

    Args:
        arm: ``sonnet`` or a candidate (``haiku``, ``luna``).
        limit: Run only the first N turns (the preflight trace).

    Returns:
        Process exit code.
    """
    from structlog.testing import capture_logs  # noqa: PLC0415

    import personal_agent.config as config_pkg  # noqa: PLC0415
    from personal_agent.captains_log import reflection  # noqa: PLC0415
    from personal_agent.config import settings  # noqa: PLC0415
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )

    key = ARMS[arm]
    real_resolve = config_pkg.resolve_role_model_key

    def _resolve(role: str) -> str:
        return key if role == "captains_log" else real_resolve(role)

    config_pkg.resolve_role_model_key = _resolve  # type: ignore[assignment]

    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    out: list[JSONDict] = []
    try:
        for row in _read_jsonl(INPUTS)[:limit]:
            events = _translated_events(row)

            async def _stored(_trace_id: str, _events: list[JSONDict] = events) -> list[JSONDict]:
                return _events

            reflection._fetch_trace_events = _stored  # type: ignore[assignment]
            with capture_logs() as logs:
                entry = await reflection.generate_reflection_entry(
                    user_message=str(row["user_message"]),
                    trace_id=str(row["trace_id"]),
                    steps_count=cast(int, row["steps_count"]),
                    final_state="COMPLETED",
                    reply_length=cast(int, row["reply_length"]),
                    metrics_summary=cast("dict[str, object] | None", row["metrics_summary"]),
                    session_id=str(row["session_id"]),
                    eval_mode=True,
                )
            settled = [e for e in logs if e.get("event") == "dspy_job_settled"]
            calls = [e for e in logs if e.get("event") == "model_call_completed"]
            dumped = entry.model_dump(mode="json")
            out.append(
                {
                    "rid": row["rid"],
                    "arm": arm,
                    "path": _path_of(logs),
                    "models": sorted({str(e.get("model")) for e in settled + calls}),
                    "cost_usd": sum(float(cast(float, e.get("settled_usd") or 0)) for e in settled)
                    + sum(float(cast(float, e.get("cost_usd") or 0)) for e in calls),
                    "warnings": sorted(
                        {str(e.get("event")) for e in logs if e.get("log_level") == "warning"}
                    ),
                    "reflection": {
                        k: dumped.get(k)
                        for k in (
                            "rationale",
                            "proposed_change",
                            "supporting_metrics",
                            "impact_assessment",
                        )
                    },
                }
            )
            print(f"{row['rid']} {arm} path={out[-1]['path']} models={out[-1]['models']}")  # noqa: T201
    finally:
        await gate.disconnect()
        set_default_gate(None)
    _write_jsonl(_outputs_path(arm), out)
    return 0 if all(o["path"] == "dspy" for o in out) else 1


# ── 3. judge ──────────────────────────────────────────────────────────────────


def _judge_call(key: str) -> Callable[[str, str], Awaitable[tuple[str, float]]]:
    from personal_agent.llm_client import ModelRole  # noqa: PLC0415
    from personal_agent.llm_client.factory import get_llm_client_for_key  # noqa: PLC0415
    from personal_agent.telemetry.trace import SystemTraceContext  # noqa: PLC0415

    client = get_llm_client_for_key(key, budget_role="study")
    session_id = str(uuid.uuid4())

    async def call(system: str, user: str) -> tuple[str, float]:
        resp = await client.respond(
            role=ModelRole.PRIMARY,
            messages=[{"role": "user", "content": user}],
            system_prompt=system,
            max_tokens=MAX_TOKENS,
            trace_ctx=SystemTraceContext.new("fre1628_cl_judge", session_id=session_id),
        )
        return str(resp.get("content") or ""), float(resp.get("cost_usd") or 0.0)

    return call


def _parse_verdict(text: str) -> tuple[str, str] | None:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    verdict = data.get("verdict")
    return (verdict, str(data.get("reason", ""))) if verdict in ("A", "B", "tie") else None


async def judge(candidate: str) -> int:
    """Label every blind Sonnet/candidate pair with every judge of the FRE-1598 panel.

    Args:
        candidate: The arm that Sonnet is compared with (``haiku`` or ``luna``).

    Returns:
        Process exit code.
    """
    from scripts.eval.fre1598.judge import JUDGES  # noqa: PLC0415

    from personal_agent.captains_log.reflection import _summarize_telemetry  # noqa: PLC0415
    from personal_agent.config import (
        load_model_config,  # noqa: PLC0415
        settings,  # noqa: PLC0415
    )
    from personal_agent.cost_gate import (  # noqa: PLC0415
        CostGate,
        load_budget_config,
        set_default_gate,
    )

    inputs = {str(r["rid"]): r for r in _read_jsonl(INPUTS)}
    outputs = {
        arm: {str(o["rid"]): o for o in _read_jsonl(_outputs_path(arm))}
        for arm in ("sonnet", candidate)
    }
    run_date = datetime.now(timezone.utc).date().isoformat()
    system = JUDGE_SYSTEM.format(run_date=run_date)
    rng = random.Random(BLIND_SEED)
    catalog = load_model_config().models

    gate = CostGate(config=load_budget_config(), db_url=settings.database_url)
    await gate.connect()
    set_default_gate(gate)
    rows: list[JSONDict] = []
    try:
        calls = {key: _judge_call(key) for key in JUDGES.values()}
        for rid, row in inputs.items():
            if rid not in outputs["sonnet"] or rid not in outputs[candidate]:
                continue
            candidate_is_a = rng.random() < 0.5
            first, second = (candidate, "sonnet") if candidate_is_a else ("sonnet", candidate)
            user = JUDGE_USER.format(
                user_message=str(row["user_message"])[:400],
                final_state="COMPLETED",
                steps=row["steps_count"],
                reply_length=row["reply_length"],
                telemetry=_summarize_telemetry(
                    _translated_events(row),
                    cast("dict[str, object] | None", row["metrics_summary"]),
                ),
                a=json.dumps(outputs[first][rid]["reflection"], indent=1),
                b=json.dumps(outputs[second][rid]["reflection"], indent=1),
            )
            for key, call in calls.items():
                parsed, cost = None, 0.0
                for _attempt in range(2):
                    text, spent = await call(system, user)
                    cost += spent
                    parsed = _parse_verdict(text)
                    if parsed:
                        break
                verdict, reason = parsed or ("error", "unparsed twice")
                winner = {"A": first, "B": second}.get(verdict, verdict)
                rows.append(
                    {
                        "rid": rid,
                        "judge": key,
                        "model_id": catalog[key].id,
                        "candidate_is_a": candidate_is_a,
                        "verdict": verdict,
                        "winner": winner,
                        "reason": reason,
                        "cost_usd": cost,
                        "run_date": run_date,
                    }
                )
                print(f"{rid} {key}: {winner}")  # noqa: T201
    finally:
        await gate.disconnect()
        set_default_gate(None)
    _write_jsonl(_labels_path(candidate), rows)
    return 0


# ── 4. score ──────────────────────────────────────────────────────────────────


def score(candidate: str) -> int:
    """Apply the preregistered bar and write ``report-<candidate>.json``.

    Args:
        candidate: The arm that Sonnet is compared with (``haiku`` or ``luna``).

    Returns:
        0 when the candidate passes the bar, else 1.
    """
    labels = _read_jsonl(_labels_path(candidate))
    cand_out = {str(o["rid"]): o for o in _read_jsonl(_outputs_path(candidate))}
    sonnet_out = {str(o["rid"]): o for o in _read_jsonl(_outputs_path("sonnet"))}
    pairs: dict[str, list[str]] = {}
    for label in labels:
        pairs.setdefault(str(label["rid"]), []).append(str(label["winner"]))
    per_pair: dict[str, str] = {}
    for rid, winners in sorted(pairs.items()):
        majority = [w for w in (candidate, "sonnet") if winners.count(w) >= 2]
        # A pair with an unparsed label is not evidence that the candidate is not worse.
        per_pair[rid] = "error" if "error" in winners else (majority[0] if majority else "tie")
    not_worse = sum(1 for w in per_pair.values() if w in (candidate, "tie"))
    off_path = sorted(rid for rid, o in cand_out.items() if o["path"] != "dspy")
    complete = len(per_pair) == len(_read_jsonl(INPUTS))
    passed = complete and not off_path and not_worse >= BAR_NOT_WORSE
    report: JSONDict = {
        "pairs": len(per_pair),
        "complete": complete,
        "per_pair": per_pair,
        "per_judge": {
            key: {
                w: sum(1 for x in labels if x["judge"] == key and x["winner"] == w)
                for w in (candidate, "sonnet", "tie", "error")
            }
            for key in sorted({str(x["judge"]) for x in labels})
        },
        f"{candidate}_not_worse": not_worse,
        "bar_not_worse": BAR_NOT_WORSE,
        f"{candidate}_off_dspy_path": off_path,
        "sonnet_off_dspy_path": sorted(rid for rid, o in sonnet_out.items() if o["path"] != "dspy"),
        "cost_usd": {
            "sonnet_generate": sum(float(cast(float, o["cost_usd"])) for o in sonnet_out.values()),
            f"{candidate}_generate": sum(
                float(cast(float, o["cost_usd"])) for o in cand_out.values()
            ),
            "judges": sum(float(cast(float, x["cost_usd"])) for x in labels),
        },
        "passed": passed,
    }
    (OUT_DIR / f"report-{candidate}.json").write_text(
        json.dumps(report, indent=2), encoding="utf-8"
    )
    print(json.dumps(report, indent=2))  # noqa: T201
    return 0 if passed else 1


def main() -> int:
    """Parse arguments and run one step.

    Returns:
        Process exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="step", required=True)
    p_pull = sub.add_parser("pull")
    p_pull.add_argument("--count", type=int, default=10)
    p_pull.add_argument("--days", type=int, default=30)
    p_gen = sub.add_parser("generate")
    p_gen.add_argument("--arm", choices=sorted(ARMS), required=True)
    p_gen.add_argument("--limit", type=int, default=None)
    for step in ("judge", "score"):
        sub.add_parser(step).add_argument("--candidate", choices=CANDIDATES, default="haiku")
    args = parser.parse_args()
    if args.step == "pull":
        return pull(args.count, args.days)
    if args.step == "generate":
        return asyncio.run(generate(args.arm, args.limit))
    if args.step == "judge":
        return asyncio.run(judge(args.candidate))
    return score(args.candidate)


if __name__ == "__main__":
    raise SystemExit(main())
