"""AST lint: flag log/bus.publish/Cypher MERGE sites missing identity kwargs.

Enforces ADR-0074 §I3 (every async boundary preserves identity) and §I5
(memory writes carry origination). Pulled forward from FRE-376 Phase 5 as the
definition-of-done for Phase 3.

Usage:
    uv run python scripts/check_identity_threaded.py src/personal_agent/
    uv run python scripts/check_identity_threaded.py --strict src/personal_agent/

Exit code is non-zero if any non-allowlisted violation is found.
"""

from __future__ import annotations

import argparse
import ast
import re
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml  # type: ignore[import-untyped]

LOG_METHODS = {"info", "debug", "warning", "error", "exception", "critical"}
REQUIRED_LOG_KWARGS = frozenset({"trace_id"})
REQUIRED_BUS_KWARGS = frozenset({"trace_id", "session_id"})
ORIGIN_NODE_LABELS = ("Turn", "Entity", "Relationship", "DescriptionVersion")
ORIGIN_PROPS = ("originating_trace_id", "originating_session_id")


@dataclass(frozen=True)
class Violation:
    """A single identity-threading violation flagged by the lint.

    Attributes:
        path: Path of the offending source file.
        line: 1-based line number of the offending AST node.
        kind: One of `log_missing_trace_id`, `bus_publish_missing_identity`,
            `cypher_merge_missing_origination`.
        detail: Short excerpt or hint to help identify the site (≤80 chars).
    """

    path: Path
    line: int
    kind: str
    detail: str


def _kwarg_names(call: ast.Call) -> set[str]:
    """Collect kwarg names and dict-literal-keys-as-kwargs for log calls.

    Returns the sentinel ``{"<spread>"}`` if ``**kwargs`` is spread and the
    static analysis cannot prove identity is set.
    """
    names: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None:
            return {"<spread>"}
        names.add(kw.arg)
    # accept identity present in a dict-literal first positional arg (rare for log.*)
    if call.args and isinstance(call.args[0], ast.Dict):
        for k in call.args[0].keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                names.add(k.value)
    return names


def _is_log_call(call: ast.Call) -> bool:
    """log.info(...) — receiver identifier must be literally ``log`` (convention)."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in LOG_METHODS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id == "log"
    )


def _is_bus_publish(call: ast.Call) -> bool:
    """Match any ``*.publish`` attribute call.

    Covers ``bus.publish``, ``self._bus.publish``, ``self._event_bus.publish``,
    ``get_event_bus().publish(...)``. False-positive surface (e.g. ``ws.publish``)
    is acceptable within ``src/personal_agent/`` — the only ``.publish(`` callers
    there are the event bus. Add explicit allowlist entries if that ever changes.
    """
    return isinstance(call.func, ast.Attribute) and call.func.attr == "publish"


def _is_typed_event_constructor(call: ast.Call) -> bool:
    """Whether ``call`` constructs a typed ``Event`` Pydantic model.

    Matches any ``XxxEvent(...)`` call by name suffix. The substrate
    (``personal_agent.events.models``) makes ``trace_id`` / ``session_id``
    mandatory at the type level for request-scoped event subclasses and
    explicitly ``Optional[None]`` for system-scoped events (``MetricsSampled``,
    ``ModeTransition``, ``ErrorPatternDetected``). Both are acceptable: the
    Pydantic constructor enforces the §I3 contract at runtime, so a static
    lint can trust the type.
    """
    if isinstance(call.func, ast.Name):
        return call.func.id.endswith("Event")
    if isinstance(call.func, ast.Attribute):
        return call.func.attr.endswith("Event")
    return False


def _annotation_is_event(node: ast.expr | None) -> bool:
    """Whether a type annotation ends with ``Event`` (e.g. ``MetricsSampledEvent``)."""
    if isinstance(node, ast.Name):
        return node.id.endswith("Event")
    if isinstance(node, ast.Attribute):
        return node.attr.endswith("Event")
    return False


def _name_resolves_to_event(tree: ast.AST, var_name: str, before_lineno: int) -> bool:
    """Whether ``var_name`` resolves to a typed Event payload.

    Two signals are accepted:
        1. A local assignment ``var_name = XxxEvent(...)`` earlier in the file.
        2. A function parameter ``var_name: XxxEvent`` on any enclosing or
           sibling ``def``/``async def`` in the file (best-effort — we don't
           track which function each ``bus.publish`` is actually in, only that
           SOME function in the same file declares the name with an Event
           annotation).

    Best-effort intra-procedural lookup — ignores scopes and conditional
    control flow (acceptable for the simple patterns in ``src/personal_agent/``).
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and node.lineno < before_lineno:
            if isinstance(node.value, ast.Call) and _is_typed_event_constructor(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == var_name:
                        return True
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in node.args.args + node.args.kwonlyargs:
                if arg.arg == var_name and _annotation_is_event(arg.annotation):
                    return True
    return False


def _bus_publish_identity_kwargs(call: ast.Call, tree: ast.AST | None = None) -> set[str]:
    """Identity field names visible on a ``bus.publish`` call.

    Positional signature is ``bus.publish(stream, payload)`` — the payload
    is the SECOND positional argument, not the first. Also accept explicit
    kwargs.

    Accepts three payload shapes:
        1. Inline dict literal with ``trace_id``/``session_id`` keys.
        2. Inline typed event constructor (``XxxEvent(...)``).
        3. ``Name`` reference to a variable assigned from a typed event
           constructor earlier in the file.

    Returns ``{"<spread>"}`` when ``**kwargs`` is spread and identity can't be
    proven statically. Returns ``{"<opaque-var>"}`` (plus any kwarg names) when
    the payload is an unrecognized expression.
    """
    names: set[str] = set()
    for kw in call.keywords:
        if kw.arg is None:
            return {"<spread>"}
        names.add(kw.arg)
    payload_arg = call.args[1] if len(call.args) >= 2 else None
    if isinstance(payload_arg, ast.Dict):
        for k in payload_arg.keys:
            if isinstance(k, ast.Constant) and isinstance(k.value, str):
                names.add(k.value)
    elif isinstance(payload_arg, ast.Call) and _is_typed_event_constructor(payload_arg):
        names.update({"trace_id", "session_id"})
    elif isinstance(payload_arg, ast.Name) and tree is not None:
        if _name_resolves_to_event(tree, payload_arg.id, call.lineno):
            names.update({"trace_id", "session_id"})
        else:
            names.add("<opaque-var>")
    elif payload_arg is not None:
        names.add("<opaque-var>")
    return names


_MERGE_RE = re.compile(r"MERGE\s*\(\s*\w+\s*:\s*(" + "|".join(ORIGIN_NODE_LABELS) + r")\b")


def _string_chunks(node: ast.AST) -> Iterable[str]:
    """Yield every str chunk reachable from ``node``.

    Covers dynamic Cypher built via:
      * ``+ "..." +`` BinOp concat
      * f-strings (``ast.JoinedStr``)
      * ``"sep".join([...])`` method calls over a list/tuple literal
    """
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        yield node.value
    elif isinstance(node, ast.JoinedStr):
        for part in node.values:
            if isinstance(part, ast.Constant) and isinstance(part.value, str):
                yield part.value
    elif isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        yield from _string_chunks(node.left)
        yield from _string_chunks(node.right)
    elif (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "join"
        and isinstance(node.func.value, ast.Constant)
        and isinstance(node.func.value.value, str)
    ):
        sep = node.func.value.value
        if node.args and isinstance(node.args[0], (ast.List, ast.Tuple)):
            elements: list[str] = []
            for elt in node.args[0].elts:
                elements.extend(_string_chunks(elt))
            yield sep.join(elements)


def _cypher_violations_for_query(text: str, lineno: int, path: Path) -> list[Violation]:
    """Return a violation if ``text`` contains a MERGE on a §I5 node label without origination."""
    if not _MERGE_RE.search(text):
        return []
    if all(prop in text for prop in ORIGIN_PROPS):
        return []
    return [
        Violation(path, lineno, "cypher_merge_missing_origination", text[:80].replace("\n", " "))
    ]


def lint_file(path: Path, allowlist: Iterable[dict[str, object]]) -> list[Violation]:
    """Lint a single Python source file and return all unsuppressed violations.

    Args:
        path: Path to the ``.py`` source file.
        allowlist: Iterable of dicts with at least ``path`` (str) and ``line`` (int)
            keys. Entries whose ``(path, line)`` matches a violation suppress it.

    Returns:
        List of :class:`Violation` instances. Empty list means clean.
    """
    src = path.read_text()
    tree = ast.parse(src)
    violations: list[Violation] = []

    visited_binops: set[int] = set()

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_log_call(node):
                kwargs = _kwarg_names(node)
                if "<spread>" not in kwargs and not REQUIRED_LOG_KWARGS.issubset(kwargs):
                    violations.append(Violation(path, node.lineno, "log_missing_trace_id", ""))
            elif _is_bus_publish(node):
                kwargs = _bus_publish_identity_kwargs(node, tree)
                if "<spread>" not in kwargs and not REQUIRED_BUS_KWARGS.issubset(kwargs):
                    violations.append(
                        Violation(path, node.lineno, "bus_publish_missing_identity", "")
                    )
        elif isinstance(node, ast.Constant) and isinstance(node.value, str):
            violations.extend(_cypher_violations_for_query(node.value, node.lineno, path))
        elif (
            isinstance(node, ast.BinOp)
            and isinstance(node.op, ast.Add)
            and id(node) not in visited_binops
        ):
            visited_binops.add(id(node))
            joined = "".join(_string_chunks(node))
            if joined:
                violations.extend(_cypher_violations_for_query(joined, node.lineno, path))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join"
        ):
            joined = "".join(_string_chunks(node))
            if joined:
                violations.extend(_cypher_violations_for_query(joined, node.lineno, path))

    allow = {(item["path"], item["line"]) for item in allowlist}
    return [v for v in violations if (str(v.path), v.line) not in allow]


def main() -> int:
    """CLI entrypoint. Returns non-zero if any violation is found."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument(
        "--allowlist",
        type=Path,
        default=Path("scripts/identity_threading_allowlist.yaml"),
    )
    ap.add_argument("--strict", action="store_true", help="ignore allowlist")
    args = ap.parse_args()

    allowlist: list[dict[str, object]] = []
    if not args.strict and args.allowlist.exists():
        allowlist = yaml.safe_load(args.allowlist.read_text()) or []

    total: list[Violation] = []
    for root in args.paths:
        files = [root] if root.is_file() else list(root.rglob("*.py"))
        for f in files:
            total.extend(lint_file(f, allowlist))

    for v in total:
        print(f"{v.path}:{v.line}: {v.kind} {v.detail}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
