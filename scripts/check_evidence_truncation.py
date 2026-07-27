"""AST lint: flag silent truncation of evidence-path content (ADR-0125 D5, AC-5).

D5 forbids silent truncation on any path feeding a durable artifact or assembled
context: content must be stored whole, or shortened with an explicit marker
recording that it was shortened and by how much
(``personal_agent.captains_log.turn_evidence.mark_truncated``).

The boundary is documented in
``docs/superpowers/plans/2026-07-27-fre-1002-evidence-path-boundary-guard.md``.
It is expressed here as content **identifiers**, not as a fixed list of
``(file, function)`` anchors — a codex plan review of the first draft found that
an anchor list silently misses a shortening site introduced in a different
function or file, which is exactly the failure AC-5 names ("if the
evidence-path boundary is left undefined so the guard's scope is
unverifiable"). Matching by identifier instead of location means a new site is
caught wherever it appears, without updating this file.

Three rules, applied tree-wide over the target path(s):

  * Rule A — a shortening shape targets an ``ast.Name``, an ``ast.Attribute``,
    a dict key, or a keyword-argument name in ``EVIDENCE_CONTENT_NAMES``.
  * Rule B — a shortening shape targets the result of ``X.get(K, ...)`` where
    ``K`` is a string literal in ``EVIDENCE_CONTENT_NAMES``. Needed for sites
    with no named variable, e.g. ``mem.get("summary", ...)[:150]``.
  * Rule C — whole-file scope for ``request_gateway/state_document.py`` only,
    audited individually: every shortening shape in that file feeds the
    assembled state document, and several do so through generically-named
    locals (``line``, ``first_line``) that no name-based rule would catch.

Four exclusions prevent false positives verified against real call sites:

  * Log-call exclusion — a match feeding a ``log.*``/``logger.*`` call
    (diagnostic telemetry, not a durable artifact or assembled context) is
    exempt.
  * Marker exemption — a match wrapped in a resolved import of
    ``personal_agent.captains_log.turn_evidence.mark_truncated`` is exempt.
    Resolved by import binding, not bare callee name, so a shadowed or
    unrelated ``mark_truncated`` cannot suppress a real violation.
  * Cosmetic-label exclusion — an assignment to a bare ``title`` target is
    exempt (see :data:`_COSMETIC_LABEL_TARGETS`).
  * Directory exclusion — ``ui/`` is skipped entirely (see
    :data:`EXCLUDED_DIR_MARKERS`).

Known limitation, not a live gap: Rule A matches ``ast.Name``,
``ast.Attribute``, dict-literal keys, and keyword-argument names, plus Rule
B's ``.get(key, ...)`` chains — it does not inspect a bare dict-subscript
target (``data["summary"][:200]``, no ``.get()``). No such site currently
exists in ``src/personal_agent/`` (verified by the real-tree scan tests), so
this is a documented design boundary rather than a missed detection; extend
Rule A to ``ast.Subscript`` targets with a string-constant key if one appears.

Usage:
    uv run python scripts/check_evidence_truncation.py src/personal_agent/
    uv run python scripts/check_evidence_truncation.py --strict src/personal_agent/

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

EVIDENCE_CONTENT_NAMES = frozenset(
    {
        "user_message",
        "assistant_response",
        "summary",
        "content",
        "last_error",
        "error_summary",
        "matching_sentence",
        "stub_summary",
    }
)
"""Identifiers that name evidence-path content: a D3 record's value (user
message, assistant response, tool/reflection error text) or the text field of
an assembled-context item. Matched against a Name or an Attribute's ``.attr``
— never against a file or function location, so relocation cannot silently
defeat the guard."""

DICT_KEY_CONTENT_NAMES = EVIDENCE_CONTENT_NAMES - {"content"}
"""Dict-literal-key and keyword-argument match set — deliberately narrower
than :data:`EVIDENCE_CONTENT_NAMES`. ``"content"`` is too generic a *key name*
for arbitrary API-response-shaped dicts: ``tools/web.py`` builds
``{"content": ib.get("content", "")[:500], ...}`` for an unrelated SearXNG
infobox field that merely shares the key name, verified by reading the site
directly. As a bare Python *variable* (``content = msg.get(...)`` then
``content[:200]``), ``content`` is precise enough — that match still goes
through :data:`EVIDENCE_CONTENT_NAMES` via Rule A's Name check."""

GET_CHAIN_KEYS = frozenset({"summary", "user_message"})
"""Rule B's key set — deliberately narrower than :data:`EVIDENCE_CONTENT_NAMES`.
A ``.get(key, ...)`` chain only has these two keys verified against a real
assembled-context item shape (``ep.get("summary")``,
``mem.get("summary", mem.get("user_message", ""))``)."""

LOG_METHODS = frozenset({"info", "debug", "warning", "error", "exception", "critical"})
LOG_RECEIVERS = frozenset({"log", "logger"})

_COSMETIC_LABEL_TARGETS = frozenset({"title"})
"""Assignment targets that are a short display label by contract, not the
evidence record — e.g. ``title = f"Task: {user_message[:50]}..."`` building
``CaptainLogEntry.title`` (``models.py``: "Short, actionable title"). The full
``user_message`` remains reachable via the entry's ``trace_id`` join to the
untouched ``TaskCapture``, so shortening it here loses no evidence."""

MARKER_MODULE = "personal_agent.captains_log.turn_evidence"
MARKER_FUNC = "mark_truncated"

_TRUNCATE_HELPER_RE = re.compile(r"truncat|clip|excerpt", re.IGNORECASE)

_MIN_CONTENT_SLICE_BOUND = 50
"""Slices with a smaller constant upper bound are item-count caps
(``entity_names[:5]``, ``errors[:3]``), not character truncation — verified by
grepping every ``[:N]`` across the evidence-boundary files: observed list caps
are <= 15, observed character-truncation idioms are >= 80, so any threshold in
that gap is safe. 50 leaves margin on both sides."""

EXCLUDED_DIR_MARKERS = ("/personal_agent/ui/",)
"""Directories excluded from the scan entirely: the CLI/terminal presentation
layer (``ui/memory_cli.py`` formats Rich table columns for a human operator,
e.g. ``(t.summary or t.assistant_response or "")[:300]``) is neither a durable
artifact nor assembled context — D5's boundary — it's a display concern,
verified by reading the site directly."""

WHOLE_FILE_SCOPE_SUFFIXES = ("request_gateway/state_document.py",)
"""Files audited individually where every shortening shape in the file is a
candidate violation, not just ones matching Rule A/B. state_document.py's
``_extract_constraints``/``_extract_recent_actions``/``_extract_open_questions``
clip through generically-named locals (``line``, ``first_line``) that no
name-based rule catches; grepping every ``[:N]`` in the file confirmed none
feed a log call or a query parameter, so whole-file scope here has no
false-positive risk. This is a one-file special case, not a general rule for
small files."""


@dataclass(frozen=True)
class Violation:
    """A single silent-truncation violation flagged by the lint.

    Attributes:
        path: Path of the offending source file.
        line: 1-based line number of the offending AST node.
        kind: One of `bare_slice`, `helper_clip`, `byte_limit`.
        detail: Short excerpt or hint to help identify the site (<=80 chars).
    """

    path: Path
    line: int
    kind: str
    detail: str


def _is_log_call(call: ast.Call) -> bool:
    """``log.info(...)`` / ``logger.warning(...)`` — diagnostic, not evidence."""
    return (
        isinstance(call.func, ast.Attribute)
        and call.func.attr in LOG_METHODS
        and isinstance(call.func.value, ast.Name)
        and call.func.value.id in LOG_RECEIVERS
    )


def _marker_import_aliases(tree: ast.AST) -> set[str]:
    """Local names bound to the sanctioned ``mark_truncated``.

    Resolves ``from personal_agent.captains_log.turn_evidence import
    mark_truncated`` (optionally ``as x``) and
    ``personal_agent.captains_log.turn_evidence.mark_truncated`` module-qualified
    access, so a shadowed local function of the same name is never trusted.
    """
    aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == MARKER_MODULE:
            for alias in node.names:
                if alias.name == MARKER_FUNC:
                    aliases.add(alias.asname or alias.name)
    return aliases


def _is_marker_call(node: ast.AST, marker_aliases: set[str]) -> bool:
    """Whether ``node`` is a call to the resolved ``mark_truncated`` binding."""
    if not isinstance(node, ast.Call):
        return False
    func = node.func
    if isinstance(func, ast.Name):
        return func.id in marker_aliases
    if isinstance(func, ast.Attribute):
        # module-qualified: turn_evidence.mark_truncated(...)
        return func.attr == MARKER_FUNC
    return False


def _enclosing_log_call(node: ast.AST, ancestors: list[ast.AST]) -> bool:
    """Whether ``node`` sits inside a keyword argument of a recognized log call."""
    for ancestor in reversed(ancestors):
        if isinstance(ancestor, ast.Call) and _is_log_call(ancestor):
            return True
    return False


def _enclosing_cosmetic_label_assign(ancestors: list[ast.AST]) -> bool:
    """Whether ``node`` sits inside an assignment to a short-label target.

    E.g. ``title = f"Task: {user_message[:50]}..."`` — see
    :data:`_COSMETIC_LABEL_TARGETS`.
    """
    for ancestor in reversed(ancestors):
        if isinstance(ancestor, ast.Assign):
            return any(
                isinstance(t, ast.Name) and t.id in _COSMETIC_LABEL_TARGETS
                for t in ancestor.targets
            )
    return False


def _shortening_kind(node: ast.expr) -> str | None:
    """Classify ``node`` as one of the three shortening shapes, or ``None``.

    * ``bare_slice`` — ``x[:200]`` / ``x[0:200]`` where the upper bound is a
      constant int >= :data:`_MIN_CONTENT_SLICE_BOUND`.
    * ``byte_limit`` — the same slice shape chained off ``.encode(...)``.
    * ``helper_clip`` — a call whose name matches a truncate-like heuristic.
    """
    if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Slice):
        upper = node.slice.upper
        if upper is None:
            return None  # no upper bound (`x[5:]`, `x[-3:]`): not a truncation
        if not (isinstance(upper, ast.Constant) and isinstance(upper.value, int)):
            return None  # non-constant bound: can't classify, don't guess
        if upper.value < _MIN_CONTENT_SLICE_BOUND:
            return None  # item-count cap (`x[:5]`), not character truncation
        base = node.value
        if (
            isinstance(base, ast.Call)
            and isinstance(base.func, ast.Attribute)
            and base.func.attr == "encode"
        ):
            return "byte_limit"
        return "bare_slice"
    if isinstance(node, ast.Call):
        name = None
        if isinstance(node.func, ast.Name):
            name = node.func.id
        elif isinstance(node.func, ast.Attribute):
            name = node.func.attr
        if name and _TRUNCATE_HELPER_RE.search(name):
            return "helper_clip"
    return None


_UNWRAP_METHODS = frozenset({"strip", "rstrip", "lstrip", "encode"})


def _unwrap(node: ast.expr) -> ast.expr:
    """Peel ``.strip()``/``.rstrip()``/``.lstrip()``/``.encode(...)`` calls.

    Reaches the underlying expression so the byte-limit shape
    (``content.encode("utf-8")[:200]``) matches on ``content`` rather than on
    the intermediate ``.encode()`` call.
    """
    while isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
        if node.func.attr not in _UNWRAP_METHODS:
            break
        node = node.func.value
    return node


class _ParentTrackingVisitor(ast.NodeVisitor):
    """Base visitor that maintains an ancestor stack for context-sensitive checks."""

    def __init__(self) -> None:
        self._ancestors: list[ast.AST] = []

    def generic_visit(self, node: ast.AST) -> None:
        self._ancestors.append(node)
        super().generic_visit(node)
        self._ancestors.pop()


class _EvidenceTruncationVisitor(_ParentTrackingVisitor):
    """Finds Rule A / Rule B violations tree-wide, or Rule C within a whole-file-scoped file."""

    def __init__(self, path: Path, marker_aliases: set[str], whole_file: bool) -> None:
        super().__init__()
        self.path = path
        self.marker_aliases = marker_aliases
        self.whole_file = whole_file
        self.violations: list[Violation] = []

    def _record(self, node: ast.expr, kind: str) -> None:
        if (
            self._enclosing_marker()
            or _enclosing_log_call(node, self._ancestors)
            or _enclosing_cosmetic_label_assign(self._ancestors)
        ):
            return
        detail = ast.unparse(node)[:80] if hasattr(ast, "unparse") else ""
        self.violations.append(Violation(self.path, node.lineno, kind, detail))

    def _enclosing_marker(self) -> bool:
        for ancestor in self._ancestors:
            if _is_marker_call(ancestor, self.marker_aliases):
                return True
        return False

    def _check_candidate(self, node: ast.expr) -> None:
        if _is_marker_call(node, self.marker_aliases):
            return  # the candidate IS the resolved marker call itself: compliant
        kind = _shortening_kind(node)
        if kind is not None:
            self._record(node, kind)

    def _matches_evidence_content(self, target: ast.expr) -> bool:
        """Rule A (Name/Attribute) and Rule B (``.get(key, ...)`` chain).

        Recurses into ``x or default`` fallback expressions (e.g.
        ``(capture.user_message or "").strip()[:200]``).
        """
        if isinstance(target, ast.BoolOp):
            return any(self._matches_evidence_content(v) for v in target.values)
        if isinstance(target, ast.Name):
            return target.id in EVIDENCE_CONTENT_NAMES
        if isinstance(target, ast.Attribute):
            return target.attr in EVIDENCE_CONTENT_NAMES
        if (
            isinstance(target, ast.Call)
            and isinstance(target.func, ast.Attribute)
            and target.func.attr == "get"
            and target.args
            and isinstance(target.args[0], ast.Constant)
            and target.args[0].value in GET_CHAIN_KEYS
        ):
            return True  # Rule B
        return False

    # Rule A / Rule B: Name / Attribute / `.get(key)` targets named as evidence content.
    def visit_Subscript(self, node: ast.Subscript) -> None:
        target = _unwrap(node.value)
        if self._matches_evidence_content(target) or self.whole_file:
            self._check_candidate(node)
        self.generic_visit(node)

    # Rule A: dict-literal keys and keyword-argument names.
    def visit_Dict(self, node: ast.Dict) -> None:
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in DICT_KEY_CONTENT_NAMES
            ):
                self._ancestors.append(node)
                self._check_candidate(value)
                self._ancestors.pop()
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        for kw in node.keywords:
            if kw.arg is not None and kw.arg in DICT_KEY_CONTENT_NAMES:
                self._ancestors.append(node)
                self._check_candidate(kw.value)
                self._ancestors.pop()
        self.generic_visit(node)


def lint_file(path: Path, allowlist: Iterable[dict[str, object]]) -> list[Violation]:
    """Lint a single Python source file and return all unsuppressed violations.

    Args:
        path: Path to the ``.py`` source file.
        allowlist: Iterable of dicts with at least ``path`` (str) and ``line``
            (int) keys. Entries whose ``(path, line)`` matches a violation
            suppress it.

    Returns:
        List of :class:`Violation` instances. Empty list means clean.
    """
    posix_path = path.as_posix()
    if any(marker in posix_path for marker in EXCLUDED_DIR_MARKERS):
        return []

    src = path.read_text()
    tree = ast.parse(src)
    marker_aliases = _marker_import_aliases(tree)
    whole_file = path.as_posix().endswith(WHOLE_FILE_SCOPE_SUFFIXES)

    visitor = _EvidenceTruncationVisitor(path, marker_aliases, whole_file)
    visitor.visit(tree)

    # visit_Call (kwarg matching) and visit_Subscript both recurse via
    # generic_visit, so a kwarg-matched slice is visited twice; dedupe by the
    # AST node's identifying triple rather than suppress either visit.
    deduped = list({(v.line, v.kind, v.detail): v for v in visitor.violations}.values())

    allow = {(item["path"], item["line"]) for item in allowlist}
    return [v for v in deduped if (str(v.path), v.line) not in allow]


def main() -> int:
    """CLI entrypoint. Returns non-zero if any violation is found."""
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument(
        "--allowlist",
        type=Path,
        default=Path("scripts/evidence_truncation_allowlist.yaml"),
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
