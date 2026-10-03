"""FRE-1511 / ADR-0154 D7: the decision arm and the timing arm, sent direct to llama.cpp.

Design A only: an isolated planner call, in a chosen planner mode. Designs B and C are rejected in
ADR-0154 and are not ported.

    decide  per fixture and trial, the planner decides decline or expand        -> rows/<tag>/decide.jsonl
    timing  per fixture, time to first token of the primary alone (T1), then of
            the planner followed by the primary (T3)                            -> rows/<tag>/timing.jsonl

Every call is one JSON row, appended when it lands. A rerun resumes. No tool is ever executed.

Run from the repo root:

    uv run python -m scripts.eval.fre1537.replay --run-dir <run> --mode thinking_off
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import fingerprint as fp_mod
from scripts.eval.fre1537.common import RunPaths, append_jsonl, done_keys, read_jsonl, utc_now
from scripts.eval.fre1537.llama import (
    DEFAULT_MODEL,
    DEFAULT_URL,
    MODES,
    Inputs,
    PlannerMode,
    load_inputs,
    parse_plan,
    planner_body,
    primary_body,
    prime_body,
    stream,
)


def _kind(inputs: Inputs, label: str) -> str:
    return "single" if inputs.captured[label].get("history") is None else "followup"


def run_decide(
    client: httpx.Client,
    url: str,
    model: str,
    paths: RunPaths,
    inputs: Inputs,
    mode: PlannerMode,
    digest: str | None,
    labels: Sequence[str],
    trials: int,
) -> None:
    """Run the decision arm: one planner call per fixture and trial.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        paths: The run paths.
        inputs: The run inputs.
        mode: The planner mode.
        digest: Optional digest text inserted after the history and before the query.
        labels: Fixture labels to run.
        trials: Draws per fixture.
    """
    seen = done_keys(paths.decide)
    for trial in range(trials):
        for label in labels:
            if (label, trial) in seen:
                continue
            row: dict[str, object] = {
                "arm": "decide",
                "tag": paths.tag,
                "label": label,
                "trial": trial,
                "expected": inputs.captured[label]["expected"],
                "kind": _kind(inputs, label),
                "mode": mode.name,
                "ts": utc_now(),
            }
            try:
                result = stream(client, url, model, planner_body(inputs, label, mode, digest))
                plan = parse_plan(str(result["content"]))
                row.update(result)
                row["plan"] = plan
                row["declined"] = plan.get("declined")
            except (httpx.HTTPError, ValueError) as exc:
                row["error"] = repr(exc)[:300]
            append_jsonl(paths.decide, row)
            print(
                "decide",
                trial,
                label,
                row.get("secs"),
                row.get("declined"),
                row.get("error", "")[:80],
                flush=True,
            )


def run_timing(
    client: httpx.Client,
    url: str,
    model: str,
    paths: RunPaths,
    inputs: Inputs,
    mode: PlannerMode,
    labels: Sequence[str],
    digest: str | None = None,
) -> None:
    """Run the timing arm: time to first token with and without the planner call.

    Per fixture: prime the previous turn's prefix, T1 the primary alone, prime again, then the planner
    call followed by the primary (T3). The engine's ``cache_n`` and ``prompt_n`` are recorded on every call.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        paths: The run paths.
        inputs: The run inputs.
        mode: The planner mode.
        labels: Fixture labels to run.
        digest: Optional digest text inserted after the history and before the query.
    """
    seen = done_keys(paths.timing)
    for label in labels:
        if (label, 0) in seen:
            continue
        row: dict[str, object] = {
            "arm": "timing",
            "tag": paths.tag,
            "label": label,
            "trial": 0,
            "expected": inputs.captured[label]["expected"],
            "kind": _kind(inputs, label),
            "ts": utc_now(),
        }
        try:
            stream(client, url, model, prime_body(inputs, label))
            row["T1_primary"] = stream(
                client, url, model, primary_body(inputs, label, max_tokens=1)
            )
            stream(client, url, model, prime_body(inputs, label))
            planner = stream(client, url, model, planner_body(inputs, label, mode, digest))
            planner["plan"] = parse_plan(str(planner.pop("content")))
            row["T3_planner"] = planner
            row["T3_primary"] = stream(
                client, url, model, primary_body(inputs, label, max_tokens=1)
            )
        except (httpx.HTTPError, ValueError) as exc:
            row["error"] = repr(exc)[:300]
        append_jsonl(paths.timing, row)
        print("timing", label, row.get("error", "ok")[:80], flush=True)  # type: ignore[index]


def common_arguments(parser: argparse.ArgumentParser) -> None:
    """Add the arguments that every llama.cpp step shares."""
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--mode", choices=sorted(MODES), default="thinking_off")
    parser.add_argument("--tag", default=None, help="rows directory name; default is the mode")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--quant", default=None, help="model quant, when the engine does not report it"
    )
    parser.add_argument(
        "--engine-build", default=None, help="engine build, when the engine does not report it"
    )
    parser.add_argument(
        "--digest-file", type=Path, default=None, help="digest text for a digest run"
    )


def prepare(
    args: argparse.Namespace, client: httpx.Client
) -> tuple[RunPaths, Inputs, PlannerMode, str | None]:
    """Resolve the tag, load the inputs, and write or check the fingerprint.

    Args:
        args: Parsed arguments from :func:`common_arguments`.
        client: HTTP client, for the engine's ``/props``.

    Returns:
        The run paths, inputs, planner mode and digest text.
    """
    digest = args.digest_file.read_text().strip() if args.digest_file else None
    tag = args.tag or (f"{args.mode}-digest" if digest else args.mode)
    paths = RunPaths(args.run_dir, tag)
    inputs = load_inputs(paths)
    mode = MODES[args.mode]
    fingerprint = fp_mod.build_fingerprint(
        client, args.url, args.model, mode, inputs, paths, args.quant, args.engine_build, digest
    )
    fp_mod.ensure_compatible(paths, fingerprint)
    return paths, inputs, mode, digest


def main(argv: Sequence[str] | None = None) -> int:
    """Run the decision arm, the timing arm, or both.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common_arguments(parser)
    parser.add_argument("--arms", default="decide,timing")
    parser.add_argument("--labels", default="")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args(argv)
    with httpx.Client(timeout=900.0) as client:
        paths, inputs, mode, digest = prepare(args, client)
        labels = args.labels.split(",") if args.labels else list(inputs.captured)
        for arm in args.arms.split(","):
            if arm == "decide":
                run_decide(
                    client, args.url, args.model, paths, inputs, mode, digest, labels, args.trials
                )
            elif arm == "timing":
                run_timing(client, args.url, args.model, paths, inputs, mode, labels, digest)
            else:
                raise SystemExit(f"unknown arm {arm!r}: use decide or timing")
    rows = len(read_jsonl(paths.decide)) + len(read_jsonl(paths.timing))
    print(f"rows in {paths.rows}: {rows}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
