"""FRE-1615 Phase 0: the true token count of a request body, with no model call.

llama.cpp's ``/tokenize`` and ``/apply-template`` answer 403 behind slm_server's router (2026-10-18), so the
count is made offline. It renders the body with the chat template that llama.cpp serves and tokenizes the text
with the model's own tokenizer:

- the template is ``chat_template`` from a saved ``/props`` (``--props``), which equals
  ``unsloth/Qwen3.8-Flash-Next`` ``chat_template.jinja`` byte for byte;
- the tokenizer is that repository's ``tokenizer.json`` (``--tokenizer``);
- the template variables are the server's ``--chat-template-kwargs``, then the body's
  ``chat_template_kwargs`` on top, as llama.cpp merges them.

``calibrate`` checks the method on the FRE-1597 bodies that llama.cpp counted (``cache_n + prompt_n`` with
``cache_n`` 0). ``count`` splits a body into the template's tool preamble, the tool definitions, the system
prompt and the rest.

    calibrate --report <prompt_size/report.json> ...
    count     --body <file.json> [--json]

Run from the repo root.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path

import jinja2
from jinja2.sandbox import ImmutableSandboxedEnvironment
from tokenizers import Tokenizer

JsonDict = dict[str, object]
# The server's --chat-template-kwargs (telemetry/.../pins/server-args.txt, 2026-10-13).
SERVER_KWARGS: JsonDict = {
    "enable_thinking": True,
    "preserve_thinking": False,
    "reasoning_effort": "medium",
}
_TOOLS_OPEN = "# Tools\n\nYou have access to the following functions:\n\n<tools>"
_TOOLS_CLOSE = "\n</tools>"
_SYSTEM_END = "<|im_end|>\n"


def _object_arguments(messages: Sequence[Mapping[str, object]]) -> list[JsonDict]:
    """Parse each tool call's JSON-string arguments into an object.

    llama.cpp does this before it renders a template whose caps say ``supports_object_arguments``.
    """
    out: list[JsonDict] = []
    for message in messages:
        calls = message.get("tool_calls")
        if not isinstance(calls, list):
            out.append(dict(message))
            continue
        parsed = []
        for call in calls:
            function = dict(call["function"])
            if isinstance(function.get("arguments"), str):
                function["arguments"] = json.loads(function["arguments"] or "{}")
            parsed.append({**call, "function": function})
        out.append({**message, "tool_calls": parsed})
    return out


class Counter:
    """Renders a request body with the served template and counts its tokens."""

    def __init__(self, template: str, tokenizer: Tokenizer) -> None:
        """Build the counter.

        Args:
            template: The served chat template (Jinja).
            tokenizer: The model's tokenizer.
        """
        env = ImmutableSandboxedEnvironment(trim_blocks=True, lstrip_blocks=True)

        def raise_exception(message: str) -> None:
            raise jinja2.TemplateError(message)

        def tojson(value: object, indent: int | None = None) -> str:
            return json.dumps(value, ensure_ascii=False, indent=indent)

        env.globals["raise_exception"] = raise_exception
        env.filters["tojson"] = tojson
        self._template = env.from_string(template)
        self._tokenizer = tokenizer

    def render(self, body: Mapping[str, object]) -> str:
        """Return the prompt text that llama.cpp builds from ``body``.

        Args:
            body: An OpenAI chat-completions request body.

        Returns:
            The rendered prompt, generation prompt included.
        """
        kwargs = {**SERVER_KWARGS, **dict(body.get("chat_template_kwargs") or {})}  # type: ignore[call-overload]
        return self._template.render(
            messages=_object_arguments(body["messages"]),  # type: ignore[arg-type]
            tools=body.get("tools") or None,
            add_generation_prompt=True,
            **kwargs,
        )

    def tokens(self, text: str) -> int:
        """Return the token count of ``text``, special tokens parsed as llama.cpp does."""
        return len(self._tokenizer.encode(text, add_special_tokens=False).ids)

    def breakdown(self, body: Mapping[str, object]) -> dict[str, int]:
        """Split the rendered prompt of ``body`` into parts and count each.

        Args:
            body: A request body.

        Returns:
            ``total``, ``tool_definitions`` (the JSON lines inside ``<tools>``), ``tool_preamble`` (the
            template's own text around them), ``system_prompt`` (the harness's system text) and
            ``conversation`` (every message after the system block, and the generation prompt).
        """
        text = self.render(body)
        total = self.tokens(text)
        system_end = text.index(_SYSTEM_END) + len(_SYSTEM_END)
        system_block, rest = text[:system_end], text[system_end:]
        messages = body["messages"]
        assert isinstance(messages, list)
        system_text = str(messages[0]["content"]).strip() if messages[0]["role"] == "system" else ""
        if _TOOLS_OPEN in system_block:
            start = system_block.index(_TOOLS_OPEN) + len(_TOOLS_OPEN)
            end = system_block.index(_TOOLS_CLOSE)
            definitions = self.tokens(system_block[start:end])
        else:
            definitions = 0
        system = self.tokens(system_text) if system_text else 0
        return {
            "total": total,
            "tool_definitions": definitions,
            "tool_preamble": self.tokens(system_block) - definitions - system,
            "system_prompt": system,
            "conversation": self.tokens(rest),
        }


def load_counter(props: Path, tokenizer: Path) -> Counter:
    """Build a counter from a saved ``/props`` file and a ``tokenizer.json``."""
    template = json.loads(props.read_text())["chat_template"]
    return Counter(template, Tokenizer.from_file(str(tokenizer)))


def tool_tokens(counter: Counter, body: Mapping[str, object]) -> list[tuple[str, int]]:
    """Return each tool's name and the token count of its rendered definition line."""
    out = []
    for tool in body.get("tools") or []:  # type: ignore[union-attr]
        name = tool["function"]["name"]
        out.append((name, counter.tokens("\n" + json.dumps(tool, ensure_ascii=False))))
    return out


# --------------------------------------------------------------------------- CLI


def _calibrate(args: argparse.Namespace) -> int:
    from scripts.eval.fre1597 import common, prompt_size

    counter = load_counter(args.props, args.tokenizer)
    worst = 0
    for report_path in args.report:
        report = json.loads(report_path.read_text())
        root = report_path.parent
        _, b = common.load_capture(root / "B.json")
        _, a = common.load_capture(root / "A.json")
        bodies = {
            "B": b.first,
            "B-minus": prompt_size.b_minus(b.first),
            "A": prompt_size.align(a.first, b.first),
        }
        for label, body in bodies.items():
            if common.body_sha(body) != report["body_sha256"][label]:
                print(f"{report_path} {label}: the rebuilt body differs from the measured one")
                return 1
            mine = counter.breakdown(body)["total"]
            measured = int(report["tokens"][label])
            worst = max(worst, abs(mine - measured))
            print(
                f"{report_path.parent.parent.name} {label}: offline {mine}  llama.cpp {measured}  diff {mine - measured}"
            )
    print("largest difference:", worst)
    return 0 if worst <= args.tolerance else 1


def _count(args: argparse.Namespace) -> int:
    counter = load_counter(args.props, args.tokenizer)
    record = json.loads(args.body.read_text())
    body = record.get("body", record)
    parts = counter.breakdown(body)
    if args.tools:
        parts["per_tool"] = dict(tool_tokens(counter, body))  # type: ignore[assignment]
    print(json.dumps(parts, indent=2))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    """Run the CLI."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--props", type=Path, required=True, help="a saved llama.cpp /props JSON")
    parser.add_argument("--tokenizer", type=Path, required=True, help="the model's tokenizer.json")
    sub = parser.add_subparsers(dest="cmd", required=True)
    cal = sub.add_parser("calibrate")
    cal.add_argument("--report", type=Path, nargs="+", required=True)
    cal.add_argument("--tolerance", type=int, default=0)
    cnt = sub.add_parser("count")
    cnt.add_argument("--body", type=Path, required=True)
    cnt.add_argument("--tools", action="store_true", help="add the count of each tool definition")
    args = parser.parse_args(argv)
    return _calibrate(args) if args.cmd == "calibrate" else _count(args)


if __name__ == "__main__":
    sys.exit(main())
