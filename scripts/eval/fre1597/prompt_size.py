"""FRE-1597 / ADR-0156 A5 (T-D1): the prompt-size drop (AC-3, ADR AC-F1).

Three first requests of one no-skill prompt, each sent to llama.cpp with an empty cache:

- ``B``: captured at ``b02503fc``, the commit before T-C2 (FRE-1596);
- ``B-minus``: ``B`` with only the ``query_telemetry`` definition removed;
- ``A``: captured on the final build, with its non-system messages aligned to ``B``'s.

    capture  --label B|A   one probe-gateway turn, text reply   -> <run>/prompt_size/<label>.json
    measure                the three counts, the drop and the diff -> <run>/prompt_size/report.json

A token count is ``cache_n + prompt_n`` with ``cache_n`` 0: each body goes out with
``cache_prompt: false``, and a reply that reused the cache is refused. ``max_tokens`` is 1, which
changes the output, not the prompt.

The test is ``tokens(B) > tokens(B-minus)`` and ``tokens(B) - tokens(A) >= 0.8 x (tokens(B) -
tokens(B-minus))``. It also fails if ``B`` and ``A`` differ in a way that is not a T-C2 change: a
changed block of system-prompt or tool-definition lines must name a skill or tool that T-C2 renamed,
split or removed, the only removed tool is ``query_telemetry``, no tool is added, and every other key
is equal.

Run from the repo root. See README.md beside this file.
"""

from __future__ import annotations

import argparse
import copy
import difflib
import json
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537.common import utc_now
from scripts.eval.fre1581.g2_skill_choice import load_items
from scripts.eval.fre1594 import spike
from scripts.eval.fre1594.reveal import tool_name
from scripts.eval.fre1594.transport import LlamaTransport, Transport
from scripts.eval.fre1597 import common
from scripts.eval.fre1597.calls import JsonDict
from scripts.eval.fre1597.routing import AREA_SKILLS

PROMPT_ID = "ns01"
REMOVED_TOOL = "query_telemetry"
SHARE = 0.8
LABELS = ("B", "A")
# The names that T-C2 removed, renamed or split (ADR-0156 A4, A5.1), and the names it added.
T_C2_NAMES: tuple[str, ...] = (
    REMOVED_TOOL,
    "query-elasticsearch",
    "query-tempo",
    "self-telemetry",
    "turn-forensics",
    "infrastructure-health",
    "neo4j-direct",
    "search_logs",
    "measure_model_latency",
    *AREA_SKILLS,
)
_TIME = re.compile(r"^(Current (?:date|time): ).*$", re.M)
_MEMORY_CALL = re.compile(r"call_mem_[0-9a-f]+")
_TOOL_COUNT = re.compile(r"^Available tools \((\d+) total\):$")


class SizeError(Exception):
    """A body or a reply that the AC-3 test cannot use."""


def _messages(body: Mapping[str, object]) -> list[JsonDict]:
    messages = body.get("messages")
    if not isinstance(messages, list):
        raise SizeError("a body has no messages list")
    return messages


def _tools(body: Mapping[str, object]) -> list[JsonDict]:
    tools = body.get("tools")
    if not isinstance(tools, list):
        raise SizeError("a body has no tools list")
    return tools


def b_minus(b: Mapping[str, object]) -> JsonDict:
    """Return ``B`` with only the ``query_telemetry`` definition removed.

    Raises:
        SizeError: If ``B`` does not hold that definition exactly once.
    """
    tools = _tools(b)
    kept = [t for t in tools if tool_name(t) != REMOVED_TOOL]
    if len(kept) != len(tools) - 1:
        raise SizeError(f"B must hold {REMOVED_TOOL} exactly once")
    return {**copy.deepcopy(dict(b)), "tools": copy.deepcopy(kept)}


def _neutral(messages: Sequence[Mapping[str, object]]) -> str:
    """Return the messages as text, with the time lines and the memory call ids masked."""
    text = json.dumps(list(messages), sort_keys=True)
    return _MEMORY_CALL.sub("call_mem_*", _TIME.sub(r"\1*", text.replace("\\n", "\n")))


def align(a: Mapping[str, object], b: Mapping[str, object]) -> JsonDict:
    """Return ``A`` with its non-system messages replaced by ``B``'s.

    Raises:
        SizeError: If the non-system messages differ in more than the time lines and the memory
            call ids.
    """
    head_a, head_b = _messages(a)[1:], _messages(b)[1:]
    if _neutral(head_a) != _neutral(head_b):
        raise SizeError("A's and B's non-system messages differ in more than the time and call id")
    aligned = copy.deepcopy(dict(a))
    aligned["messages"] = [_messages(a)[0], *copy.deepcopy(head_b)]
    return aligned


def _blocks(old: str, new: str) -> list[tuple[list[str], list[str]]]:
    """Return each changed block of lines as (removed lines, added lines)."""
    old_lines, new_lines = old.splitlines(), new.splitlines()
    matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    return [
        (old_lines[i1:i2], new_lines[j1:j2])
        for tag, i1, i2, j1, j2 in matcher.get_opcodes()
        if tag != "equal"
    ]


def _named(lines: Sequence[str]) -> bool:
    return any(name in line for line in lines for name in T_C2_NAMES)


def _tool_count_change(removed: Sequence[str], added: Sequence[str], fewer: int) -> bool:
    """Return True for the tool-count line of the system prompt, changed by the tool difference."""
    if len(removed) != 1 or len(added) != 1:
        return False
    old, new = _TOOL_COUNT.match(removed[0]), _TOOL_COUNT.match(added[0])
    return bool(old and new) and int(old.group(1)) - int(new.group(1)) == fewer  # type: ignore[union-attr]


def body_diff(b: Mapping[str, object], a: Mapping[str, object]) -> dict[str, object]:
    """Compare ``B`` with the aligned ``A``.

    Returns:
        ``system_blocks`` and ``tool_blocks`` (each changed block, with ``t_c2`` true when a line
        names a T-C2 skill or tool), ``tools_removed``, ``tools_added`` and ``unexpected`` (every
        difference that is not a T-C2 change).
    """
    unexpected: list[str] = []
    for key in sorted((set(a) | set(b)) - {"messages", "tools"}):
        if a.get(key) != b.get(key):
            unexpected.append(f"key {key}: {b.get(key)!r} -> {a.get(key)!r}")
    if _messages(a)[1:] != _messages(b)[1:]:
        unexpected.append("the non-system messages differ")

    tools_b = {tool_name(t): t for t in _tools(b)}
    tools_a = {tool_name(t): t for t in _tools(a)}
    removed_tools = sorted(set(tools_b) - set(tools_a))
    added_tools = sorted(set(tools_a) - set(tools_b))

    system_blocks = []
    for removed, added in _blocks(str(_messages(b)[0]["content"]), str(_messages(a)[0]["content"])):
        allowed = _named(removed + added) or _tool_count_change(
            removed, added, len(tools_b) - len(tools_a)
        )
        system_blocks.append({"removed": removed, "added": added, "t_c2": allowed})
        if not allowed:
            unexpected.append(f"system prompt: -{removed} +{added}")

    if removed_tools != [REMOVED_TOOL]:
        unexpected.append(f"tools removed: {removed_tools}")
    if added_tools:
        unexpected.append(f"tools added: {added_tools}")
    if [n for n in tools_b if n in tools_a] != [n for n in tools_a if n in tools_b]:
        unexpected.append("the order of the kept tools changed")
    tool_blocks = []
    for name in [n for n in tools_b if n in tools_a]:
        old = json.dumps(tools_b[name], indent=1, sort_keys=True)
        new = json.dumps(tools_a[name], indent=1, sort_keys=True)
        for removed, added in _blocks(old, new):
            allowed = _named(removed + added)
            tool_blocks.append({"tool": name, "removed": removed, "added": added, "t_c2": allowed})
            if not allowed:
                unexpected.append(f"tool {name}: -{removed} +{added}")
    return {
        "system_blocks": system_blocks,
        "tool_blocks": tool_blocks,
        "tools_removed": removed_tools,
        "tools_added": added_tools,
        "unexpected": unexpected,
    }


def count_tokens(sender: Transport, bodies: Mapping[str, Mapping[str, object]]) -> dict[str, int]:
    """Send each body once with an empty cache and return its prompt token count.

    Args:
        sender: The llama.cpp transport.
        bodies: The bodies by label, in the order to send them.

    Returns:
        ``cache_n + prompt_n`` for each label.

    Raises:
        SizeError: A call failed, has no timings, or reused the cache.
    """
    counts: dict[str, int] = {}
    for label, body in bodies.items():
        reply = sender.send({**body, "cache_prompt": False, "max_tokens": 1})
        cache_n, prompt_n = reply.timings.get("cache_n"), reply.timings.get("prompt_n")
        if not reply.ok or cache_n is None or prompt_n is None:
            raise SizeError(f"{label}: no timings ({reply.error})")
        if cache_n != 0:
            raise SizeError(f"{label}: cache_n is {cache_n}, not 0")
        counts[label] = int(cache_n + prompt_n)
    return counts


def drop_verdict(counts: Mapping[str, int]) -> dict[str, object]:
    """Decide the AC-F1 test on the three counts."""
    definition = counts["B"] - counts["B-minus"]
    drop = counts["B"] - counts["A"]
    share = drop / definition if definition > 0 else None
    passed = definition > 0 and drop >= SHARE * definition
    return {
        "definition_tokens": definition,
        "drop_tokens": drop,
        "share": share,
        "bound": SHARE,
        "verdict": "pass" if passed else "fail",
    }


# --------------------------------------------------------------------------- CLI


def _root(run_dir: Path) -> Path:
    return run_dir / "prompt_size"


def _prompt() -> str:
    return next(i.prompt for i in load_items() if i.id == PROMPT_ID)


def _cmd_capture(args: argparse.Namespace) -> int:
    with httpx.Client() as client:
        build = common.gateway_build(args.chat_url)
        two = common.capture_turn(
            client, args.chat_url, args.run_dir, _prompt(), model_key=args.model, skill=None
        )
    meta = {"label": args.label, "gateway_build": build, "captured_at": utc_now()}
    common.save_capture(_root(args.run_dir) / f"{args.label}.json", two, meta)
    print(args.label, "captured; gateway build", build)
    return 0


def _cmd_measure(args: argparse.Namespace) -> int:
    root = _root(args.run_dir)
    meta_b, b = common.load_capture(root / "B.json")
    meta_a, a = common.load_capture(root / "A.json")
    aligned = align(a.first, b.first)
    bodies = {"B": b.first, "B-minus": b_minus(b.first), "A": aligned}
    diff = body_diff(b.first, aligned)
    client = httpx.Client(timeout=httpx.Timeout(900.0, connect=10.0))
    try:
        pins = common.local_pins(args, client)
        sender = common.RetryOnce(LlamaTransport(client, args.url, args.llama_model))
        counts = count_tokens(sender, bodies)
    finally:
        client.close()
    drop = drop_verdict(counts)
    verdict = "pass" if drop["verdict"] == "pass" and not diff["unexpected"] else "fail"
    report = {
        "at": utc_now(),
        "prompt_id": PROMPT_ID,
        "gateway_builds": {"B": meta_b.get("gateway_build"), "A": meta_a.get("gateway_build")},
        "body_sha256": {k: common.body_sha(v) for k, v in bodies.items()},
        "tokens": counts,
        "drop": drop,
        "diff": diff,
        "pins": pins,
        "git_head": spike._git_head(),
        "verdict": verdict,
    }
    (root / "report.json").write_text(json.dumps(report, indent=2))
    print(json.dumps({k: report[k] for k in ("tokens", "drop", "verdict")}, indent=2))
    print("unexpected differences:", diff["unexpected"] or "none")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step: capture or measure."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("capture")
    p.add_argument("--run-dir", type=Path, required=True)
    p.add_argument("--label", choices=LABELS, required=True)
    p.add_argument("--chat-url", default=capture.DEFAULT_CHAT_URL)
    p.add_argument("--model", default=common.DEFAULT_MODEL_KEY, help="the catalog key")
    p = sub.add_parser("measure")
    p.add_argument("--run-dir", type=Path, required=True)
    common.add_local_args(p)
    args = parser.parse_args(argv)
    return _cmd_capture(args) if args.command == "capture" else _cmd_measure(args)


if __name__ == "__main__":
    sys.exit(main())
