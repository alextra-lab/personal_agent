"""FRE-1728 AC-2: memory crosses sessions, the wipe removes it, and the check can fail.

    uv run python -m scripts.eval.fre1728.stub_stack up --run-dir <run>
    uv run python -m scripts.eval.fre1728.isolation_probe --run-dir <run>

The probe runs on the stub gateway: the real gateway code and the real consolidation path, with a stub model. It
drives the real bridge. Each step waits for full gateway quiet, so the probe takes about 20 minutes.

The two seeded negatives use the same checks:

- A wipe per session, the old default: the fact is lost, so the check can detect a loss.
- A graph wipe that keeps the captures: the fact returns through consolidation, so the check can detect a leak.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import httpx
from scripts.eval.eval_isolation import IsolatedArmRunner
from scripts.eval.fre1337_intent_probe.behavioral import EVAL_ES_URL
from scripts.eval.fre1728.bridge import create_app
from scripts.eval.fre1728.spend import PILOT_CAP_USD
from scripts.eval.fre1728.stack import eval_postgres_dsn, open_stack
from scripts.eval.fre1728.stub_stack import (
    STUB_DATABASE,
    STUB_EMBEDDING_MODEL,
    STUB_REDIS_INDEX,
)


def _token() -> str:
    return f"FACT-{uuid.uuid4().hex[:10].upper()}"


def _holds(memories: list[dict[str, str]], token: str) -> bool:
    return token in json.dumps(memories)


async def _turn(
    client: httpx.AsyncClient, message: str, session_id: str | None = None
) -> dict[str, str]:
    response = await client.post("/turn", json={"message": message, "session_id": session_id})
    response.raise_for_status()
    return response.json()


async def _settle(client: httpx.AsyncClient, trace_id: str) -> dict[str, object]:
    response = await client.post("/settle", json={"trace_ids": [trace_id]}, timeout=3800.0)
    response.raise_for_status()
    return response.json()


async def _memories(client: httpx.AsyncClient) -> list[dict[str, str]]:
    response = await client.get("/memories")
    response.raise_for_status()
    return response.json()


async def _postgres_carry(token: str) -> dict[str, int]:
    connection = await asyncpg.connect(eval_postgres_dsn(STUB_DATABASE))
    try:
        sessions = await connection.fetchval("SELECT count(*) FROM sessions")
        holding = await connection.fetchval(
            "SELECT count(*) FROM sessions WHERE messages::text LIKE $1", f"%{token}%"
        )
        # The stub embedder books a price-list cost under its own model name, and it reaches no vendor.
        costs = await connection.fetchval(
            "SELECT count(*) FROM api_costs WHERE model <> $1", STUB_EMBEDDING_MODEL
        )
    finally:
        await connection.close()
    return {"sessions": sessions, "sessions_holding_fact": holding, "api_costs_rows": costs}


async def _elasticsearch_carry(es: httpx.AsyncClient, token: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for pattern in ("agent-captains-captures-*", "agent-logs-*"):
        response = await es.post(
            f"{EVAL_ES_URL}/{pattern}/_count",
            json={"query": {"query_string": {"query": f'"{token}"'}}},
            timeout=30.0,
        )
        response.raise_for_status()
        counts[pattern] = response.json()["count"]
    return counts


def _redis_streams() -> list[str]:
    keys = subprocess.run(
        ["docker", "exec", "cloud-sim-redis-eval", "redis-cli", "-n", str(STUB_REDIS_INDEX),
         "KEYS", "stream:*"],
        capture_output=True,
        text=True,
        check=False,
    )  # fmt: skip
    return keys.stdout.split()


def _primary_requests_holding(calls_dir: Path, token: str, since_seq: int) -> int:
    count = 0
    for path in sorted(calls_dir.glob("*.json")):
        call = json.loads(path.read_text())
        if call["seq"] > since_seq and call["body"].get("tools") and token in path.read_text():
            count += 1
    return count


def _last_seq(calls_dir: Path) -> int:
    paths = sorted(calls_dir.glob("*.json"))
    return json.loads(paths[-1].read_text())["seq"] if paths else 0


async def probe(run_dir: Path) -> dict[str, object]:
    """Run every step.

    Args:
        run_dir: The run directory that ``stub_stack up`` used.

    Returns:
        The measured result of each step, and the verdict.
    """
    stack = open_stack("stub", STUB_DATABASE, PILOT_CAP_USD, datetime.now(UTC))
    deps = stack.deps
    calls_dir = run_dir / "stub-out" / "calls"
    transport = httpx.ASGITransport(app=create_app(deps))
    fact_one, fact_two = _token(), _token()
    out: dict[str, object] = {"fact_one": fact_one, "fact_two": fact_two}
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://bridge", timeout=3800.0
        ) as client:
            (await client.post("/wipe")).raise_for_status()

            # A fact written in one session is readable in the same user's next session.
            first = await _turn(client, f"My badge number is {fact_one}.")
            settled = await _settle(client, first["trace_id"])
            written = _holds(await _memories(client), fact_one)
            next_session = await _turn(
                client, "ECHO-MEMORY: what do you remember about my badge number?"
            )
            await _settle(client, next_session["trace_id"])
            out["same_user_next_session"] = {
                "missing_turns": settled["missing_turns"],
                "fact_in_graph_after_session_1": written,
                "new_session_id_differs": next_session["session_id"] != first["session_id"],
                "fact_in_graph_after_session_2": _holds(await _memories(client), fact_one),
                "agent_reply_in_session_2": next_session["response"],
                "fact_in_reply": fact_one in next_session["response"],
            }

            # Seeded negative 1: a wipe per session, the old default, loses the fact.
            old_default = IsolatedArmRunner(driver=stack.driver)
            lost = await old_default.run_turn(
                deps.http,
                deps.es,
                "ECHO-MEMORY: what do you remember about my badge number now?",
                arm="stub",
                wait_settle=False,
            )
            out["seeded_negative_wipe_per_session"] = {
                "agent_reply": lost.response,
                "fact_in_reply": fact_one in lost.response,
            }

            # Seeded negative 2: that wipe kept the captures, so consolidation brings the fact back.
            await _settle(client, lost.trace_id)
            out["seeded_negative_graph_wipe_keeps_captures"] = {
                "fact_returned_to_graph": _holds(await _memories(client), fact_one),
            }

            # The real wipe removes the fact, and a later capture event does not bring it back.
            second = await _turn(client, f"My locker code is {fact_two}.")
            await _settle(client, second["trace_id"])
            fact_two_written = _holds(await _memories(client), fact_two)
            wipe = await client.post("/wipe")
            wipe.raise_for_status()
            seq_after_wipe = _last_seq(calls_dir)
            third = await _turn(client, "Hello. I am a new user.")
            await _settle(client, third["trace_id"])
            after = await _memories(client)
            new_user = await _turn(
                client, "ECHO-MEMORY: what do you remember about my locker code?"
            )
            await _settle(client, new_user["trace_id"])
            out["after_full_wipe"] = {
                "fact_two_was_in_graph_before": fact_two_written,
                "residual_memories_at_wipe": wipe.json()["residual_memories"],
                "fact_one_in_graph_after_a_new_capture": _holds(after, fact_one),
                "fact_two_in_graph_after_a_new_capture": _holds(after, fact_two),
                "agent_reply_for_next_user": new_user["response"],
                "fact_two_in_reply": fact_two in new_user["response"],
                "primary_requests_holding_fact_two": _primary_requests_holding(
                    calls_dir, fact_two, seq_after_wipe
                ),
            }

            # What the stores still carry after the wipe, and whether a turn reads it.
            out["carried_across_the_wipe"] = {
                "postgres": await _postgres_carry(fact_two),
                "elasticsearch": await _elasticsearch_carry(deps.es, fact_two),
                "redis_streams": _redis_streams(),
            }
    finally:
        await stack.close()

    same = out["same_user_next_session"]
    negative_one = out["seeded_negative_wipe_per_session"]
    negative_two = out["seeded_negative_graph_wipe_keeps_captures"]
    after_wipe = out["after_full_wipe"]
    carried = out["carried_across_the_wipe"]
    out["verdict"] = {
        "fact_readable_in_next_session": bool(same["fact_in_reply"]),  # type: ignore[index]
        "negative_1_detects_a_loss": not negative_one["fact_in_reply"],  # type: ignore[index]
        "negative_2_detects_a_leak": bool(negative_two["fact_returned_to_graph"]),  # type: ignore[index]
        "wipe_removes_the_fact": not (
            after_wipe["fact_two_in_graph_after_a_new_capture"]  # type: ignore[index]
            or after_wipe["fact_two_in_reply"]  # type: ignore[index]
            or after_wipe["primary_requests_holding_fact_two"]  # type: ignore[index]
        ),
        "no_paid_call": carried["postgres"]["api_costs_rows"] == 0,  # type: ignore[index]
    }
    return out


def main() -> int:
    """Run the probe and print its result.

    Returns:
        0 if every verdict holds, else 1.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = asyncio.run(probe(args.run_dir.resolve()))
    print(json.dumps(result, indent=2))
    return 0 if all(result["verdict"].values()) else 1  # type: ignore[attr-defined]


if __name__ == "__main__":
    sys.exit(main())
