"""AST lint: flag client-facing transport envelopes missing ``trace_id``.

Extends the ADR-0074 joinability discipline (``check_identity_threaded.py``, which
covers ``log.*`` / ``bus.publish`` / Cypher ``MERGE``) to the transport boundary
itself (FRE-427). The gap this closes: a ``{"type": "DONE"}`` WebSocket envelope
shipped without ``trace_id``, which broke the turn-rating control's join and was
invisible to every existing check — none of them look at what actually reaches
the client over the wire.

Three checks, each targeting a concretely observed or observable failure shape:

1. ``done_missing_trace_id`` — a dict literal with ``"type": "DONE"`` (the
   terminal turn envelope) must also carry a ``"trace_id"`` key.
2. ``turn_status_missing_trace_id`` — a call to ``emit_turn_status(...)`` whose
   ``value=`` argument is a dict literal must carry a ``"trace_id"`` key (the
   per-turn STATE_DELTA the client actually joins the rating control on).
3. ``adapter_drops_trace_id`` — in ``transport/agui/adapter.py``'s
   ``to_agui_event``, any ``case <EventClass>(...):`` arm whose internal event
   dataclass (from ``transport/events.py``) declares a ``trace_id`` field must
   produce an ``envelope`` dict literal that also carries ``"trace_id"`` — an
   event already carrying identity internally must not have it silently
   dropped in the wire conversion.

Suppression uses the same ``# trace-allow: <reason>`` convention as
``check_identity_threaded.py`` (ADR-0074's escape hatch) — duplicated here
rather than imported, keeping the two lint scripts independently runnable.

Usage:
    uv run python scripts/check_transport_envelope_identity.py src/personal_agent/
    uv run python scripts/check_transport_envelope_identity.py --strict src/personal_agent/

Exit code is non-zero if any unsuppressed violation is found. ``--strict`` ignores
all ``# trace-allow:`` markers, reporting the raw underlying violations.
"""

from __future__ import annotations

import argparse
import ast
import io
import re
import sys
import tokenize
from dataclasses import dataclass
from pathlib import Path

DONE_TYPE_VALUE = "DONE"
TURN_STATUS_EMITTER_NAME = "emit_turn_status"
ADAPTER_FILENAME = "adapter.py"
EVENTS_FILENAME = "events.py"
# A marker only counts with a non-empty reason after the colon.
TRACE_ALLOW_RE = re.compile(r"^trace-allow:\s*\S")


def _trace_allow_lines(src: str) -> set[int]:
    """Return the 1-based line numbers carrying a genuine ``# trace-allow: <reason>`` comment.

    Uses :mod:`tokenize` rather than a regex over raw source text so a string
    *literal* that happens to contain the marker text can never be mistaken for
    a real suppression comment (mirrors ``check_identity_threaded.py``'s
    ``_trace_allow_lines`` — duplicated rather than imported so the two lint
    scripts stay independently runnable).
    """
    return {
        tok.start[0]
        for tok in tokenize.generate_tokens(io.StringIO(src).readline)
        if tok.type == tokenize.COMMENT and TRACE_ALLOW_RE.match(tok.string.lstrip("#").strip())
    }


@dataclass(frozen=True)
class Violation:
    """A single transport-envelope identity violation.

    Attributes:
        path: Path of the offending source file.
        line: 1-based line number of the offending AST node.
        kind: One of ``done_missing_trace_id``, ``turn_status_missing_trace_id``,
            ``adapter_drops_trace_id``.
        detail: Short hint identifying the site (e.g. the event class name).
    """

    path: Path
    line: int
    kind: str
    detail: str


def _dict_keys(node: ast.Dict) -> set[str]:
    """String keys of a dict literal (non-string / starred keys are ignored)."""
    keys: set[str] = set()
    for k in node.keys:
        if isinstance(k, ast.Constant) and isinstance(k.value, str):
            keys.add(k.value)
    return keys


def _is_done_type_dict(node: ast.Dict) -> bool:
    """Whether a dict literal declares ``"type": "DONE"``."""
    for k, v in zip(node.keys, node.values, strict=False):
        if (
            isinstance(k, ast.Constant)
            and k.value == "type"
            and isinstance(v, ast.Constant)
            and v.value == DONE_TYPE_VALUE
        ):
            return True
    return False


def _check_done_literals(tree: ast.AST, path: Path) -> list[Violation]:
    """Flag any ``{"type": "DONE", ...}`` dict literal missing ``trace_id``."""
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict) and _is_done_type_dict(node):
            if "trace_id" not in _dict_keys(node):
                violations.append(Violation(path, node.lineno, "done_missing_trace_id", ""))
    return violations


def _check_turn_status_calls(tree: ast.AST, path: Path) -> list[Violation]:
    """Flag ``emit_turn_status(..., value={...})`` calls whose literal lacks ``trace_id``.

    Only literal ``value=`` dicts are checked — an opaque expression (a variable,
    a ``dict(x)`` call) is trusted, matching ``check_identity_threaded.py``'s
    treatment of non-literal payloads elsewhere in the codebase.
    """
    violations: list[Violation] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != TURN_STATUS_EMITTER_NAME:
            continue
        for kw in node.keywords:
            if kw.arg == "value" and isinstance(kw.value, ast.Dict):
                if "trace_id" not in _dict_keys(kw.value):
                    violations.append(
                        Violation(path, node.lineno, "turn_status_missing_trace_id", "")
                    )
    return violations


def lint_file(path: Path, *, strict: bool = False) -> list[Violation]:
    """Lint a single Python source file for DONE-literal and turn_status violations.

    Args:
        path: Path to the ``.py`` source file.
        strict: If ``True``, ignore ``# trace-allow:`` markers.

    Returns:
        List of :class:`Violation` instances. Empty list means clean.
    """
    src = path.read_text()
    tree = ast.parse(src)
    violations = _check_done_literals(tree, path) + _check_turn_status_calls(tree, path)
    if strict:
        return violations
    marked = _trace_allow_lines(src)
    return [v for v in violations if v.line not in marked]


def _trace_id_bearing_classes(events_tree: ast.AST) -> set[str]:
    """Names of dataclasses in ``events.py`` that declare a ``trace_id`` field."""
    classes: set[str] = set()
    for node in ast.walk(events_tree):
        if not isinstance(node, ast.ClassDef):
            continue
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                if stmt.target.id == "trace_id":
                    classes.add(node.name)
                    break
    return classes


def _match_class_name(pattern: ast.pattern) -> str | None:
    """The class name a ``case ClassName(...):`` pattern matches, if any."""
    if not isinstance(pattern, ast.MatchClass):
        return None
    cls = pattern.cls
    if isinstance(cls, ast.Name):
        return cls.id
    if isinstance(cls, ast.Attribute):
        return cls.attr
    return None


def _case_envelope_dict(case: ast.match_case) -> ast.Dict | None:
    """The dict literal assigned to ``envelope`` in a match-case body, if any."""
    for stmt in case.body:
        if (
            isinstance(stmt, ast.Assign)
            and isinstance(stmt.value, ast.Dict)
            and any(isinstance(t, ast.Name) and t.id == "envelope" for t in stmt.targets)
        ):
            return stmt.value
    return None


def lint_adapter_completeness(
    events_path: Path, adapter_path: Path, *, strict: bool = False
) -> list[Violation]:
    """Flag adapter match-arms that drop ``trace_id`` for an event that carries it.

    Args:
        events_path: Path to ``transport/events.py`` (or an equivalent module
            defining the internal event dataclasses).
        adapter_path: Path to ``transport/agui/adapter.py`` (or an equivalent
            module containing a ``match`` statement building AG-UI envelopes).
        strict: If ``True``, ignore ``# trace-allow:`` markers.

    Returns:
        List of :class:`Violation` instances. Empty list means clean.
    """
    trace_bearing = _trace_id_bearing_classes(ast.parse(events_path.read_text()))
    adapter_src = adapter_path.read_text()
    adapter_tree = ast.parse(adapter_src)

    violations: list[Violation] = []
    for node in ast.walk(adapter_tree):
        if not isinstance(node, ast.Match):
            continue
        for case in node.cases:
            cls_name = _match_class_name(case.pattern)
            if cls_name is None or cls_name not in trace_bearing:
                continue
            envelope = _case_envelope_dict(case)
            if envelope is None or "trace_id" not in _dict_keys(envelope):
                violations.append(
                    Violation(adapter_path, case.pattern.lineno, "adapter_drops_trace_id", cls_name)
                )

    if strict:
        return violations
    marked = _trace_allow_lines(adapter_src)
    return [v for v in violations if v.line not in marked]


def _lint_tree(root: Path, *, strict: bool) -> list[Violation]:
    files = [root] if root.is_file() else sorted(root.rglob("*.py"))
    violations: list[Violation] = []
    events_path = next((f for f in files if f.name == EVENTS_FILENAME), None)
    adapter_path = next((f for f in files if f.name == ADAPTER_FILENAME), None)
    for f in files:
        violations.extend(lint_file(f, strict=strict))
    if events_path is not None and adapter_path is not None:
        violations.extend(lint_adapter_completeness(events_path, adapter_path, strict=strict))
    return violations


def main() -> int:
    """CLI entrypoint. Returns non-zero if any violation is found."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--strict", action="store_true", help="ignore # trace-allow: markers")
    args = ap.parse_args()

    total: list[Violation] = []
    for root in args.paths:
        total.extend(_lint_tree(root, strict=args.strict))

    for v in total:
        print(f"{v.path}:{v.line}: {v.kind} {v.detail}")
    return 1 if total else 0


if __name__ == "__main__":
    sys.exit(main())
