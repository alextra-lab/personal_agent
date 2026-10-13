"""FRE-1597 / ADR-0156 A5 (T-D1): the full tool loop (AC-2) and the cache cost (AC-4) on the final build.

The FRE-1594 session (``fre1594.session.run_draw``) in the simple form, over the four FRE-1594
prompts (2 ``log-search``, 2 ``model-latency``). The final build replaces the spike's synthetic
pieces with captured production ones (``calls.py``): the ``read_skill`` result that the gateway
sent, and call 2's ``tools`` with the revealed tool where the registry puts it. The control keeps
call 1's tools and the same ``read_skill`` result, so it differs only by the reveal (AC-F3).

    capture  one probe-gateway turn per prompt, the stub scripted to load the prompt's skill
                                                    -> <run>/loop/captured/<prompt id>.json
    run      one provider: 4 prompts x 3 draws, each a treatment and its control
                                                    -> <run>/loop/rows-<provider>.jsonl
    score    AC-2 and AC-4 (local), the cloud cache fields   -> <run>/loop/report.json

Call 3 carries the shim's tool result. Production adds no ``_citation`` field to a ``search_logs``
or ``measure_model_latency`` result (``grounding/source_registry.py:328``), so call 3 is the
production shape.

Run from the repo root. See README.md beside this file.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537.common import append_jsonl, read_jsonl, utc_now
from scripts.eval.fre1590.triggering import safe_name
from scripts.eval.fre1594 import reveal, score, session, shim, spike, transport
from scripts.eval.fre1597 import common
from scripts.eval.fre1597.calls import FidelityError, JsonDict, TwoCalls

from personal_agent.config.settings import get_settings
from personal_agent.orchestrator.skills import providers_of
from personal_agent.telemetry.trace import SystemTraceContext

PROVIDERS = ("local", "cloud")
LOOP_FLOOR = 10  # AC-F2: at least 10 of 12 draws on the production primary
POPULATION = 12  # 4 prompts x 3 draws


def _root(run_dir: Path) -> Path:
    return run_dir / "loop"


def captured_file(run_dir: Path, prompt_id: str) -> Path:
    """Return the capture file of one prompt."""
    return _root(run_dir) / "captured" / f"{safe_name(prompt_id)}.json"


def rows_file(run_dir: Path, provider: str) -> Path:
    """Return the rows file of one provider."""
    return _root(run_dir) / f"rows-{provider}.jsonl"


def make_draw(prompt: reveal.Prompt, two: TwoCalls, *, number: int) -> session.Draw:
    """Build one simple-form draw of a prompt from its captured calls.

    Args:
        prompt: The FRE-1594 prompt.
        two: The prompt's captured calls on the final build.
        number: The draw number.

    Returns:
        The draw: call 1 is the captured first request; the treatment's call 2 and call 3 carry
        the captured call-2 tools; both arms carry the captured ``read_skill`` result.

    Raises:
        FidelityError: The capture did not reveal the prompt's tool.
    """
    scenarios = reveal.load_scenarios()
    tool = scenarios.skills[prompt.skill].tool
    if two.second is None or two.skill_message is None or two.added_tools != (tool,):
        raise FidelityError(f"{prompt.id}: the capture reveals {list(two.added_tools)}, not {tool}")
    second_tools = two.second["tools"]
    assert isinstance(second_tools, list)
    definitions = {
        reveal.tool_name(t): t for t in second_tools if reveal.tool_name(t) in two.added_tools
    }

    def place(base: Sequence[JsonDict], revealed: tuple[str, ...]) -> list[JsonDict]:
        if revealed != (tool,):
            raise FidelityError(f"{prompt.id}: revealed {revealed}, the capture holds {tool}")
        return list(second_tools)

    return session.Draw(
        prompt=prompt,
        form="simple",
        draw=number,
        base=two.first,
        scenarios=scenarios,
        tools=definitions,
        skill_content=str(two.skill_message["content"]),
        place_tools=place,
    )


def verdicts(rows: Sequence[Mapping[str, object]]) -> dict[str, object]:
    """Decide AC-2 and AC-4 over the local rows.

    Args:
        rows: The rows of the local provider.

    Returns:
        ``ac2``: the loop count and ``verdict`` (``incomplete`` below 12 draws, else ``pass`` at 10
        or more held). ``ac4``: the FRE-1594 cache verdict of the simple form, made ``fail`` by any
        lineage-void pair or missing timings.
    """
    counted = score.loop_count(rows)
    held, draws = int(str(counted["held"])), int(str(counted["draws"]))
    if draws < POPULATION:
        ac2 = "incomplete"
    else:
        ac2 = "pass" if held >= LOOP_FLOOR else "fail"
    cache = score.cache_verdict(rows, "simple")
    pairs = cache["pairs"]
    assert isinstance(pairs, list)
    reasons = list(cache["reasons"])  # type: ignore[call-overload]
    if any(p.get("void") == "lineage" for p in pairs):
        reasons.append("lineage")
    verdict = "fail" if reasons else cache["verdict"]
    return {
        "ac2": {**counted, "floor": LOOP_FLOOR, "verdict": ac2},
        "ac4": {**cache, "reasons": reasons, "verdict": verdict},
    }


# --------------------------------------------------------------------------- CLI


def _prompts(limit: int) -> list[reveal.Prompt]:
    prompts = list(reveal.load_scenarios().prompts)
    return prompts[:limit] if limit else prompts


def _cmd_capture(args: argparse.Namespace) -> int:
    failed = 0
    with httpx.Client() as client:
        build = common.gateway_build(args.chat_url)
        for prompt in _prompts(0):
            target = captured_file(args.run_dir, prompt.id)
            if target.exists():
                continue
            try:
                two = common.capture_turn(
                    client,
                    args.chat_url,
                    args.run_dir,
                    prompt.text,
                    model_key=args.model,
                    skill=prompt.skill,
                )
            except (httpx.HTTPError, FidelityError, ValueError) as exc:
                failed += 1
                print(prompt.id, "capture failed:", repr(exc)[:300], file=sys.stderr, flush=True)
                continue
            meta = {"id": prompt.id, "prompt": prompt.text, "gateway_build": build}
            common.save_capture(target, two, {**meta, "captured_at": utc_now()})
            print(prompt.id, "captured", list(two.added_tools), flush=True)
    return 1 if failed else 0


def _cmd_run(args: argparse.Namespace) -> int:
    prompts = _prompts(args.limit)
    captures = {p.id: common.load_capture(captured_file(args.run_dir, p.id)) for p in prompts}
    settings = get_settings()
    shim.assert_eval_elasticsearch(settings.elasticsearch_url)
    providers = {tool: providers_of(tool) for tool in ("search_logs", "measure_model_latency")}
    event_loop = asyncio.new_event_loop()
    ctx = SystemTraceContext.new("fre1597_loop")

    def run_tool(name: str, arguments: str, revealed: tuple[str, ...]) -> shim.ToolRun:
        return event_loop.run_until_complete(
            shim.run_tool(name, arguments, revealed=revealed, providers=providers, ctx=ctx)
        )

    client = httpx.Client(timeout=httpx.Timeout(900.0, connect=10.0))
    cloud: transport.CloudTransport | None = None
    try:
        sender: transport.Transport
        if args.provider == "local":
            pins = {"provider": "local", **common.local_pins(args, client)}
            sender = common.RetryOnce(transport.LlamaTransport(client, args.url, args.llama_model))
        else:
            target = cloud_mod.resolve_target(args.deployment, "default")
            pins = {
                "provider": "cloud",
                "deployment": target.key,
                "model_id": target.model_id,
                "endpoint": target.endpoint,
            }
            cloud = transport.CloudTransport(
                target,
                ledger=_root(args.run_dir) / "ledger-cloud.jsonl",
                max_usd=args.max_usd,
            )
            sender = cloud
        records = [meta for meta, _ in captures.values()]
        fingerprint = {
            **pins,
            **common.request_pins([two.first for _, two in captures.values()]),
            "prompts": {p.id: common.sha256(p.text.encode()) for p in prompts},
            "skill_results": {
                p.id: common.sha256(str((two.skill_message or {}).get("content")).encode())
                for p, (_, two) in zip(prompts, captures.values(), strict=True)
            },
            "gateway_builds": sorted({str(r.get("gateway_build")) for r in records}),
            "draws": args.draws,
            "warmup": common.WARMUP,
            "git_head": spike._git_head(),
        }
        spike.ensure_fingerprint(
            _root(args.run_dir) / f"fingerprint-{args.provider}.json", fingerprint
        )
        if args.provider == "local":
            first = captures[prompts[0].id][1].first
            common.warm_up(sender, first, _root(args.run_dir) / "warmup.jsonl")

        path = rows_file(args.run_dir, args.provider)
        done = {(str(r["prompt_id"]), int(str(r["draw"]))) for r in read_jsonl(path)}
        for prompt in prompts:
            two = captures[prompt.id][1]
            for number in range(args.draws):
                if (prompt.id, number) in done:
                    continue
                draw = make_draw(prompt, two, number=number)
                nonces = (secrets.token_hex(8), secrets.token_hex(8))
                row = session.run_draw(sender, draw, run_tool, nonces=nonces)
                append_jsonl(path, {"at": utc_now(), "provider": args.provider, **row})
                print(prompt.id, number, "steps", row["steps"], flush=True)
    finally:
        client.close()
        if cloud is not None:
            cloud.close()
        event_loop.close()
    return 0


def _cmd_score(args: argparse.Namespace) -> int:
    report: dict[str, object] = {}
    local = read_jsonl(rows_file(args.run_dir, "local"))
    if local:
        report["local"] = verdicts(local)
    cloud = read_jsonl(rows_file(args.run_dir, "cloud"))
    if cloud:
        report["cloud"] = {"loop": score.loop_count(cloud), "cache": score.cloud_cache(cloud)}
    (_root(args.run_dir) / "report.json").write_text(json.dumps(report, indent=2))
    for provider, entry in report.items():
        assert isinstance(entry, Mapping)
        if provider == "local":
            ac2, ac4 = entry["ac2"], entry["ac4"]
            assert isinstance(ac2, Mapping) and isinstance(ac4, Mapping)
            print(f"local AC-2 {ac2['verdict']}: {ac2['held']} of {ac2['draws']} held")
            print(f"local AC-4 {ac4['verdict']}: reasons {ac4['reasons']}")
        else:
            print(f"cloud loop (no bound): {entry['loop']}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step: capture, run or score."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "run", "score"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        if name == "capture":
            p.add_argument("--chat-url", default=capture.DEFAULT_CHAT_URL)
            p.add_argument("--model", default=common.DEFAULT_MODEL_KEY, help="the catalog key")
        if name == "run":
            p.add_argument("--provider", choices=PROVIDERS, required=True)
            p.add_argument("--draws", type=int, default=common.DEFAULT_DRAWS)
            p.add_argument("--limit", type=int, default=0, help="first N prompts only (a dry run)")
            p.add_argument("--llama-model", help="the llama.cpp model name (local)")
            p.add_argument("--quant", help="the quant of the loaded model (local)")
            p.add_argument("--server-args", help="the llama-server launch arguments (local)")
            p.add_argument("--props-file", type=Path, help="a saved llama-server /props (local)")
            p.add_argument("--url", default=common.DEFAULT_URL)
            p.add_argument("--deployment", default=spike.DEFAULT_DEPLOYMENT, help="(cloud)")
            p.add_argument("--max-usd", type=float, default=spike.DEFAULT_MAX_USD, help="(cloud)")
    args = parser.parse_args(argv)
    if args.command == "run" and args.provider == "local":
        if not (args.llama_model and args.quant and args.server_args):
            parser.error("--llama-model, --quant and --server-args are required for local")
    handlers = {"capture": _cmd_capture, "run": _cmd_run, "score": _cmd_score}
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
