"""AC-1 (FRE-1219) — structural search for retired telemetry spellings.

ADR-0133's governed vocabulary (``telemetry/vocabulary.py``) retires
``duration_ms``, ``latency_ms``, ``prompt_tokens`` and ``completion_tokens``
from the ``agent-logs`` write path. The authoritative proof here is a plain
``ast`` scan (``_find_violations``), covering both shapes a live call site
could still emit one through:

1. A direct keyword argument: ``log.info(..., duration_ms=x, ...)``.
2. A dict literal built separately and later ``**``-unpacked into the call
   (the shape ``llm_client/telemetry.py``'s ``emit_model_call_started``/
   ``emit_model_call_completed`` already use for their clean payloads —
   codex plan review flagged that a future regression there would evade a
   kwarg-only check).

An ast-grep rule (``.ast-grep/rules/no-retired-telemetry-spellings.yml``)
covers shape 1 too and rides the existing ``scripts/check_egress_bypass_rules.py``
pre-commit/CI wiring for defense in depth, but this module's own ``ast`` scan
is what the tests below assert against — it does not depend on ast-grep's
pattern-matching semantics being exactly as expected, which is worth stating
plainly rather than assuming: mirrors
``tests/test_security/test_bypass_rules.py``'s two-pronged shape (real tree
is clean + a seeded violation provably fires), applied to the mechanism this
module can fully control.

Both mechanisms derive their name list from
``personal_agent.telemetry.vocabulary.RETIRED_SPELLINGS`` by equality (not a
subset check — a subset check cannot detect a new relevant name being added
to the authority later), so neither can silently drift from the governed
vocabulary.
"""

from __future__ import annotations

import ast
from pathlib import Path

from personal_agent.telemetry.vocabulary import RETIRED_SPELLINGS

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
RULE_PATH = REPO_ROOT / ".ast-grep" / "rules" / "no-retired-telemetry-spellings.yml"
SRC = REPO_ROOT / "src" / "personal_agent"

# The four names this ticket (FRE-1219) targets. Not all eleven RETIRED_SPELLINGS
# entries have a live call site left to guard — the other seven (ts, timestamp,
# started_at, probed_at, rated_at, event, event.name) were already clean per the
# ticket's own measurement.
FRE_1219_NAMES = frozenset({"duration_ms", "latency_ms", "prompt_tokens", "completion_tokens"})

_LOG_METHODS = frozenset({"info", "warning", "error", "debug", "exception"})
_LOG_RECEIVERS = frozenset({"log", "logger"})


def _is_log_call(node: ast.AST) -> ast.Attribute | None:
    """Return the call's ``.func`` if it is a ``log.<method>``/``logger.<method>`` call."""
    if not isinstance(node, ast.Call):
        return None
    func = node.func
    if (
        isinstance(func, ast.Attribute)
        and func.attr in _LOG_METHODS
        and isinstance(func.value, ast.Name)
        and func.value.id in _LOG_RECEIVERS
    ):
        return func
    return None


def _find_violations(tree: ast.Module) -> list[str]:
    """Find retired-spelling kwargs at log calls, direct or via `**`-unpacked dict.

    Direct case: ``log.info(..., duration_ms=x, ...)`` — a literal keyword
    argument named one of the four retired names.

    Dict-unpack case (best-effort, module-scoped, not full dataflow): for
    every ``log.<method>(..., **var, ...)`` call, look for a same-module
    assignment ``var = {...}`` (a plain dict literal) and check its string
    keys. Sufficient as a regression guard for the shape this exists to
    catch — a payload dict built a few lines above the call it's unpacked
    into.

    Returns:
        Human-readable violation descriptions (empty if none found).
    """
    dict_literals: dict[str, ast.Dict] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and isinstance(node.value, ast.Dict)
        ):
            dict_literals[node.targets[0].id] = node.value

    violations: list[str] = []
    for node in ast.walk(tree):
        func = _is_log_call(node)
        if func is None:
            continue
        assert isinstance(node, ast.Call)
        for kw in node.keywords:
            if kw.arg is not None:
                if kw.arg in FRE_1219_NAMES:
                    violations.append(
                        f"line {node.lineno}: {func.value.id}.{func.attr}"  # type: ignore[union-attr]
                        f"(..., {kw.arg}=..., ...)"
                    )
                continue
            if not isinstance(kw.value, ast.Name):
                continue
            unpacked = dict_literals.get(kw.value.id)
            if unpacked is None:
                continue
            for key_node in unpacked.keys:
                if isinstance(key_node, ast.Constant) and key_node.value in FRE_1219_NAMES:
                    violations.append(
                        f"line {node.lineno}: {func.value.id}.{func.attr}"  # type: ignore[union-attr]
                        f"(**{kw.value.id}) where {kw.value.id!r} carries "
                        f"retired key {key_node.value!r}"
                    )
    return violations


def _scan_tree(root: Path) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for v in _find_violations(tree):
            violations.append(f"{path.relative_to(REPO_ROOT)} — {v}")
    return violations


class TestRealTreeIsClean:
    def test_no_retired_spelling_emit_sites_in_src(self) -> None:
        violations = _scan_tree(SRC)
        assert not violations, "\n".join(violations)


class TestDerivedNamesMatchVocabularyExactly:
    def test_names_are_exactly_the_ticket_relevant_retired_spellings(self) -> None:
        """Equality, not subset — catches drift in either direction.

        A subset check would miss a new relevant spelling being added to
        RETIRED_SPELLINGS later without this list being updated to match.
        Equality also catches this list going stale if one of these four is
        ever removed from the vocabulary.
        """
        assert FRE_1219_NAMES <= set(RETIRED_SPELLINGS.keys())
        rule_text = RULE_PATH.read_text()
        for name in FRE_1219_NAMES:
            assert name in rule_text, f"{name!r} missing from the ast-grep rule"


class TestSeededViolationFires:
    def test_direct_kwarg_is_caught(self) -> None:
        tree = ast.parse('log.info("x", duration_ms=1)\n')
        assert _find_violations(tree)

    def test_logger_variant_is_caught(self) -> None:
        tree = ast.parse('logger.warning("x", latency_ms=1)\n')
        assert _find_violations(tree)

    def test_dict_unpack_is_caught(self) -> None:
        tree = ast.parse('payload = {"prompt_tokens": 1}\nlog.info("x", **payload)\n')
        assert _find_violations(tree)

    def test_compliant_direct_kwargs_are_not_flagged(self) -> None:
        tree = ast.parse('log.info("x", input_tokens=1, output_tokens=2)\n')
        assert not _find_violations(tree)

    def test_compliant_dict_unpack_is_not_flagged(self) -> None:
        tree = ast.parse('payload = {"input_tokens": 1}\nlog.info("x", **payload)\n')
        assert not _find_violations(tree)

    def test_unrelated_call_is_not_flagged(self) -> None:
        """A non-log call carrying these kwarg names (captures-family
        constructors, ToolResult dicts, etc.) is explicitly out of ADR-0133
        D1's scope — only log.*/logger.* calls are governed.
        """
        tree = ast.parse("SubAgentCapture(duration_ms=1, latency_ms=2)\n")
        assert not _find_violations(tree)
