"""FRE-1594 / ADR-0156 A5 (T-B): the reveal spike.

Does a tool revealed after ``read_skill`` complete the full tool loop, and what does each reveal
form cost in cache?

    preflight  no model call: builds every first request, projects time and cost, reads the
               llama.cpp ``/props``, and runs one call of each tool through the shim
    run        one provider and one form: 4 prompts x 3 draws, each a treatment and its control
    score      AC-1, AC-2 and AC-3 over every row of the run directory

Run from the repo root. See ``scripts/eval/fre1594/README.md``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import cloud as cloud_mod
from scripts.eval.fre1537.common import append_jsonl, read_jsonl, utc_now
from scripts.eval.fre1594 import reveal, score, session, shim, transport

from personal_agent.config.settings import get_settings
from personal_agent.llm_client.token_counter import estimate_tokens
from personal_agent.telemetry.trace import SystemTraceContext

DEFAULT_URL = "http://127.0.0.1:8600/v1/chat/completions"
DEFAULT_DEPLOYMENT = "claude_sonnet"
DEFAULT_DRAWS = 3
DEFAULT_MAX_USD = 10.0
PROVIDERS = ("local", "cloud")
LOOP_FLOOR = 10  # AC-F2: at least 10 of 12 draws on the production primary
POPULATION = 12  # 4 prompts x 3 draws
WARMUP = (
    "one request before the first draw of a run invocation: the base request of the first prompt, "
    "its own nonce, max_tokens 1; its row is kept in warmup.jsonl"
)
# Projection assumptions (FRE-1594 preflight). Measured 2026-08-28 (config/models.yaml):
# Flash-Next prefill 285 t/s, decode 21.1 t/s. The output per call is an assumption that the dry
# draw replaces.
PREFILL_TPS = 285.0
DECODE_TPS = 21.1
OUTPUT_PER_CALL = 600
CALLS_PER_SESSION = 3


# --------------------------------------------------------------------------- paths and inputs


def rows_path(run_dir: Path, provider: str, form: str) -> Path:
    """Return the rows file of one provider and form."""
    return run_dir / "rows" / provider / f"{form}.jsonl"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_base(path: Path) -> dict[str, object]:
    """Load a captured primary request: a capture record with ``body``, or a bare body."""
    data = json.loads(path.read_text())
    body = data.get("body", data)
    if not isinstance(body, dict) or "messages" not in body or "tools" not in body:
        raise SystemExit(f"{path} holds no captured primary request body")
    return body


def _git_head() -> str:
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True, check=False)  # noqa: S607
    return out.stdout.strip() or "unknown"


def _find_cache_reuse(node: object, path: str = "") -> dict[str, object]:
    found: dict[str, object] = {}
    if isinstance(node, Mapping):
        for key, value in node.items():
            here = f"{path}.{key}" if path else str(key)
            if "cache_reuse" in str(key):
                found[here] = value
            found.update(_find_cache_reuse(value, here))
    return found


def read_props(client: httpx.Client, url: str, props_file: Path | None = None) -> dict[str, object]:
    """Read the llama.cpp server's ``/props``: the chat-template hash and the cache settings.

    The slm_server router on ``:8600`` refuses ``/props`` (403). The saved ``/props`` of the
    llama-server behind it can be passed as a file instead.

    Args:
        client: The HTTP client.
        url: The chat-completions URL. ``/props`` is on the same host.
        props_file: A saved ``/props`` reply. When given, no request is sent.

    Returns:
        ``source``, ``chat_template_sha256``, ``build_info``, ``model_path``, ``total_slots`` and
        every ``*cache_reuse*`` key, or ``error``.
    """
    props_url = url.split("/v1/")[0] + "/props"
    try:
        if props_file is not None:
            props = json.loads(props_file.read_text())
            source = str(props_file)
        else:
            response = client.get(props_url)
            response.raise_for_status()
            props = response.json()
            source = props_url
    except (httpx.HTTPError, json.JSONDecodeError, OSError) as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:300]}
    template = str(props.get("chat_template") or "")
    return {
        "source": source,
        "chat_template_sha256": _sha(template.encode()) if template else None,
        "build_info": props.get("build_info"),
        "model_path": props.get("model_path"),
        "total_slots": props.get("total_slots"),
        "cache_reuse": _find_cache_reuse(props),
    }


def build_fingerprint(
    base_path: Path,
    base: Mapping[str, object],
    scenarios: reveal.Scenarios,
    draws: int,
    provider: dict[str, object],
) -> dict[str, object]:
    """Build the pinning record of ADR-0156 A5 (AC-F pinning list).

    Args:
        base_path: The captured base file.
        base: The captured base body.
        scenarios: The scenarios.
        draws: Draws per prompt.
        provider: The provider's own pins (model, quant, build, chat template, or deployment).

    Returns:
        The fingerprint.
    """
    messages = base["messages"]
    tools = base["tools"]
    assert isinstance(messages, list) and isinstance(tools, list)
    return {
        "base_file": str(base_path),
        "base_sha256": _sha(reveal.request_bytes(base)),
        "system_prompt_sha256": _sha(str(messages[0]["content"]).encode()),
        "tools_sha256": _sha(reveal.request_bytes({"tools": tools})),
        "tool_names": [reveal.tool_name(t) for t in tools],
        "sampling": {k: v for k, v in base.items() if k not in ("messages", "tools")},
        "scenarios_sha256": _sha(reveal.SCENARIOS.read_bytes()),
        "prompts": {p.id: _sha(p.text.encode()) for p in scenarios.prompts},
        "skill_bodies": {s.name: _sha(s.body.encode()) for s in scenarios.skills.values()},
        "experimental_tools_sha256": _sha(reveal.request_bytes(reveal.experimental_tools())),
        "draws": draws,
        "warmup": WARMUP,
        "git_head": _git_head(),
        **provider,
    }


def ensure_fingerprint(path: Path, fingerprint: Mapping[str, object]) -> None:
    """Write the fingerprint, or refuse a run whose configuration differs from the recorded one.

    Every key is pinned, the harness commit included. A dry draw uses its own run directory.

    Args:
        path: The fingerprint file.
        fingerprint: This invocation's fingerprint.

    Raises:
        SystemExit: If a pinned value differs.
    """
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(fingerprint, indent=2))
        return
    recorded = json.loads(path.read_text())
    changed = sorted(
        k for k in set(recorded) | set(fingerprint) if recorded.get(k) != fingerprint.get(k)
    )
    if changed:
        raise SystemExit(f"refusing to mix configurations in {path.parent}: {changed} changed")


# --------------------------------------------------------------------------- preflight


def preflight(args: argparse.Namespace) -> int:
    """Build every first request, project the run, read ``/props`` and smoke-test the shim."""
    scenarios = reveal.load_scenarios()
    base = load_base(args.base)
    bodies = {p.id: reveal.base_body(base, scenarios, p) for p in scenarios.prompts}
    tokens = {pid: estimate_tokens(json.dumps(b)) for pid, b in bodies.items()}
    prompt_tokens = max(tokens.values())
    sessions = len(scenarios.prompts) * args.draws * 2  # treatment and control
    calls = sessions * CALLS_PER_SESSION
    local_secs = sessions * (
        prompt_tokens / PREFILL_TPS + CALLS_PER_SESSION * OUTPUT_PER_CALL / DECODE_TPS
    )
    target = cloud_mod.resolve_target(args.deployment, "default")
    cloud_worst = calls * (prompt_tokens * target.input_cost + OUTPUT_PER_CALL * target.output_cost)
    with httpx.Client(timeout=10.0) as client:
        props = read_props(client, args.url, args.props_file)

    settings = get_settings()
    smoke: dict[str, object] = {"elasticsearch_url": settings.elasticsearch_url}
    try:
        shim.assert_eval_elasticsearch(settings.elasticsearch_url)
    except SystemExit as exc:
        smoke["error"] = str(exc)
    else:
        ctx = SystemTraceContext.new("fre1594_reveal_spike_preflight")
        providers = _providers(scenarios)
        for name, arguments in (
            ("search_logs", '{"action": "count", "index": "agent-logs", "since": "24h"}'),
            ("measure_model_latency", '{"since": "1h"}'),
        ):
            run = asyncio.run(
                shim.run_tool(name, arguments, revealed=(name,), providers=providers, ctx=ctx)
            )
            smoke[name] = {"ran": run.ran, "code": run.code, "content": run.content[:300]}

    report = {
        "at": utc_now(),
        "base_sha256": _sha(reveal.request_bytes(base)),
        "first_request_tokens": tokens,
        "population": {
            "prompts": len(scenarios.prompts),
            "draws": args.draws,
            "forms": list(reveal.FORMS),
            "sessions_per_form_and_provider": sessions,
            "calls_per_form_and_provider_at_most": calls,
        },
        "local_projection": {
            "assumptions": {
                "prefill_tps": PREFILL_TPS,
                "decode_tps": DECODE_TPS,
                "output_tokens_per_call": OUTPUT_PER_CALL,
            },
            "hours_per_form": round(local_secs / 3600, 2),
            "hours_both_forms": round(2 * local_secs / 3600, 2),
        },
        "cloud_projection": {
            "deployment": args.deployment,
            "model_id": target.model_id,
            "worst_case_usd_per_form_no_cache": round(cloud_worst, 2),
            "worst_case_usd_both_forms_no_cache": round(2 * cloud_worst, 2),
            "cap_usd": args.max_usd,
        },
        "llama_props": props,
        "shim_smoke": smoke,
    }
    out = args.run_dir / "preflight.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2))
    print(json.dumps(report, indent=2))
    return 0


# --------------------------------------------------------------------------- run


def _providers(scenarios: reveal.Scenarios) -> dict[str, tuple[str, ...]]:
    out: dict[str, list[str]] = {}
    for skill in scenarios.skills.values():
        out.setdefault(skill.tool, []).append(skill.name)
    return {tool: tuple(names) for tool, names in out.items()}


def _done(path: Path) -> set[tuple[str, int]]:
    return {(str(r["prompt_id"]), int(str(r["draw"]))) for r in read_jsonl(path)}


def run(args: argparse.Namespace) -> int:
    """Run one provider and one form over the population, resuming."""
    scenarios = reveal.load_scenarios()
    base = load_base(args.base)
    tools = reveal.experimental_tools()
    prompts = scenarios.prompts[: args.limit] if args.limit else scenarios.prompts
    settings = get_settings()
    shim.assert_eval_elasticsearch(settings.elasticsearch_url)
    providers = _providers(scenarios)
    loop = asyncio.new_event_loop()
    ctx = SystemTraceContext.new("fre1594_reveal_spike")

    def run_tool(name: str, arguments: str, revealed: tuple[str, ...]) -> shim.ToolRun:
        return loop.run_until_complete(
            shim.run_tool(name, arguments, revealed=revealed, providers=providers, ctx=ctx)
        )

    path = rows_path(args.run_dir, args.provider, args.form)
    client = httpx.Client(timeout=httpx.Timeout(900.0, connect=10.0))
    cloud: transport.CloudTransport | None = None
    try:
        sender: transport.Transport
        if args.provider == "local":
            if not args.llama_model or not args.quant:
                raise SystemExit("--llama-model and --quant are required for the local provider")
            props = read_props(client, args.url, args.props_file)
            if "error" in props:
                raise SystemExit(
                    f"no llama.cpp /props ({props['error']}): pass --props-file, so the run pins "
                    "the chat template and shows any chunk reuse"
                )
            pins: dict[str, object] = {
                "provider": "local",
                "llama_model": args.llama_model,
                "quant": args.quant,
                "llama_url": args.url,
                "llama_props": props,
            }
            sender = transport.LlamaTransport(client, args.url, args.llama_model)
        else:
            target = cloud_mod.resolve_target(args.deployment, "default")
            pins = {
                "provider": "cloud",
                "deployment": target.key,
                "model_id": target.model_id,
                "endpoint": target.endpoint,
                "mode": target.declared,
            }
            # The cap is a safety bound, not a pinned input: an owner OK can raise it on resume.
            cloud = transport.CloudTransport(
                target,
                ledger=args.run_dir / "rows" / "cloud" / "ledger.jsonl",
                max_usd=args.max_usd,
            )
            sender = cloud
        fingerprint = build_fingerprint(args.base, base, scenarios, args.draws, pins)
        ensure_fingerprint(args.run_dir / f"fingerprint-{args.provider}.json", fingerprint)

        if args.provider == "local":
            warm = reveal.with_nonce(
                {**reveal.base_body(base, scenarios, prompts[0]), "max_tokens": 1},
                f"warmup-{secrets.token_hex(6)}",
            )
            reply = sender.send(warm)
            append_jsonl(
                args.run_dir / "rows" / "local" / "warmup.jsonl",
                {"at": utc_now(), "form": args.form, **reply.record()},
            )

        done = _done(path)
        for prompt in prompts:
            body = reveal.base_body(base, scenarios, prompt)
            for number in range(args.draws):
                if (prompt.id, number) in done:
                    continue
                draw = session.Draw(prompt, args.form, number, body, scenarios, tools)
                nonces = (secrets.token_hex(8), secrets.token_hex(8))
                row = session.run_draw(sender, draw, run_tool, nonces=nonces)
                append_jsonl(path, {"at": utc_now(), "provider": args.provider, **row})
                print(prompt.id, number, "steps", row["steps"], flush=True)
    finally:
        client.close()
        if cloud is not None:
            cloud.close()
        loop.close()
    return 0


# --------------------------------------------------------------------------- score


def build_report(run_dir: Path) -> dict[str, object]:
    """Score every rows file of a run directory.

    Args:
        run_dir: The run directory.

    Returns:
        Per provider and form: the AC-1 count, and the AC-2 verdict (local) or the AC-3 cache
        table (cloud). ``w3`` is true when both local forms ran all 12 draws and each holds fewer
        than 10.
    """
    report: dict[str, object] = {}
    local_held: list[int] = []
    for provider in PROVIDERS:
        for form in reveal.FORMS:
            rows = read_jsonl(rows_path(run_dir, provider, form))
            if not rows:
                continue
            entry: dict[str, object] = {"loop": score.loop_count(rows)}
            if provider == "local":
                entry["cache"] = score.cache_verdict(rows, form)
                counted = score.loop_count(rows)
                if counted["draws"] == POPULATION:
                    local_held.append(int(counted["held"]))  # type: ignore[call-overload]
            else:
                entry["cache"] = score.cloud_cache(rows)
            report[f"{provider}/{form}"] = entry
    report["w3"] = len(local_held) == len(reveal.FORMS) and all(h < LOOP_FLOOR for h in local_held)
    return report


def score_cmd(args: argparse.Namespace) -> int:
    """Write and print the report."""
    report = build_report(args.run_dir)
    (args.run_dir / "report.json").write_text(json.dumps(report, indent=2))
    for key, entry in report.items():
        if not isinstance(entry, Mapping):
            print(key, entry)
            continue
        loop_ = entry["loop"]
        cache = entry["cache"]
        verdict = cache.get("verdict") if isinstance(cache, Mapping) else "reported"
        print(key, "loop", loop_, "cache", verdict)
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the command line."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("preflight", "run", "score"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        if name in ("preflight", "run"):
            p.add_argument("--base", type=Path, required=True, help="a captured primary request")
            p.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
            p.add_argument("--url", default=DEFAULT_URL)
            p.add_argument("--deployment", default=DEFAULT_DEPLOYMENT)
            p.add_argument("--max-usd", type=float, default=DEFAULT_MAX_USD)
            p.add_argument(
                "--props-file", type=Path, help="a saved llama-server /props reply (local)"
            )
        if name == "run":
            p.add_argument("--provider", choices=PROVIDERS, required=True)
            p.add_argument("--form", choices=reveal.FORMS, required=True)
            p.add_argument("--llama-model", help="the llama.cpp model name (local)")
            p.add_argument("--quant", help="the quant of the loaded model (local)")
            p.add_argument("--limit", type=int, default=0, help="first N prompts only (dry draw)")
    args = parser.parse_args(argv)
    handlers = {"preflight": preflight, "run": run, "score": score_cmd}
    return handlers[args.cmd](args)


if __name__ == "__main__":
    sys.exit(main())
