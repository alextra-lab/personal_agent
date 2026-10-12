"""FRE-1590 Part 1: does the primary model's first call load the right skill?

The items are the 43 ``skills/*/evals.yaml`` scenarios and the 20 no-skill prompts of FRE-1581
(``g2_skill_choice.load_items``). Three steps, per catalog key (the sampling differs per deployment):

    capture  one fresh-session turn per item through the fre1537 probe gateway, whose model is a
             recording stub, so no model is called           -> <run>/captured/<key>/<safe id>.json
    replay   the captured primary request, sent once per draw to llama.cpp, not streamed
                                                             -> <run>/rows/<key>/part1.jsonl
    score    right / wrong / none per skill, loads per no-skill prompt, missing items with a reason
                                                             -> <run>/rows/<key>/report.json

Run from the repo root (see README.md beside this file):

    uv run python -m scripts.eval.fre1590.triggering capture --run-dir <run> --model <key>
    uv run python -m scripts.eval.fre1590.triggering replay --run-dir <run> --model <key> \
        --llama-model <name> --quant <quant>
    uv run python -m scripts.eval.fre1590.triggering score --run-dir <run> --model <key>

No tool is ever executed. The replay sees only the model's first call.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx
from scripts.eval.fre1537 import capture
from scripts.eval.fre1537.common import RunPaths, append_jsonl, done_pair_keys, read_jsonl, utc_now
from scripts.eval.fre1537.fingerprint import _git_head
from scripts.eval.fre1581.g2_skill_choice import Item, load_items

DEFAULT_URL = "http://127.0.0.1:8600/v1/chat/completions"
DEFAULT_DRAWS = 3
UNKNOWN = "unknown"

# The replay sends the captured body as the gateway sent it, with two changes: `model` is the
# llama.cpp model name, and `stream` is false. Every other captured key stays. That is a wider set
# than fre1537's `llama.primary_from`, which keeps only messages, tools, tool_choice,
# parallel_tool_calls and SAMPLING_KEYS. Its SAMPLING_KEYS drop `max_tokens`, `chat_template_kwargs`
# and llama.cpp's `repeat_penalty`, and `chat_template_kwargs` must stay so that the thinking mode is
# the production one. The one key dropped is `stream_options`: it applies to a streamed reply only,
# and a non-streamed reply always carries `usage`.
DROPPED_KEYS = frozenset({"stream_options"})
# A change of any of these fields makes rows of two configurations. `git_head` is not one: a commit
# that changes the request shows in the prompt and tool hashes.
IDENTITY_FIELDS = (
    "model_key",
    "llama_model",
    "quant",
    "llama_build",
    "system_prompt_sha256",
    "tools_sha256",
    "tool_count",
    "tool_names_sha256",
    "draws",
)

Outcome = Literal["right", "wrong", "none", "malformed", "load"]


@dataclass(frozen=True)
class Part1Paths:
    """Paths of one catalog key inside a run directory.

    Attributes:
        run_dir: The run directory. ``gateway up`` and ``capture`` use the same one.
        model_key: The catalog key, for example ``qwen3.8-flash-next``.
    """

    run_dir: Path
    model_key: str

    @property
    def captured(self) -> Path:
        """Return the directory of this key's captured requests."""
        return self.run_dir / "captured" / self.model_key

    @property
    def rows(self) -> Path:
        """Return the directory of this key's rows, fingerprint and report."""
        return self.run_dir / "rows" / self.model_key

    @property
    def part1(self) -> Path:
        """Return the Part 1 rows file."""
        return self.rows / "part1.jsonl"

    @property
    def fingerprint(self) -> Path:
        """Return the configuration fingerprint file."""
        return self.rows / "fingerprint.json"

    @property
    def report(self) -> Path:
        """Return the score report file."""
        return self.rows / "report.json"

    def captured_file(self, item_id: str) -> Path:
        """Return the captured request file of one item."""
        return self.captured / f"{safe_name(item_id)}.json"


def safe_name(item_id: str) -> str:
    """Return a file name stem for an item id.

    A scenario id is ``<skill>#<n>``. The ``#`` becomes ``__``, and any other character outside
    ``[A-Za-z0-9._-]`` becomes ``_``.

    Args:
        item_id: The item id.

    Returns:
        The stem.
    """
    return re.sub(r"[^A-Za-z0-9._-]", "_", item_id.replace("#", "__"))


# --------------------------------------------------------------------------- capture


def capture_item(
    client: httpx.Client,
    chat_url: str,
    stub_paths: RunPaths,
    item: Item,
    model_key: str,
    settle: float = capture.SETTLE_SECONDS,
) -> dict[str, object]:
    """Run one fresh-session turn for an item and return its record.

    Args:
        client: HTTP client.
        chat_url: The probe gateway's ``/chat`` URL.
        stub_paths: The fre1537 run paths, for the stub's call directory.
        item: The item.
        model_key: The catalog key sent as ``/chat``'s ``model``.
        settle: Seconds to wait for post-turn background calls.

    Returns:
        ``id``, ``kind``, ``expected_skill``, ``prompt``, ``model_key`` and the primary ``body``.

    Raises:
        httpx.HTTPError: On a gateway error.
        ValueError: If the turn holds no single primary request.
    """
    _, files = capture.turn(
        client, chat_url, stub_paths, item.prompt, "OK", None, settle, model_key
    )
    return {
        "id": item.id,
        "kind": item.kind,
        "expected_skill": item.expected_skill,
        "prompt": item.prompt,
        "model_key": model_key,
        "body": capture.primary_body(files, item.prompt),
    }


def run_capture(
    client: httpx.Client,
    chat_url: str,
    paths: Part1Paths,
    items: Sequence[Item],
    settle: float = capture.SETTLE_SECONDS,
) -> int:
    """Capture every item that has no captured file yet.

    Args:
        client: HTTP client.
        chat_url: The probe gateway's ``/chat`` URL.
        paths: The Part 1 paths.
        items: The items to capture.
        settle: Seconds to wait after each turn.

    Returns:
        The number of items that failed. A failed item has no file, so a rerun retries it.
    """
    stub_paths = RunPaths(paths.run_dir)
    paths.captured.mkdir(parents=True, exist_ok=True)
    failed = 0
    for item in items:
        target = paths.captured_file(item.id)
        if target.exists():
            continue
        try:
            record = capture_item(client, chat_url, stub_paths, item, paths.model_key, settle)
        except (httpx.HTTPError, ValueError) as exc:
            failed += 1
            print(item.id, "capture failed:", repr(exc)[:200], file=sys.stderr, flush=True)
            continue
        target.write_text(json.dumps(record))
        print(item.id, "captured", flush=True)
    return failed


def load_captured(paths: Part1Paths, items: Sequence[Item]) -> list[dict[str, object]]:
    """Load the captured records of the items, in item order.

    Args:
        paths: The Part 1 paths.
        items: The items.

    Returns:
        The records that exist. An item without a file is left out.
    """
    out: list[dict[str, object]] = []
    for item in items:
        path = paths.captured_file(item.id)
        if path.exists():
            out.append(json.loads(path.read_text()))
    return out


# --------------------------------------------------------------------------- replay


def replay_request(body: Mapping[str, object], llama_model: str) -> dict[str, object]:
    """Build the llama.cpp request from a captured primary body.

    Args:
        body: The captured primary request body.
        llama_model: The llama.cpp model name.

    Returns:
        A new body: every captured key except :data:`DROPPED_KEYS`, with ``model`` set to
        ``llama_model`` and ``stream`` false.
    """
    request = {k: v for k, v in body.items() if k not in DROPPED_KEYS}
    request["model"] = llama_model
    request["stream"] = False
    return request


@dataclass(frozen=True)
class FirstCall:
    """The tool calls of the model's first reply.

    Attributes:
        tool_names: Every tool name, in order.
        skills: The ``name`` argument of every ``read_skill`` call that has one, in order.
        malformed: The raw arguments of every ``read_skill`` call without a readable ``name``.
    """

    tool_names: tuple[str, ...]
    skills: tuple[str, ...]
    malformed: tuple[str, ...]


def parse_tool_calls(message: Mapping[str, object]) -> FirstCall:
    """Read the tool calls of a reply message.

    Args:
        message: ``choices[0].message`` of a chat-completions reply.

    Returns:
        The tool names, the ``read_skill`` skill names and the malformed ``read_skill`` arguments.
    """
    calls = message.get("tool_calls")
    names: list[str] = []
    skills: list[str] = []
    malformed: list[str] = []
    for call in calls if isinstance(calls, list) else []:
        function = call.get("function") if isinstance(call, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name"))
        names.append(name)
        if name != "read_skill":
            continue
        raw = function.get("arguments")
        try:
            args = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            args = None
        skill = args.get("name") if isinstance(args, Mapping) else None
        if isinstance(skill, str) and skill:
            skills.append(skill)
        else:
            malformed.append(raw if isinstance(raw, str) else json.dumps(raw))
    return FirstCall(tuple(names), tuple(skills), tuple(malformed))


def call_llama(client: httpx.Client, url: str, request: Mapping[str, object]) -> dict[str, object]:
    """Send one non-streamed request and read the first call.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        request: The request body.

    Returns:
        The row fields: ``tool_names``, ``read_skill_names``, ``read_skill_malformed``,
        ``finish_reason``, ``usage``, ``wall_s``, ``content_chars``, ``reasoning_chars`` and
        ``system_fingerprint``.

    Raises:
        httpx.HTTPError: On a transport error or a non-2xx reply.
        ValueError: On a reply that is not JSON or has no ``choices[0].message``.
    """
    t0 = time.monotonic()
    response = client.post(url, json=request)
    response.raise_for_status()
    wall = round(time.monotonic() - t0, 2)
    data = response.json()
    choices = data.get("choices") if isinstance(data, Mapping) else None
    choice = choices[0] if isinstance(choices, list) and choices else None
    message = choice.get("message") if isinstance(choice, Mapping) else None
    if not isinstance(choice, Mapping) or not isinstance(message, Mapping):
        raise ValueError(f"reply has no choices[0].message: {str(data)[:200]}")
    first = parse_tool_calls(message)
    reasoning = message.get("reasoning_content") or message.get("reasoning") or ""
    return {
        "tool_names": list(first.tool_names),
        "read_skill_names": list(first.skills),
        "read_skill_malformed": list(first.malformed),
        "finish_reason": choice.get("finish_reason"),
        "usage": data.get("usage") or {},
        "wall_s": wall,
        "content_chars": len(str(message.get("content") or "")),
        "reasoning_chars": len(str(reasoning)),
        "system_fingerprint": data.get("system_fingerprint"),
    }


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def _combined(hashes: Iterable[str]) -> tuple[str, int]:
    """Return one hash for a set of hashes, and the number of distinct ones.

    One distinct value returns that value, so a single system prompt reads as its own SHA-256.
    """
    distinct = sorted(set(hashes))
    if len(distinct) == 1:
        return distinct[0], 1
    return _sha256("\n".join(distinct)), len(distinct)


def _system_prompt(body: Mapping[str, object]) -> str:
    messages = body.get("messages")
    first = messages[0] if isinstance(messages, list) and messages else None
    if isinstance(first, Mapping) and first.get("role") == "system":
        return str(first.get("content"))
    return ""


def _tool_names(body: Mapping[str, object]) -> list[str]:
    tools = body.get("tools")
    names: list[str] = []
    for tool in tools if isinstance(tools, list) else []:
        function = tool.get("function") if isinstance(tool, Mapping) else None
        names.append(str(function.get("name")) if isinstance(function, Mapping) else "")
    return names


def build_fingerprint(
    model_key: str,
    llama_model: str,
    quant: str,
    draws: int,
    captured: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Build the configuration fingerprint of a replay.

    Args:
        model_key: The catalog key.
        llama_model: The llama.cpp model name.
        quant: The model quant.
        draws: Draws per item.
        captured: The captured records.

    Returns:
        The fingerprint. ``llama_build`` reads ``unknown`` until the first reply fills it.
    """
    bodies: list[Mapping[str, object]] = []
    for record in captured:
        body = record.get("body")
        if isinstance(body, Mapping):
            bodies.append(body)
    system_sha, system_variants = _combined(_sha256(_system_prompt(b)) for b in bodies)
    tools_sha, tool_variants = _combined(
        _sha256(json.dumps(b.get("tools"), sort_keys=True)) for b in bodies
    )
    names = sorted({n for b in bodies for n in _tool_names(b)})
    return {
        "model_key": model_key,
        "llama_model": llama_model,
        "quant": quant,
        "llama_build": UNKNOWN,
        "git_head": _git_head(),
        "system_prompt_sha256": system_sha,
        "system_prompt_variants": system_variants,
        "tools_sha256": tools_sha,
        "tool_variants": tool_variants,
        "tool_count": len(names),
        "tool_names_sha256": _sha256(",".join(names)),
        "draws": draws,
        "items_captured": len(bodies),
    }


def _same_configuration(existing: Mapping[str, object], new: Mapping[str, object]) -> bool:
    """Compare two fingerprints. A build that the new one does not know yet is not a difference."""
    for field in IDENTITY_FIELDS:
        old, fresh = existing.get(field), new.get(field)
        if old == fresh or (field == "llama_build" and fresh == UNKNOWN):
            continue
        return False
    return True


def ensure_fingerprint(paths: Part1Paths, fingerprint: Mapping[str, object]) -> None:
    """Write the fingerprint, or check it against the one the rows already hold.

    Args:
        paths: The Part 1 paths.
        fingerprint: The fingerprint of the replay about to start.

    Raises:
        SystemExit: If the rows hold the fingerprint of a different configuration.
    """
    if paths.fingerprint.exists():
        existing = json.loads(paths.fingerprint.read_text())
        if not _same_configuration(existing, fingerprint):
            changed = [f for f in IDENTITY_FIELDS if existing.get(f) != fingerprint.get(f)]
            raise SystemExit(
                f"{paths.rows} holds rows of a different configuration ({', '.join(changed)}); "
                "use a new --run-dir"
            )
        return
    paths.rows.mkdir(parents=True, exist_ok=True)
    paths.fingerprint.write_text(json.dumps(fingerprint, indent=2, sort_keys=True))


def record_build(paths: Part1Paths, system_fingerprint: object) -> None:
    """Fill the llama.cpp build from a reply, or stop when the build changed.

    Args:
        paths: The Part 1 paths.
        system_fingerprint: The ``system_fingerprint`` of a reply.

    Raises:
        SystemExit: If the reply reports a build other than the recorded one.
    """
    if not isinstance(system_fingerprint, str) or not system_fingerprint:
        return
    stored = json.loads(paths.fingerprint.read_text())
    build = stored.get("llama_build")
    if build in (None, "", UNKNOWN):
        stored["llama_build"] = system_fingerprint
        paths.fingerprint.write_text(json.dumps(stored, indent=2, sort_keys=True))
    elif build != system_fingerprint:
        raise SystemExit(
            f"llama.cpp build changed from {build} to {system_fingerprint}; the reply is not "
            "recorded. Use a new --run-dir"
        )


def run_replay(
    client: httpx.Client,
    url: str,
    paths: Part1Paths,
    captured: Sequence[Mapping[str, object]],
    llama_model: str,
    draws: int,
) -> int:
    """Send each captured request once per draw, and append one row per call.

    A rerun skips each ``(id, draw)`` that has a row without an error.

    Args:
        client: HTTP client.
        url: The chat-completions URL.
        paths: The Part 1 paths. The fingerprint is already written.
        captured: The captured records.
        llama_model: The llama.cpp model name.
        draws: Draws per item.

    Returns:
        The number of error rows that this run wrote.
    """
    seen = done_pair_keys(paths.part1, "id")
    errors = 0
    for draw in range(draws):
        for record in captured:
            item_id = str(record["id"])
            if (item_id, draw) in seen:
                continue
            body = record["body"]
            assert isinstance(body, Mapping)
            row: dict[str, object] = {
                "id": item_id,
                "kind": record["kind"],
                "expected_skill": record["expected_skill"],
                "draw": draw,
                "model_key": paths.model_key,
                "llama_model": llama_model,
                "ts": utc_now(),
            }
            try:
                result = call_llama(client, url, replay_request(body, llama_model))
            except (httpx.HTTPError, ValueError) as exc:
                row["error"] = repr(exc)[:300]
                errors += 1
            else:
                record_build(paths, result["system_fingerprint"])
                row.update(result)
            append_jsonl(paths.part1, row)
            print(
                "draw",
                draw,
                item_id,
                row.get("read_skill_names"),
                row.get("wall_s"),
                str(row.get("error", ""))[:80],
                flush=True,
            )
    return errors


# --------------------------------------------------------------------------- score


def classify(
    kind: str, expected: str | None, skills: Sequence[str], malformed: Sequence[str]
) -> Outcome:
    """Classify one draw of the first call.

    Args:
        kind: ``scenario`` or ``no_skill``.
        expected: The expected skill of a scenario.
        skills: The ``read_skill`` skill names of the first call.
        malformed: The malformed ``read_skill`` arguments of the first call.

    Returns:
        For a no-skill prompt: ``load`` on any ``read_skill`` call, else ``none``. For a scenario:
        ``wrong`` if any other skill loads; else ``malformed`` if a ``read_skill`` call has no
        readable name; else ``right`` if the expected skill loads; else ``none``.
    """
    if kind == "no_skill":
        return "load" if skills or malformed else "none"
    if any(s != expected for s in skills):
        return "wrong"
    if malformed:
        return "malformed"
    return "right" if skills else "none"


def _kept_rows(
    rows: Sequence[Mapping[str, object]], draws: int
) -> dict[str, dict[int, Mapping[str, object]]]:
    """Return the last row without an error per item and draw, for draws below ``draws``."""
    kept: dict[str, dict[int, Mapping[str, object]]] = {}
    for r in rows:
        draw = int(str(r["draw"]))
        if "error" not in r and draw < draws:
            kept.setdefault(str(r["id"]), {})[draw] = r
    return kept


def _names(row: Mapping[str, object], field: str) -> list[str]:
    value = row.get(field)
    return [str(x) for x in value] if isinstance(value, list) else []


def score_rows(
    items: Sequence[Item],
    rows: Sequence[Mapping[str, object]],
    draws: int,
    captured_ids: Iterable[str],
) -> dict[str, object]:
    """Build the AC-2 table of one catalog key.

    Args:
        items: Every item.
        rows: The Part 1 rows.
        draws: Draws per item, from the fingerprint.
        captured_ids: The ids that have a captured file.

    Returns:
        ``skills`` (per expected skill: scenarios, draws and the count of each outcome, with the
        other skills that loaded), ``no_skill`` (per prompt: draws, loads and the skills that
        loaded), ``missing`` (items without a row without an error, each with a reason) and
        ``incomplete`` (items with fewer good draws than ``draws``, each with a reason).
    """
    kept = _kept_rows(rows, draws)
    captured = set(captured_ids)
    last_error: dict[str, str] = {}
    for r in rows:
        if "error" in r:
            last_error[str(r["id"])] = str(r["error"])
    skills: dict[str, dict[str, object]] = {}
    no_skill: dict[str, dict[str, object]] = {}
    missing: list[dict[str, object]] = []
    incomplete: list[dict[str, object]] = []
    for item in items:
        done = kept.get(item.id, {})
        if not done:
            if item.id not in captured:
                reason = "not captured"
            elif item.id in last_error:
                reason = f"every replay errored: {last_error[item.id][:200]}"
            else:
                reason = "captured, not replayed"
            missing.append({"id": item.id, "reason": reason})
        elif len(done) < draws:
            reason = f"{len(done)} of {draws} draws"
            if item.id in last_error:
                reason += f"; last error: {last_error[item.id][:200]}"
            incomplete.append({"id": item.id, "draws_done": len(done), "reason": reason})
        outcomes = [
            (
                classify(
                    item.kind,
                    item.expected_skill,
                    _names(r, "read_skill_names"),
                    _names(r, "read_skill_malformed"),
                ),
                _names(r, "read_skill_names"),
            )
            for _, r in sorted(done.items())
        ]
        if item.kind == "no_skill":
            no_skill[item.id] = {
                "draws": len(outcomes),
                "loads": sum(o == "load" for o, _ in outcomes),
                "skills": sorted({s for _, names in outcomes for s in names}),
            }
            continue
        entry = skills.setdefault(
            str(item.expected_skill),
            {"scenarios": 0, "draws": 0, "right": 0, "wrong": 0, "none": 0, "malformed": 0},
        )
        entry["scenarios"] = int(str(entry["scenarios"])) + 1
        entry["draws"] = int(str(entry["draws"])) + len(outcomes)
        others = entry.setdefault("wrong_loads", {})
        assert isinstance(others, dict)
        for outcome, names in outcomes:
            entry[outcome] = int(str(entry[outcome])) + 1
            for s in names:
                if s != item.expected_skill:
                    others[s] = others.get(s, 0) + 1
    return {
        "draws": draws,
        "skills": dict(sorted(skills.items())),
        "no_skill": no_skill,
        "missing": missing,
        "incomplete": incomplete,
    }


def render(report: Mapping[str, object]) -> str:
    """Render the AC-2 table as text.

    Args:
        report: The output of :func:`score_rows`, with ``model_key`` added.

    Returns:
        The text.
    """
    lines = [f"FRE-1590 Part 1: first-call skill loading, {report.get('model_key')}"]
    lines.append(f"  draws per item: {report['draws']}")
    lines.append("")
    lines.append(
        f"  {'skill':<24} {'scen':>4} {'draws':>5} {'right':>5} {'wrong':>5} "
        f"{'none':>5} {'malf':>5}  other skills loaded"
    )
    skills = report["skills"]
    assert isinstance(skills, Mapping)
    totals = {"scenarios": 0, "draws": 0, "right": 0, "wrong": 0, "none": 0, "malformed": 0}
    for skill, e in skills.items():
        assert isinstance(e, Mapping)
        for k in totals:
            totals[k] += int(str(e[k]))
        others = e.get("wrong_loads") or {}
        assert isinstance(others, Mapping)
        lines.append(
            f"  {skill:<24} {e['scenarios']:>4} {e['draws']:>5} {e['right']:>5} {e['wrong']:>5} "
            f"{e['none']:>5} {e['malformed']:>5}  "
            + (", ".join(f"{s} x{n}" for s, n in sorted(others.items())) or "-")
        )
    lines.append(
        f"  {'TOTAL':<24} {totals['scenarios']:>4} {totals['draws']:>5} {totals['right']:>5} "
        f"{totals['wrong']:>5} {totals['none']:>5} {totals['malformed']:>5}"
    )
    lines.append("")
    no_skill = report["no_skill"]
    assert isinstance(no_skill, Mapping)
    loads = 0
    lines.append(f"  {'no-skill prompt':<24} {'draws':>5} {'loads':>5}  skills loaded")
    for item_id, e in no_skill.items():
        assert isinstance(e, Mapping)
        loads += int(str(e["loads"]))
        names = e.get("skills") or []
        assert isinstance(names, list)
        lines.append(f"  {item_id:<24} {e['draws']:>5} {e['loads']:>5}  {', '.join(names) or '-'}")
    lines.append(f"  no-skill draws that loaded a skill: {loads}")
    lines.append("")
    for key in ("missing", "incomplete"):
        entries = report[key]
        assert isinstance(entries, list)
        lines.append(f"  {key}: {len(entries)}")
        lines += [f"    {e['id']}: {e['reason']}" for e in entries]
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- CLI


def _select(items: Sequence[Item], limit: int) -> Sequence[Item]:
    return items[:limit] if limit else items


def _cmd_capture(args: argparse.Namespace) -> int:
    paths = Part1Paths(args.run_dir, args.model)
    items = _select(load_items(), args.limit)
    with httpx.Client() as client:
        failed = run_capture(client, args.chat_url, paths, items)
    print(f"captured {len(load_captured(paths, items))} of {len(items)} items in {paths.captured}")
    return 1 if failed else 0


def _cmd_replay(args: argparse.Namespace) -> int:
    paths = Part1Paths(args.run_dir, args.model)
    captured = load_captured(paths, _select(load_items(), args.limit))
    if not captured:
        raise SystemExit(f"no captured requests in {paths.captured}: run capture first")
    fingerprint = build_fingerprint(args.model, args.llama_model, args.quant, args.draws, captured)
    ensure_fingerprint(paths, fingerprint)
    with httpx.Client(timeout=900.0) as client:
        errors = run_replay(client, args.url, paths, captured, args.llama_model, args.draws)
    print(f"rows in {paths.part1}; {errors} error rows this run. Next: score")
    return 1 if errors else 0


def _cmd_score(args: argparse.Namespace) -> int:
    paths = Part1Paths(args.run_dir, args.model)
    if not paths.fingerprint.exists():
        raise SystemExit(f"{paths.fingerprint} is missing: run replay first")
    fingerprint = json.loads(paths.fingerprint.read_text())
    items = load_items()
    captured_ids = [i.id for i in items if paths.captured_file(i.id).exists()]
    report: dict[str, object] = {
        "model_key": args.model,
        "fingerprint": fingerprint,
        **score_rows(items, read_jsonl(paths.part1), int(fingerprint["draws"]), captured_ids),
    }
    paths.report.write_text(json.dumps(report, indent=2))
    sys.stdout.write(render(report))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run one step: capture, replay or score.

    Args:
        argv: Command-line arguments. Default is ``sys.argv[1:]``.

    Returns:
        0 on success. 1 when a capture failed or a replay wrote an error row.
    """
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("capture", "replay", "score"):
        p = sub.add_parser(name)
        p.add_argument("--run-dir", type=Path, required=True)
        p.add_argument("--model", required=True, help="catalog key, e.g. qwen3.8-flash-next")
        if name == "score":
            continue
        p.add_argument("--limit", type=int, default=0, help="first N items only (a dry run)")
        if name == "capture":
            p.add_argument("--chat-url", default=capture.DEFAULT_CHAT_URL)
        else:
            p.add_argument("--llama-model", required=True, help="the llama.cpp model name")
            p.add_argument("--quant", required=True, help="the quant of the loaded model")
            p.add_argument("--draws", type=int, default=DEFAULT_DRAWS)
            p.add_argument("--url", default=DEFAULT_URL)
    args = parser.parse_args(argv)
    if args.command == "capture":
        return _cmd_capture(args)
    if args.command == "replay":
        return _cmd_replay(args)
    return _cmd_score(args)


if __name__ == "__main__":
    sys.exit(main())
