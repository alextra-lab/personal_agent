"""FRE-1511 / ADR-0154 D7 and Appendix A1: the long-history arm.

Design A in a chosen planner mode, with synthetic histories built from the four fixture conversations at
8,000, 30,000 and 60,000 characters. Each size runs a cold planner call, then a primary call, then the next
turn's planner call whose history extends the first by one exchange. Timing only.

    uv run python -m scripts.eval.fre1537.longhist --run-dir <run> --mode thinking_off
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Sequence

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537 import fingerprint as fp_mod
from scripts.eval.fre1537 import render
from scripts.eval.fre1537.common import RunPaths, append_jsonl, read_jsonl
from scripts.eval.fre1537.fixtures import load_histories
from scripts.eval.fre1537.llama import (
    Inputs,
    PlannerMode,
    parse_plan,
    planner_request,
    primary_body,
    reference_label,
    stream,
)
from scripts.eval.fre1537.replay import common_arguments, prepare, prepare_cloud

SIZES = (8000, 30000, 60000)
FIRST_QUERY = "Thanks. Which of the three did you say is cheapest to install?"
FIRST_REPLY = "Air-source is the cheapest to install."
SECOND_QUERY = "So Tempo is the one with red days?"
MAX_TOKENS = 4096


def render_history(n_chars: int, nonce: str) -> str:
    """Build a role-labelled history of at least ``n_chars`` characters from the fixture conversations.

    Args:
        n_chars: Minimum length.
        nonce: A per-run token in the first line, so that no size reuses the engine's cached prefix.

    Returns:
        The history text.
    """
    pairs = list(load_histories().values())
    lines = [f"user: (session {nonce}) Hello."]
    i = 0
    while sum(len(x) for x in lines) < n_chars:
        user, assistant = pairs[i % len(pairs)]
        lines += [f"user: {user} (part {i})", f"assistant: {assistant}"]
        i += 1
    return "\n".join(lines)


def _call(
    client: httpx.Client,
    url: str,
    model: str,
    inputs: Inputs,
    mode: PlannerMode,
    history: str,
    query: str,
    cloud: cloud_mod.CloudSession | None = None,
    budget: cloud_mod.Budget | None = None,
) -> dict[str, object]:
    user = render.build_user_message(history, None, query)
    if cloud is not None:
        if budget:
            budget.check(cloud.target, inputs.system, user)
        result = cloud.call(inputs.system, user)
    else:
        body = planner_request(
            inputs.system, user, inputs.body(reference_label(inputs)), mode, MAX_TOKENS
        )
        result = stream(client, url, model, body)
    result["plan"] = parse_plan(str(result.pop("content")))
    return result


def run_longhist(
    client: httpx.Client,
    url: str,
    model: str,
    paths: RunPaths,
    inputs: Inputs,
    mode: PlannerMode,
    sizes: Sequence[int] = SIZES,
    *,
    cloud: cloud_mod.CloudSession | None = None,
    budget: cloud_mod.Budget | None = None,
) -> None:
    """Run the long-history arm, one row per size. A size that already has a row is skipped.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        paths: The run paths.
        inputs: The run inputs.
        mode: The planner mode.
        sizes: History sizes in characters.
        cloud: A managed-deployment session. The primary call between the two planner calls primes the llama.cpp
            prefix cache and decides no threshold, so a managed row has none.
        budget: The spending cap, for a managed deployment.
    """
    seen = {int(str(r["size_chars"])) for r in read_jsonl(paths.longhist)}
    for size in sizes:
        if size in seen:
            continue
        first = render_history(size, secrets.token_hex(4))
        cold = _call(client, url, model, inputs, mode, first, FIRST_QUERY, cloud, budget)
        row: dict[str, object] = {
            "arm": "longhist",
            "tag": paths.tag,
            "size_chars": size,
            "cold": cold,
        }
        if cloud is None:
            fp_mod.fill_engine_build(paths, cold.get("system_fingerprint"))
            between = stream(
                client, url, model, primary_body(inputs, reference_label(inputs), max_tokens=1)
            )
            between.pop("content", None)
            row["primary_between"] = between
        else:
            fp_mod.fill_served_model(paths, cold.get("served_model"))
        extended_history = f"{first}\nuser: {FIRST_QUERY}\nassistant: {FIRST_REPLY}"
        row["extended"] = _call(
            client, url, model, inputs, mode, extended_history, SECOND_QUERY, cloud, budget
        )
        append_jsonl(paths.longhist, row)
        print("longhist", size, cold["secs"], row["extended"]["secs"], flush=True)  # type: ignore[index]


def main(argv: Sequence[str] | None = None) -> int:
    """Run the long-history arm.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common_arguments(parser)
    args = parser.parse_args(argv)
    if args.deployment:
        paths, inputs, target, _digest, budget = prepare_cloud(args)
        mode = PlannerMode(target.mode_name, dict(target.declared))
        with cloud_mod.CloudSession(target) as session:
            run_longhist(
                None,  # type: ignore[arg-type]
                "",
                "",
                paths,
                inputs,
                mode,
                cloud=session,
                budget=budget,
            )
        print(f"spent in {args.run_dir}: {budget.spent():.4f} USD of {args.max_usd:g}")
        print(f"rows in {paths.longhist}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
        return 0
    with httpx.Client(timeout=900.0) as client:
        paths, inputs, mode, _digest = prepare(args, client)
        run_longhist(client, args.url, args.model, paths, inputs, mode)
    print(f"rows in {paths.longhist}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
