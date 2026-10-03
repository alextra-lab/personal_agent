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
import json
import sys
from collections.abc import Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537 import fingerprint as fp_mod
from scripts.eval.fre1537.common import RunPaths, append_jsonl, done_keys, read_jsonl, utc_now
from scripts.eval.fre1537.llama import (
    DEFAULT_MODEL,
    DEFAULT_URL,
    MODE_NAMES,
    Inputs,
    PlannerMode,
    load_inputs,
    parse_plan,
    planner_body,
    planner_user,
    primary_body,
    prime_body,
    resolve_mode,
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
    *,
    cloud: cloud_mod.CloudSession | None = None,
    budget: cloud_mod.Budget | None = None,
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
        cloud: A managed-deployment session. When set, the call goes through the production client
            and ``client``, ``url`` and ``model`` are not used.
        budget: The spending cap, for a managed deployment.
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
                if cloud is not None:
                    user = planner_user(inputs, label, digest)
                    row["cost_usd"] = (
                        budget.check(cloud.target, inputs.system, user) if budget else 0.0
                    )
                    result = cloud.call(inputs.system, user)
                    fp_mod.fill_served_model(paths, result.get("served_model"))
                else:
                    result = stream(client, url, model, planner_body(inputs, label, mode, digest))
                    fp_mod.fill_engine_build(paths, result.get("system_fingerprint"))
                plan = parse_plan(str(result["content"]))
                row.update(result)
                row["plan"] = plan
                row["declined"] = plan.get("declined")
            except (httpx.HTTPError, ValueError, cloud_mod.CloudCallError) as exc:
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
            fp_mod.fill_engine_build(paths, planner.get("system_fingerprint"))
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
    parser.add_argument(
        "--mode", choices=sorted({*MODE_NAMES, *cloud_mod.MODE_NAMES}), default="thinking_off"
    )
    parser.add_argument(
        "--deployment",
        default=None,
        help="catalog key of a managed deployment (OVH, Anthropic); default is the llama.cpp path",
    )
    parser.add_argument(
        "--candidate",
        default=None,
        help="JSON of a candidate `planner` mode for a managed deployment whose catalog has none yet",
    )
    parser.add_argument(
        "--max-usd",
        type=float,
        default=None,
        help="spending cap in USD over every tag of the run directory; required with --deployment",
    )
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
    if args.mode not in MODE_NAMES:
        raise SystemExit(f"--mode {args.mode} needs --deployment (a managed deployment)")
    digest = args.digest_file.read_text().strip() if args.digest_file else None
    tag = args.tag or (f"{args.mode}-digest" if digest else args.mode)
    paths = RunPaths(args.run_dir, tag)
    inputs = load_inputs(paths)
    mode = resolve_mode(args.mode)
    fingerprint = fp_mod.build_fingerprint(
        client, args.url, args.model, mode, inputs, paths, args.quant, args.engine_build, digest
    )
    fp_mod.ensure_compatible(paths, fingerprint)
    return paths, inputs, mode, digest


def prepare_cloud(
    args: argparse.Namespace,
) -> tuple[RunPaths, Inputs, cloud_mod.CloudTarget, str | None, cloud_mod.Budget]:
    """Resolve a managed deployment, load the inputs, and write or check the fingerprint.

    Args:
        args: Parsed arguments from :func:`common_arguments`, with ``--deployment`` set.

    Returns:
        The run paths, inputs, target, digest text and spending cap.

    Raises:
        SystemExit: On an argument that a managed run refuses, or a tag of another configuration.
    """
    if args.max_usd is None:
        raise SystemExit("--max-usd is required with --deployment: these calls are paid")
    if args.mode not in cloud_mod.MODE_NAMES:
        raise SystemExit(f"--mode {args.mode}: a managed deployment takes planner or default")
    candidate = json.loads(args.candidate) if args.candidate else None
    target = cloud_mod.resolve_target(args.deployment, args.mode, candidate=candidate)
    digest = args.digest_file.read_text().strip() if args.digest_file else None
    tag = args.tag or "-".join([args.deployment, args.mode, *(["digest"] if digest else [])])
    paths = RunPaths(args.run_dir, tag)
    inputs = load_inputs(paths)
    fp_mod.ensure_compatible(paths, fp_mod.build_cloud_fingerprint(target, inputs, paths, digest))
    return paths, inputs, target, digest, cloud_mod.Budget(args.run_dir, args.max_usd)


def _main_cloud(args: argparse.Namespace) -> int:
    paths, inputs, target, digest, budget = prepare_cloud(args)
    mode = PlannerMode(target.mode_name, dict(target.declared))
    labels = args.labels.split(",") if args.labels else list(inputs.captured)
    with cloud_mod.CloudSession(target) as session:
        run_decide(
            None,  # type: ignore[arg-type]
            "",
            "",
            paths,
            inputs,
            mode,
            digest,
            labels,
            args.trials,
            cloud=session,
            budget=budget,
        )
    print(f"spent in {args.run_dir}: {budget.spent():.4f} USD of {args.max_usd:g}")
    print(
        f"rows in {paths.rows}. Next: longhist, then score --run-dir {args.run_dir} --tag {paths.tag}"
    )
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the decision arm, the timing arm, or both.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common_arguments(parser)
    parser.add_argument("--arms", default=None, help="default: decide,timing (managed: decide)")
    parser.add_argument("--labels", default="")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args(argv)
    if args.deployment:
        if args.arms not in (None, "decide"):
            raise SystemExit(
                "--arms timing is a llama.cpp prefix-cache arm and decides no D7 threshold: "
                "a managed run uses --arms decide"
            )
        return _main_cloud(args)
    with httpx.Client(timeout=900.0) as client:
        paths, inputs, mode, digest = prepare(args, client)
        labels = args.labels.split(",") if args.labels else list(inputs.captured)
        for arm in (args.arms or "decide,timing").split(","):
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
