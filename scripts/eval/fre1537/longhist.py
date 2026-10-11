"""FRE-1511 / ADR-0154 D7 and Appendix A1: the long-history arms.

Design A in a chosen planner mode, with synthetic histories built from the four fixture conversations at
8,000, 30,000 and 60,000 characters. Timing only.

    cold    per size, a cold planner call, then a primary call, then the next turn's planner call whose
            history extends the first by one exchange                          -> rows/<tag>/longhist.jsonl
    paired  per size and draw, the turn routed SINGLE and routed to a declining
            planner (ADR-0154 Amendment 1)                                     -> rows/<tag>/longhist_paired.jsonl

    uv run python -m scripts.eval.fre1537.longhist --run-dir <run> --mode thinking_off
"""

from __future__ import annotations

import argparse
import secrets
import sys
from collections.abc import Mapping, Sequence

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537 import fingerprint as fp_mod
from scripts.eval.fre1537 import render
from scripts.eval.fre1537.common import (
    LONGHIST_SIZES,
    PAIRED_DRAWS,
    RunPaths,
    append_jsonl,
    done_pair_keys,
    read_jsonl,
    utc_now,
)
from scripts.eval.fre1537.fixtures import load_histories
from scripts.eval.fre1537.llama import (
    Inputs,
    PlannerMode,
    parse_plan,
    planner_request,
    primary_body,
    primary_from,
    prime_from,
    reference_label,
    stream,
)
from scripts.eval.fre1537.replay import (
    PairBodies,
    common_arguments,
    prepare,
    prepare_cloud,
    run_pair,
    with_nonce,
)

SIZES = LONGHIST_SIZES
ARMS = ("cold", "paired")
FIRST_QUERY = "Thanks. Which of the three did you say is cheapest to install?"
FIRST_REPLY = "Air-source is the cheapest to install."
SECOND_QUERY = "So Tempo is the one with red days?"
MAX_TOKENS = 4096


OPENER_REPLY = "Hello. How can I help?"


def history_pairs(n_chars: int, nonce: str) -> list[tuple[str, str]]:
    """Build the (user, assistant) exchanges of a history of at least ``n_chars`` rendered characters.

    The one source of a long history: :func:`render_history` makes the planner's text from it, and the
    paired arm makes the primary's role messages from it, so the two views cannot differ.

    Args:
        n_chars: Minimum length, counted over the rendered lines without their newlines.
        nonce: A per-run token in the first exchange, so that no size reuses the engine's cached prefix.

    Returns:
        The exchanges, oldest first. The first opens the session and carries the nonce.
    """
    sources = list(load_histories().values())
    pairs = [(f"(session {nonce}) Hello.", OPENER_REPLY)]

    def rendered() -> int:
        return sum(len(f"user: {u}") + len(f"assistant: {a}") for u, a in pairs)

    i = 0
    while rendered() < n_chars:
        user, assistant = sources[i % len(sources)]
        pairs.append((f"{user} (part {i})", assistant))
        i += 1
    return pairs


def render_pairs(pairs: Sequence[tuple[str, str]]) -> str:
    """Render exchanges as the role-labelled history text the planner reads."""
    return "\n".join(f"user: {u}\nassistant: {a}" for u, a in pairs)


def render_history(n_chars: int, nonce: str) -> str:
    """Build a role-labelled history of at least ``n_chars`` characters from the fixture conversations.

    Args:
        n_chars: Minimum length.
        nonce: A per-run token in the first line, so that no size reuses the engine's cached prefix.

    Returns:
        The history text.
    """
    return render_pairs(history_pairs(n_chars, nonce))


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
    pending: float = 0.0,
    digest: str | None = None,
) -> dict[str, object]:
    user = render.build_user_message(history, None, query)
    if cloud is not None:
        estimate = (
            budget.check(cloud.target, inputs.system, f"{user}\n{digest or ''}", pending=pending)
            if budget
            else 0.0
        )
        try:
            result = cloud.call(inputs.system, user, digest)
        except cloud_mod.CloudCallError as exc:
            raise cloud_mod.CloudCallError(str(exc), estimate) from exc
    else:
        body = planner_request(
            inputs.system, user, inputs.body(reference_label(inputs)), mode, MAX_TOKENS, digest
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
    digest: str | None = None,
) -> None:
    """Run the long-history arm, one row per size. A size that already has a complete row is skipped.

    A managed call that fails leaves an ``error`` row that holds what the size had already cost, so the
    spending cap sees it. A rerun does the size again.

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
        digest: Optional memory digest lines. Both planner calls carry them the way the
            decision arm does: the production ``memory_recall`` tool result after the user
            message (FRE-1360).
    """
    seen = {int(str(r["size_chars"])) for r in read_jsonl(paths.longhist) if "error" not in r}
    for size in sizes:
        if size in seen:
            continue
        first = render_history(size, secrets.token_hex(4))
        row: dict[str, object] = {"arm": "longhist", "tag": paths.tag, "size_chars": size}
        try:
            cold = _call(
                client, url, model, inputs, mode, first, FIRST_QUERY, cloud, budget, digest=digest
            )
        except cloud_mod.CloudCallError as exc:
            append_jsonl(
                paths.longhist,
                {**row, "error": repr(exc)[:300], "cost_usd": exc.input_estimate_usd},
            )
            raise
        row["cold"] = cold
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
        try:
            row["extended"] = _call(
                client,
                url,
                model,
                inputs,
                mode,
                extended_history,
                SECOND_QUERY,
                cloud,
                budget,
                pending=float(cold.get("cost_usd") or 0),  # type: ignore[arg-type]
                digest=digest,
            )
        except (cloud_mod.CloudCallError, SystemExit) as exc:
            if cloud is not None:  # keep the cold call's cost on disk
                estimate = getattr(exc, "input_estimate_usd", 0.0)
                append_jsonl(
                    paths.longhist, {**row, "error": repr(exc)[:300], "cost_usd": estimate}
                )
            raise
        append_jsonl(paths.longhist, row)
        print("longhist", size, cold["secs"], row["extended"]["secs"], flush=True)  # type: ignore[index]


def long_turn_bodies(
    inputs: Inputs, mode: PlannerMode, size: int, nonce: str, digest: str | None = None
) -> tuple[PairBodies, int]:
    """Build the four requests of one long-history pair, for one arm's nonce (Amendment 1, 1.6).

    The turn is the history H of the size, then ``FIRST_QUERY`` and ``FIRST_REPLY`` as one
    exchange, then ``SECOND_QUERY``. The previous turn is H with ``FIRST_QUERY``. The primary
    request starts from the reference fixture's captured body: its system message, tools and
    sampling stay, the messages between the system message and the last user message become the
    turn's history, the last user message becomes ``SECOND_QUERY``, and the messages after it stay.

    Args:
        inputs: The run inputs.
        mode: The planner mode.
        size: The history size in characters.
        nonce: The arm's nonce. It starts every system message and the history's first exchange.
        digest: Optional digest text, carried by the measured planner call only.

    Returns:
        The requests, and the length of the turn's rendered planner history.
    """
    h = history_pairs(size, nonce)
    turn = [*h, (FIRST_QUERY, FIRST_REPLY)]
    history = render_pairs(turn)
    src = inputs.body(reference_label(inputs))
    messages: list[Mapping[str, object]] = list(src["messages"])  # type: ignore[call-overload]
    last_user = max(i for i, m in enumerate(messages) if m.get("role") == "user")
    roles = [
        msg
        for u, a in turn
        for msg in ({"role": "user", "content": u}, {"role": "assistant", "content": a})
    ]
    body = {
        **src,
        "messages": [
            messages[0],
            *roles,
            {"role": "user", "content": SECOND_QUERY},
            *messages[last_user + 1 :],
        ],
    }

    def planner(
        text: str, query: str, max_tokens: int, with_digest: str | None
    ) -> dict[str, object]:
        user = render.build_user_message(text, None, query)
        return planner_request(inputs.system, user, src, mode, max_tokens, with_digest)

    bodies = PairBodies(
        prime_primary=with_nonce(prime_from(body), nonce),
        primary=with_nonce(primary_from(body, max_tokens=1), nonce),
        prime_planner=with_nonce(planner(render_pairs(h), FIRST_QUERY, 1, None), nonce),
        planner=with_nonce(planner(history, SECOND_QUERY, MAX_TOKENS, digest), nonce),
    )
    return bodies, len(history)


def run_longhist_paired(
    client: httpx.Client,
    url: str,
    model: str,
    paths: RunPaths,
    inputs: Inputs,
    mode: PlannerMode,
    sizes: Sequence[int] = SIZES,
    draws: int = PAIRED_DRAWS,
    digest: str | None = None,
) -> None:
    """Run the long-history paired arm: each size for ``draws`` draws (ADR-0154 Amendment 1, 1.6).

    A pair runs the ``SINGLE`` arm first when (index + draw) is even, where the index is the size's
    position in ``sizes``. A rerun resumes on the size and draw.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        model: The served model name.
        paths: The run paths.
        inputs: The run inputs.
        mode: The planner mode.
        sizes: History sizes in characters.
        draws: Draws per size.
        digest: Optional digest text, carried by the measured planner call only.
    """
    seen = done_pair_keys(paths.longhist_paired, "size_chars")
    for draw in range(draws):
        for index, size in enumerate(sizes):
            if (str(size), draw) in seen:
                continue
            lengths: list[int] = []

            def build(nonce: str, size: int = size, lengths: list[int] = lengths) -> PairBodies:
                bodies, history_chars = long_turn_bodies(inputs, mode, size, nonce, digest)
                lengths.append(history_chars)
                return bodies

            row: dict[str, object] = {
                "arm": "longhist_paired",
                "tag": paths.tag,
                "ts": utc_now(),
                "size_chars": size,
                "draw": draw,
                "kind": "longhist",
            }
            try:
                row.update(run_pair(client, url, model, build, (index + draw) % 2 == 0))
                fp_mod.fill_engine_build(
                    paths,
                    row["planner"]["planner"].get("system_fingerprint"),  # type: ignore[index]
                )
            except (httpx.HTTPError, ValueError) as exc:
                row["error"] = repr(exc)[:300]
            # The planner arm's history: both arms render the same length, the nonces differ only.
            row["history_chars"] = lengths[-1] if lengths else None
            append_jsonl(paths.longhist_paired, row)
            print(
                "longhist_paired",
                draw,
                size,
                row.get("added_ms"),
                row.get("declined"),
                row.get("void"),
                str(row.get("error", ""))[:80],
                flush=True,
            )


def main(argv: Sequence[str] | None = None) -> int:
    """Run the long-history arm.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    common_arguments(parser)
    parser.add_argument("--arms", default=None, help="default: cold,paired (managed: cold)")
    args = parser.parse_args(argv)
    arms = (args.arms or ("cold" if args.deployment else ",".join(ARMS))).split(",")
    unknown = [a for a in arms if a not in ARMS]
    if unknown:
        raise SystemExit(f"unknown arm {unknown[0]!r}: use cold or paired")
    if args.deployment:
        if "paired" in arms:
            raise SystemExit(
                "--arms paired runs on llama.cpp only, and the managed paired path does not exist "
                "yet (ADR-0154 Amendment 1, 1.2): a managed run uses --arms cold"
            )
        paths, inputs, target, digest, budget = prepare_cloud(args)
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
                digest=digest,
            )
        print(f"spent in {args.run_dir}: {budget.spent():.4f} USD of {args.max_usd:g}")
        print(f"rows in {paths.longhist}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
        return 0
    with httpx.Client(timeout=900.0) as client:
        paths, inputs, mode, digest = prepare(args, client)
        if "cold" in arms:
            run_longhist(client, args.url, args.model, paths, inputs, mode, digest=digest)
        if "paired" in arms:
            run_longhist_paired(client, args.url, args.model, paths, inputs, mode, digest=digest)
    print(f"rows in {paths.rows}. Next: score --run-dir {args.run_dir} --tag {paths.tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
