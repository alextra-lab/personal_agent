"""FRE-1598 (ADR-0156 A5, T-D2): run the benefit A/B, then pair the arms.

Each scenario of ``part2_scenarios.yaml`` runs as one full turn on each eval gateway, through
``scripts/eval/fre1590/help.py``:

- treatment (:9003): the skill catalog; ``search_logs`` and ``measure_model_latency`` are hidden until a
  ``read_skill`` reveals them, as in production;
- control (:9002): no skill catalog (``docker-compose.fre1590.yml``), and both tools written into the
  session's ``revealed_tools`` before ``/chat`` (``seed.py``), so they are visible from call 1.

Before the first turn, ``run`` writes ``<run>/fingerprint.json`` (the pins). A later ``run`` with other
pins stops. Run from the repo root, after the override is up:

    export NEO4J_PASSWORD=... POSTGRES_PASSWORD=...   # the eval passwords only
    uv run python -m scripts.eval.fre1598.run run --run-dir <run> --props-file <props.json>
        --server-args '<llama-server arguments>' --limit 1   (one command line)
    uv run python -m scripts.eval.fre1598.run report --run-dir <run>
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess  # nosec B404 — fixed git argv, no shell
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
import yaml  # type: ignore[import-untyped]
from scripts.eval.eval_isolation import create_eval_driver
from scripts.eval.fre1337_intent_probe.substrate import EVAL_ARMS
from scripts.eval.fre1590 import help as part2
from scripts.eval.fre1594 import spike
from scripts.eval.fre1598.seed import EvalRevealSeeder

HERE = Path(__file__).resolve().parent
SCENARIOS_FILE = HERE / "part2_scenarios.yaml"
MODELS_FILE = part2.REPO_ROOT / "config" / "models.yaml"
MODEL_KEY = "qwen3.8-flash-next"
CONTROL_REVEAL = ("search_logs", "measure_model_latency")
LLAMA_URL = "http://127.0.0.1:8600/v1/chat/completions"


def catalog_entry(key: str, models_file: Path = MODELS_FILE) -> dict[str, object]:
    """Return one deployment's catalog entry, verbatim (its sampling and thinking mode).

    Args:
        key: The catalog key.
        models_file: ``config/models.yaml``.

    Returns:
        The entry.

    Raises:
        KeyError: No such key.
    """
    return dict(yaml.safe_load(models_file.read_text())["models"][key])


def scenario_pins(scenarios: Sequence[part2.Scenario]) -> list[dict[str, str]]:
    """Return each scenario id with the SHA-256 of its prompt and of its ``expect`` text."""
    return [
        {
            "id": s.id,
            "prompt_sha256": part2.sha256_text(s.prompt),
            "expect_sha256": part2.sha256_text(s.expect),
        }
        for s in scenarios
    ]


def git_head(root: Path) -> str:
    """Return the commit that the eval image was built from."""
    out = subprocess.run(  # nosec B603 B607 — fixed argv
        ["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, check=True
    )
    return out.stdout.strip()


async def gateway_builds(http: httpx.AsyncClient) -> dict[str, object]:
    """Return each arm's ``build_fingerprint`` from ``/health``."""
    builds: dict[str, object] = {}
    for arm in part2.ARMS:
        resp = await http.get(f"{EVAL_ARMS[arm]}/health", timeout=10.0)
        resp.raise_for_status()
        builds[arm] = resp.json().get("build_fingerprint")
    return builds


def build_fingerprint(
    *,
    scenarios: Sequence[part2.Scenario],
    builds: Mapping[str, object],
    head: str,
    props: Mapping[str, object],
    server_args: str,
) -> dict[str, object]:
    """Assemble the run's pins (ticket: scenarios and hashes, model, build, sampling).

    Args:
        scenarios: Every scenario of the run.
        builds: Each arm's ``build_fingerprint``.
        head: The git HEAD of the build tree.
        props: The llama.cpp ``/props`` summary (chat template hash, build, cache settings).
        server_args: The llama-server launch arguments, verbatim.

    Returns:
        The fingerprint.

    Raises:
        SystemExit: The arms run different builds, or ``/props`` could not be read.
    """
    if len(set(map(str, builds.values()))) != 1:
        raise SystemExit(f"the arms run different builds: {dict(builds)}")
    if "error" in props:
        raise SystemExit(f"no llama.cpp /props ({props['error']}): pass --props-file")
    return {
        "model_key": MODEL_KEY,
        "catalog_entry": catalog_entry(MODEL_KEY),
        "gateway_builds": dict(builds),
        "git_head": head,
        "llama_props": dict(props),
        "llama_server_args": server_args,
        "control_reveal": list(CONTROL_REVEAL),
        "scenarios": scenario_pins(scenarios),
    }


def pin_run(run_dir: Path, fingerprint: Mapping[str, object]) -> None:
    """Write the fingerprint, or stop when the run directory holds another one.

    Args:
        run_dir: The run directory.
        fingerprint: The pins of this invocation.

    Raises:
        SystemExit: The directory's fingerprint differs.
    """
    path = run_dir / "fingerprint.json"
    if path.exists():
        old = json.loads(path.read_text())
        if old != json.loads(json.dumps(fingerprint)):
            changed = sorted(
                k for k in set(old) | set(fingerprint) if old.get(k) != fingerprint.get(k)
            )
            raise SystemExit(
                f"{path} holds other pins ({', '.join(changed)}): use a new run directory"
            )
        return
    run_dir.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(fingerprint, indent=2) + "\n")


async def _run(args: argparse.Namespace) -> int:
    from scripts.eval.gateway_freshness import assert_gateway_fresh

    run_dir = Path(args.run_dir)
    every = part2.load_scenarios(SCENARIOS_FILE)
    ids = [i.strip() for i in args.ids.split(",") if i.strip()]
    scenarios = part2.select(every, ids, args.limit)
    with httpx.Client(timeout=10.0) as client:
        props = spike.read_props(client, LLAMA_URL, args.props_file)
    cfg = part2.RunConfig(
        run_dir=run_dir,
        model=MODEL_KEY,
        arms=part2.ARMS,
        settle_s=args.settle_s,
        control_reveal=CONTROL_REVEAL,
    )
    async with httpx.AsyncClient() as http, httpx.AsyncClient() as es:
        for arm in part2.ARMS:
            print(
                f"{arm} gateway fresh: {await assert_gateway_fresh(http, EVAL_ARMS[arm], Path(args.build_root))}"
            )
        fingerprint = build_fingerprint(
            scenarios=every,
            builds=await gateway_builds(http),
            head=git_head(Path(args.build_root)),
            props=props,
            server_args=args.server_args,
        )
        pin_run(run_dir, fingerprint)
        seeder = await EvalRevealSeeder.connect()
        driver = create_eval_driver()
        try:
            rows = await part2.run_part2(
                cfg, scenarios, http=http, es=es, driver=driver, seeder=seeder
            )
        finally:
            await driver.close()
            await seeder.close()
    errors = sum("error" in r for r in rows)
    print(f"{len(rows)} rows written, {errors} errors -> {part2.rows_path(run_dir, MODEL_KEY)}")
    return 1 if errors else 0


def _report(args: argparse.Namespace) -> int:
    path = part2.rows_path(Path(args.run_dir), MODEL_KEY)
    expected = [s.id for s in part2.load_scenarios(SCENARIOS_FILE)]
    report = part2.build_report(part2.read_rows(path), MODEL_KEY, expected)
    (path.parent / "part2_report.json").write_text(json.dumps(report, indent=2) + "\n")
    sys.stdout.write(part2.render_report(report))
    return 0 if report["paired"] else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments, then run the turns or pair the arms.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        ``run``: 0 when no row has an error. ``report``: 0 when every scenario has both arms.
    """
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="command", required=True)
    run = sub.add_parser("run", help="run each scenario on each arm")
    run.add_argument("--run-dir", required=True)
    run.add_argument("--limit", type=int, default=0, help="at most this many scenarios")
    run.add_argument("--ids", default="", help="comma-separated scenario ids")
    run.add_argument("--settle-s", type=float, default=20.0)
    run.add_argument("--props-file", type=Path, help="a saved llama-server /props reply")
    run.add_argument("--server-args", required=True, help="the llama-server launch arguments")
    run.add_argument(
        "--build-root", default="/opt/seshat", help="the tree the eval image was built from"
    )
    report = sub.add_parser("report", help="pair the arms of each scenario")
    report.add_argument("--run-dir", required=True)
    args = p.parse_args(argv)
    if args.command == "run":
        return asyncio.run(_run(args))
    return _report(args)


if __name__ == "__main__":
    sys.exit(main())
