"""FRE-1511 / ADR-0154 D7: the decision arm and the paired arm, sent direct to llama.cpp.

Design A only: an isolated planner call, in a chosen planner mode. Designs B and C are rejected in
ADR-0154 and are not ported.

    decide  per fixture and trial, the planner decides decline or expand        -> rows/<tag>/decide.jsonl
    paired  per decline fixture and draw, the same turn routed SINGLE and routed
            to a declining planner; the difference is the planner's added delay
            (ADR-0154 Amendment 1)                                              -> rows/<tag>/paired.jsonl

Every decision call and every pair is one JSON row, appended when it lands. A rerun resumes. No tool is
ever executed.

Run from the repo root:

    uv run python -m scripts.eval.fre1537.replay --run-dir <run> --mode thinking_off
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537 import fingerprint as fp_mod
from scripts.eval.fre1537 import render
from scripts.eval.fre1537.common import (
    PAIRED_DRAWS,
    RunPaths,
    append_jsonl,
    done_keys,
    done_pair_keys,
    read_jsonl,
    utc_now,
)
from scripts.eval.fre1537.fixtures import load_histories
from scripts.eval.fre1537.llama import (
    DEFAULT_MODEL,
    DEFAULT_URL,
    MODE_NAMES,
    Inputs,
    PlannerMode,
    load_inputs,
    parse_plan,
    planner_body,
    planner_request,
    planner_user,
    primary_body,
    prime_body,
    resolve_mode,
    stream,
)
from scripts.eval.fre1537.score import classify

ARMS = ("decide", "paired")


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
        digest: Optional digest text, sent as the production memory_recall tool result.
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
                    user = planner_user(inputs, label)
                    row["cost_usd"] = (
                        budget.check(cloud.target, inputs.system, f"{user}\n{digest or ''}")
                        if budget
                        else 0.0
                    )
                    result = cloud.call(inputs.system, user, digest)
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


@dataclass(frozen=True)
class PairBodies:
    """The four requests of one pair, for one arm's nonce (ADR-0154 Amendment 1, 1.1).

    Attributes:
        prime_primary: The previous turn's primary request.
        primary: This turn's primary request, stopped at its first token.
        prime_planner: The previous turn's planner request.
        planner: This turn's planner request.
    """

    prime_primary: Mapping[str, object]
    primary: Mapping[str, object]
    prime_planner: Mapping[str, object]
    planner: Mapping[str, object]


def nonce_line(nonce: str) -> str:
    """Return the line that starts the system message of every request of one arm."""
    return f"probe nonce {nonce}"


def with_nonce(body: Mapping[str, object], nonce: str) -> dict[str, object]:
    """Return a copy of a request whose system message starts with the arm's nonce line.

    Args:
        body: A request body whose first message is the system message.
        nonce: The arm's nonce.

    Returns:
        The copy. No other part of the request changes.

    Raises:
        ValueError: If the first message is not a system message with text content.
    """
    messages = [dict(m) for m in body["messages"]]  # type: ignore[attr-defined]
    first = messages[0]
    if first.get("role") != "system" or not isinstance(first.get("content"), str):
        raise ValueError("a paired request must start with a system message of plain text")
    first["content"] = f"{nonce_line(nonce)}\n{first['content']}"
    return {**body, "messages": messages}


def _measured(result: dict[str, object]) -> dict[str, object]:
    result.pop("content", None)
    return result


def _timings(call: Mapping[str, object]) -> tuple[float, float] | None:
    t = call.get("timings")
    if not isinstance(t, Mapping):
        return None
    cache_n, prompt_n = t.get("cache_n"), t.get("prompt_n")
    if not isinstance(cache_n, int | float) or not isinstance(prompt_n, int | float):
        return None
    return float(cache_n), float(prompt_n)


def _num(x: object) -> float | None:
    return float(x) if isinstance(x, int | float) and not isinstance(x, bool) else None


def pair_outcome(
    single: Mapping[str, Mapping[str, object]], planner: Mapping[str, Mapping[str, object]]
) -> dict[str, object]:
    """Decide the planner's verdict, the added delay and the void reasons of one pair.

    A measured call is compared with the prime before it in the same arm: it is void when its
    ``timings.cache_n`` exceeds the prime's prompt tokens (``cache_n + prompt_n``), because the
    engine then reused more than the previous turn left (ADR-0154 Amendment 1, 1.1).

    Args:
        single: The ``SINGLE`` arm's calls: ``prime`` and ``primary``.
        planner: The planner arm's calls: ``prime_planner``, ``prime_primary``, ``planner`` and
            ``primary``. The measured planner call holds ``plan``.

    Returns:
        ``declined``, ``added_ms`` (null when an input is missing) and ``void`` (null, or the list
        of reasons).
    """
    checks = (
        ("planner_cache_reuse", planner["prime_planner"], planner["planner"]),
        ("primary_cache_reuse_single", single["prime"], single["primary"]),
        ("primary_cache_reuse_planner", planner["prime_primary"], planner["primary"]),
    )
    reasons: list[str] = []
    for reason, prime, measured in checks:
        before, after = _timings(prime), _timings(measured)
        if before is None or after is None:
            if "timings_missing" not in reasons:
                reasons.append("timings_missing")
        elif after[0] > before[0] + before[1]:
            reasons.append(reason)
    planner_secs = _num(planner["planner"].get("secs"))
    with_planner = _num(planner["primary"].get("ttft_any"))
    alone = _num(single["primary"].get("ttft_any"))
    added = (
        None
        if planner_secs is None or with_planner is None or alone is None
        else round((planner_secs + with_planner - alone) * 1000)
    )
    return {
        "declined": classify(planner["planner"].get("plan")) == "decline",
        "added_ms": added,
        "void": reasons or None,
    }


def run_pair(
    client: httpx.Client,
    url: str,
    model: str,
    build: Callable[[str], PairBodies],
    single_first: bool,
) -> dict[str, object]:
    """Run both arms of one pair and return the row fields they produce.

    Each arm takes a new nonce, so no arm reuses a prompt that another arm or draw cached.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        build: Builds the pair's requests for one nonce. The nonce line is already in them.
        single_first: Run the ``SINGLE`` arm first.

    Returns:
        ``order``, ``single``, ``planner``, ``declined``, ``added_ms`` and ``void``.
    """

    def single_arm() -> dict[str, object]:
        nonce = secrets.token_hex(4)
        b = build(nonce)
        prime = _measured(stream(client, url, model, b.prime_primary))
        return {
            "nonce": nonce,
            "prime": prime,
            "primary": _measured(stream(client, url, model, b.primary)),
        }

    def planner_arm() -> dict[str, object]:
        nonce = secrets.token_hex(4)
        b = build(nonce)
        out: dict[str, object] = {"nonce": nonce}
        out["prime_planner"] = _measured(stream(client, url, model, b.prime_planner))
        out["prime_primary"] = _measured(stream(client, url, model, b.prime_primary))
        planner = stream(client, url, model, b.planner)
        planner["plan"] = parse_plan(str(planner.pop("content")))
        out["planner"] = planner
        out["primary"] = _measured(stream(client, url, model, b.primary))
        return out

    if single_first:
        single = single_arm()
        planner = planner_arm()
    else:
        planner = planner_arm()
        single = single_arm()
    return {
        "order": "single_first" if single_first else "planner_first",
        "single": single,
        "planner": planner,
        **pair_outcome(single, planner),  # type: ignore[arg-type]
    }


def previous_planner_user(inputs: Inputs, label: str) -> str:
    """Return the planner user message of the turn before a fixture's turn.

    A single-turn fixture has no previous turn, so its message is ``.``. A follow-up's previous
    turn is its scripted turn 1: no history, and turn 1's user message as the query, built with
    the production ``build_planner_user_message``.

    Args:
        inputs: The run inputs.
        label: A fixture label.

    Returns:
        The user message.
    """
    history = inputs.captured[label].get("history")
    if history is None:
        return "."
    first_user, _ = load_histories()[str(history)]
    max_chars = int(str(inputs.prompts.get("history_max_chars", 60000)))
    built = render.build_planner_request(inputs.system, [], first_user, None, max_chars)
    return str(built["messages"][1]["content"])  # type: ignore[index]


def fixture_pair_bodies(
    inputs: Inputs, label: str, mode: PlannerMode, digest: str | None, nonce: str
) -> PairBodies:
    """Build the four requests of one decline fixture's pair, for one arm's nonce.

    Args:
        inputs: The run inputs.
        label: A fixture label.
        mode: The planner mode.
        digest: Optional digest text, carried by the measured planner call only.
        nonce: The arm's nonce.

    Returns:
        The requests, each with the nonce line at the start of its system message.
    """
    prime_planner = planner_request(
        inputs.system, previous_planner_user(inputs, label), inputs.body(label), mode, 1
    )
    return PairBodies(
        prime_primary=with_nonce(prime_body(inputs, label), nonce),
        primary=with_nonce(primary_body(inputs, label, max_tokens=1), nonce),
        prime_planner=with_nonce(prime_planner, nonce),
        planner=with_nonce(planner_body(inputs, label, mode, digest), nonce),
    )


def run_paired(
    client: httpx.Client,
    url: str,
    model: str,
    paths: RunPaths,
    inputs: Inputs,
    mode: PlannerMode,
    labels: Sequence[str] | None = None,
    digest: str | None = None,
    draws: int = PAIRED_DRAWS,
) -> None:
    """Run the paired arm over the decline fixtures (ADR-0154 Amendment 1, 1.1).

    A pair runs the ``SINGLE`` arm first when (index + draw) is even, where the index is the
    fixture's position in the sorted decline labels. A rerun resumes on the fixture and draw.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        paths: The run paths.
        inputs: The run inputs.
        mode: The planner mode.
        labels: Fixture labels to run. Only the decline fixtures among them run. Default is all.
        digest: Optional digest text, sent as the production memory_recall tool result.
        draws: Draws per fixture.
    """
    decline = sorted(k for k, v in inputs.captured.items() if v.get("expected") == "decline")
    wanted = [x for x in decline if labels is None or x in labels]
    seen = done_pair_keys(paths.paired, "label")
    for draw in range(draws):
        for label in wanted:
            if (label, draw) in seen:
                continue
            row: dict[str, object] = {
                "arm": "paired",
                "tag": paths.tag,
                "ts": utc_now(),
                "label": label,
                "draw": draw,
                "kind": _kind(inputs, label),
                "history_chars": inputs.fixture(label)["history_chars"],
            }
            single_first = (decline.index(label) + draw) % 2 == 0

            def build(nonce: str, label: str = label) -> PairBodies:
                return fixture_pair_bodies(inputs, label, mode, digest, nonce)

            try:
                row.update(run_pair(client, url, model, build, single_first))
                fp_mod.fill_engine_build(
                    paths,
                    row["planner"]["planner"].get("system_fingerprint"),  # type: ignore[index]
                )
            except (httpx.HTTPError, ValueError) as exc:
                row["error"] = repr(exc)[:300]
            append_jsonl(paths.paired, row)
            print(
                "paired",
                draw,
                label,
                row.get("added_ms"),
                row.get("declined"),
                row.get("void"),
                str(row.get("error", ""))[:80],
                flush=True,
            )


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
    """Run the decision arm, the paired arm, or both.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common_arguments(parser)
    parser.add_argument("--arms", default=None, help="default: decide,paired (managed: decide)")
    parser.add_argument("--labels", default="")
    parser.add_argument("--trials", type=int, default=3)
    args = parser.parse_args(argv)
    arms = (args.arms or ("decide" if args.deployment else ",".join(ARMS))).split(",")
    for arm in arms:
        if arm == "timing":
            raise SystemExit(
                "--arms timing is replaced by the paired arm (ADR-0154 Amendment 1): "
                "use --arms decide,paired"
            )
        if arm not in ARMS:
            raise SystemExit(f"unknown arm {arm!r}: use decide or paired")
    if args.deployment:
        if arms != ["decide"]:
            raise SystemExit(
                "--arms paired runs on llama.cpp only, and the managed paired path does not exist "
                "yet (ADR-0154 Amendment 1, 1.2): a managed run uses --arms decide"
            )
        return _main_cloud(args)
    with httpx.Client(timeout=900.0) as client:
        paths, inputs, mode, digest = prepare(args, client)
        labels = args.labels.split(",") if args.labels else list(inputs.captured)
        for arm in arms:
            if arm == "decide":
                run_decide(
                    client, args.url, args.model, paths, inputs, mode, digest, labels, args.trials
                )
            else:
                run_paired(client, args.url, args.model, paths, inputs, mode, labels, digest)
    rows = len(read_jsonl(paths.decide)) + len(read_jsonl(paths.paired))
    print(f"rows in {paths.rows}: {rows}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
