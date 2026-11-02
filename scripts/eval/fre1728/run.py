"""FRE-1728: run the pilot: the loopback bridge, the driver in MemAudit's environment, then the Scout scan.

    uv run python -m scripts.eval.fre1728.run --arm stub --users user_019 --tasks-per-user 1 --runs A B

A paid arm needs ``--owner-go``: the owner's go, quoted as recorded on the ticket. See ``README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from pathlib import Path

import asyncpg  # type: ignore[import-untyped]
import uvicorn
from scripts.eval.eval_isolation import wait_for_gateway_idle
from scripts.eval.fre1728.bridge import GATEWAY_CONTAINERS, create_app
from scripts.eval.fre1728.spend import PILOT_CAP_USD
from scripts.eval.fre1728.stack import eval_postgres_dsn, open_stack
from scripts.eval.fre1728.stub_stack import (
    STUB_DATABASE,
    STUB_EMBEDDING_MODEL,
    STUB_OPENAI_BASE_URL,
)

HERE = Path(__file__).resolve().parent
HOME = Path(os.environ.get("FRE1728_HOME", Path.home() / ".cache" / "seshat-fre1728"))
BRIDGE_PORT = 9016
PAID_DATABASE = "personal_agent"
#: Adults in work, so that the tasks resemble the owner's use. A child persona has school tasks.
DEFAULT_USERS = ("user_019", "user_015")
MAX_TURNS = 6
EXIT_SCOUT_FAILED = 6


def driver_env(arm: str, host_env: Mapping[str, str]) -> dict[str, str]:
    """Build a child's environment. It holds no project secret but the OpenAI key on a paid arm.

    A child starts in the run directory, because Scout searches upward from its working directory for a ``.env``
    file, and from the repo it finds production's.

    Args:
        arm: The arm.
        host_env: The caller's environment.

    Returns:
        The environment.

    Raises:
        SystemExit: If a paid arm has no OpenAI key.
    """
    env = {"PATH": host_env.get("PATH", ""), "HOME": host_env.get("HOME", "")}
    if arm == "stub":
        return {
            **env,
            "OPENAI_API_KEY": "fre1728-stub-unused",
            "OPENAI_BASE_URL": STUB_OPENAI_BASE_URL,
        }
    key = host_env.get("AGENT_OPENAI_API_KEY")
    if not key:
        raise SystemExit("set AGENT_OPENAI_API_KEY for MemAudit's simulator and judges")
    return {**env, "OPENAI_API_KEY": key}


def refuse_paid_run_without_go(arm: str, owner_go: str | None) -> None:
    """Refuse a paid arm without the owner's recorded go.

    Args:
        arm: The arm.
        owner_go: The quoted go.

    Raises:
        SystemExit: If the arm is paid and the go is empty.
    """
    if arm != "stub" and not (owner_go and owner_go.strip()):
        raise SystemExit(
            f"arm {arm!r} makes paid calls: pass --owner-go with the go recorded on FRE-1728"
        )


def _is_running(container: str) -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", container],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.stdout.strip() == "true"


def refuse_a_second_real_gateway(arm: str) -> None:
    """Refuse a paid arm while the other real gateway runs.

    Control and treatment share captures, Postgres and Neo4j, and each holds real keys. Both consolidate and
    summarize the same work, which pays for it twice.

    Args:
        arm: The arm.

    Raises:
        SystemExit: If the other real gateway is running.
    """
    if arm == "stub":
        return
    other = "treatment" if arm == "control" else "control"
    if _is_running(GATEWAY_CONTAINERS[other]):
        raise SystemExit(f"stop {GATEWAY_CONTAINERS[other]} first: it shares this arm's stores")


async def _assert_no_cost_in_stub_database() -> int:
    connection = await asyncpg.connect(eval_postgres_dsn(STUB_DATABASE))
    try:
        # The stub embedder books a price-list cost under its own model name, and it reaches no vendor.
        return int(
            await connection.fetchval(
                "SELECT count(*) FROM api_costs WHERE model <> $1", STUB_EMBEDDING_MODEL
            )
        )
    finally:
        await connection.close()


async def _run_scout(args: argparse.Namespace, run_dir: Path, remaining_usd: float) -> int:
    """Scan the saved transcripts in Scout's own environment, inside the remaining budget.

    Args:
        args: The parsed arguments.
        run_dir: The run directory.
        remaining_usd: The cap less the spend so far.

    Returns:
        0 on success, else ``EXIT_SCOUT_FAILED``.
    """
    base_url = ["--model-base-url", STUB_OPENAI_BASE_URL] if args.arm == "stub" else []
    process = await asyncio.create_subprocess_exec(
        str(args.scout_python),
        "-I",
        str(HERE / "scout_scan.py"),
        "--run-dir", str(run_dir),
        "--runs", *args.runs,
        "--remaining-usd", str(remaining_usd),
        "--model", args.scout_model,
        "--severity", str(args.severity),
        *base_url,
        env=driver_env(args.arm, os.environ),
        cwd=run_dir,
    )  # fmt: skip
    return EXIT_SCOUT_FAILED if await process.wait() else 0


async def run(args: argparse.Namespace) -> int:
    """Start the bridge, run the driver and return its exit code.

    Args:
        args: The parsed arguments.

    Returns:
        The driver's exit code, ``EXIT_SCOUT_FAILED``, or 5 if a stub run booked a cost.
    """
    run_dir: Path = args.run_dir.resolve()
    if (run_dir / "results").exists():
        raise SystemExit(f"{run_dir} holds an earlier run: pass a new --run-dir")
    run_dir.mkdir(parents=True, exist_ok=True)
    database = STUB_DATABASE if args.arm == "stub" else PAID_DATABASE
    since = datetime.now(UTC)
    stack = open_stack(args.arm, database, args.cap_usd, since)
    if args.arm != "stub":
        await stack.assert_gateway_fresh()
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "arm": args.arm,
                "started_at": since.isoformat(),
                "cap_usd": args.cap_usd,
                "owner_go": args.owner_go,
            },
            indent=2,
        )
    )
    server = uvicorn.Server(
        uvicorn.Config(
            create_app(stack.deps), host="127.0.0.1", port=BRIDGE_PORT, log_level="warning"
        )
    )
    serving = asyncio.create_task(server.serve())
    try:
        while not server.started:
            await asyncio.sleep(0.1)
        driver = await asyncio.create_subprocess_exec(
            str(args.memaudit_python),
            "-I",
            str(HERE / "memaudit_driver.py"),
            "--memaudit-dir", str(args.memaudit_dir),
            "--bridge-url", f"http://127.0.0.1:{BRIDGE_PORT}",
            "--run-dir", str(run_dir),
            "--runs", *args.runs,
            "--users", *args.users,
            "--tasks-per-user", str(args.tasks_per_user),
            "--max-turns", str(MAX_TURNS),
            "--severity", str(args.severity),
            env=driver_env(args.arm, os.environ),
            cwd=run_dir,
        )  # fmt: skip
        code = await driver.wait()
        if code == 0:
            # The last questions' extraction and summaries land after the driver exits.
            await wait_for_gateway_idle(stack.deps.es)
        remaining_usd = args.cap_usd - await stack.deps.guard.spent()
    finally:
        server.should_exit = True
        await serving
        await stack.close()
    if code == 0 and not args.skip_scout:
        code = await _run_scout(args, run_dir, remaining_usd)
    if args.arm == "stub":
        rows = await _assert_no_cost_in_stub_database()
        print(f"stub database api_costs rows: {rows}")
        if rows:
            return 5
    return code


def main(argv: Sequence[str] | None = None) -> int:
    """Parse the arguments and run the pilot.

    Args:
        argv: Command-line arguments.

    Returns:
        The exit code.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--arm", choices=sorted(GATEWAY_CONTAINERS), required=True)
    parser.add_argument("--users", nargs="+", default=list(DEFAULT_USERS))
    parser.add_argument("--tasks-per-user", type=int, default=8)
    parser.add_argument("--runs", nargs="+", default=["A", "B"])
    parser.add_argument("--cap-usd", type=float, default=PILOT_CAP_USD)
    parser.add_argument("--owner-go", default=None)
    parser.add_argument("--run-dir", type=Path, default=None)
    parser.add_argument("--memaudit-dir", type=Path, default=HOME / "memaudit")
    parser.add_argument("--memaudit-python", type=Path, default=HOME / "venv" / "bin" / "python")
    parser.add_argument("--severity", type=Path, default=HERE / "severity.json")
    parser.add_argument("--scout-python", type=Path, default=HOME / "scout-venv" / "bin" / "python")
    parser.add_argument("--scout-model", default="openai/gpt-5.4-mini")
    parser.add_argument("--skip-scout", action="store_true")
    args = parser.parse_args(argv)
    if args.run_dir is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.run_dir = HOME / "runs" / f"{args.arm}-{stamp}"
    # The children start in the run directory, so a relative path must not reach them.
    args.severity = args.severity.resolve()
    args.memaudit_dir = args.memaudit_dir.resolve()
    refuse_paid_run_without_go(args.arm, args.owner_go)
    refuse_a_second_real_gateway(args.arm)
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())
