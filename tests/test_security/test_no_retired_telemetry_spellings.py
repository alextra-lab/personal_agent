"""AC-1 (FRE-1219) — structural search for retired telemetry spellings.

ADR-0133's governed vocabulary (``telemetry/vocabulary.py``) retires
``duration_ms``, ``latency_ms``, ``prompt_tokens`` and ``completion_tokens``
(among 7 other names already clean per the ticket's own measurement) from the
``agent-logs`` write path. Two independent mechanisms prove no live call site
still emits one of the four this ticket targets, both verified against
*seeded* fixtures, never only against the (already clean) real tree — a
vacuous rule and a clean tree are indistinguishable by inspection alone,
which is exactly how the first version of the ast-grep rule below shipped
under-matching (master gate finding, FRE-1219 PR #904 round 1): every
pattern's trailing ``$$$`` failed to match zero args in argument position, so
the rule only caught the retired kwarg when another argument followed it —
missing the common real shape (kwarg last).

1. A plain ``ast`` scan (``_find_violations``) — direct kwarg or a dict
   literal later ``**``-unpacked into the call (the shape
   ``llm_client/telemetry.py``'s ``emit_model_call_started``/
   ``emit_model_call_completed`` already use for their clean payloads).
2. The ast-grep rule (``.ast-grep/rules/no-retired-telemetry-spellings.yml``),
   now a ``kind: keyword_argument`` match scoped ``inside`` a ``log.*``/
   ``logger.*`` call's argument list — matches regardless of the retired
   kwarg's position — invoked here via subprocess and checked against the
   same seeded cases as mechanism 1, so a regression in the rule's own
   pattern is caught by this suite, not just inferred from a clean tree.

Both mechanisms derive their name list from
``personal_agent.telemetry.vocabulary.RETIRED_SPELLINGS``. The vocabulary is
the authority: ``TestVocabularyIsTheAuthority`` scans ``src/`` for *all
eleven* retired spellings (not just this ticket's four) and asserts the
"overflow" — an *undocumented* violation for a name outside ``FRE_1219_NAMES``
— is empty, so a future spelling gaining a live emit site is caught even
though this ticket's own scope is only 4 names (literal equality against the
full 11-name vocabulary would be simply wrong, per master gate finding #2).
Running that scan surfaced two real findings outside this ticket's scope —
see ``_KNOWN_OUT_OF_SCOPE_OVERFLOW_REASONS`` — both reported rather than
fixed here or silently dropped from the check.
"""

from __future__ import annotations

import ast
import subprocess
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


def _find_violations(tree: ast.Module, names: frozenset[str] = FRE_1219_NAMES) -> list[str]:
    """Find retired-spelling kwargs at log calls, direct or via `**`-unpacked dict.

    Direct case: ``log.info(..., duration_ms=x, ...)`` — a literal keyword
    argument named one of ``names``.

    Dict-unpack case (best-effort, module-scoped, not full dataflow): for
    every ``log.<method>(..., **var, ...)`` call, look for a same-module
    assignment ``var = {...}`` (a plain dict literal) and check its string
    keys. Sufficient as a regression guard for the shape this exists to
    catch — a payload dict built a few lines above the call it's unpacked
    into.

    Args:
        tree: Parsed module to scan.
        names: Retired spellings to check for. Defaults to this ticket's
            four; ``TestVocabularyIsTheAuthority`` passes the full
            eleven-name vocabulary instead.

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
                if kw.arg in names:
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
                if isinstance(key_node, ast.Constant) and key_node.value in names:
                    violations.append(
                        f"line {node.lineno}: {func.value.id}.{func.attr}"  # type: ignore[union-attr]
                        f"(**{kw.value.id}) where {kw.value.id!r} carries "
                        f"retired key {key_node.value!r}"
                    )
    return violations


def _scan_tree(root: Path, names: frozenset[str] = FRE_1219_NAMES) -> list[str]:
    violations: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for v in _find_violations(tree, names):
            violations.append(f"{path.relative_to(REPO_ROOT)} — {v}")
    return violations


def _run_ast_grep(target: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["ast-grep", "scan", "--rule", str(RULE_PATH), str(target)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


class TestRealTreeIsClean:
    def test_no_retired_spelling_emit_sites_in_src(self) -> None:
        violations = _scan_tree(SRC)
        assert not violations, "\n".join(violations)

    def test_ast_grep_rule_finds_nothing_in_src(self) -> None:
        result = _run_ast_grep(SRC)
        assert result.returncode == 0, result.stdout + result.stderr


#: Structural hits outside FRE_1219_NAMES that this scan finds and are NOT this
#: ticket's job to fix. Each entry names a reason — an exclusion without one is
#: a defect, not a configuration (mirrors vocabulary.py's own
#: NEAR_MISS_EXCEPTIONS/FIELD_EXCLUSIONS convention). Both entries are reported
#: to master rather than silently fixed or silently ignored — this dict is what
#: makes that reporting durable instead of a one-time comment.
#:
#: "event" (5 sites, es_logger.py) — log.warning/log.error calls pass a
#: positional message string AND event=event_type as an explicit kwarg.
#: structlog's BoundLogger reserves "event" as the message parameter's own
#: name, so this is a Python call-signature collision (TypeError: got multiple
#: values for argument 'event'), not a document-field emission — it crashes
#: before validate_document() is ever reached, so it structurally matches
#: Rule 1's shape without being able to violate it in practice, for ANY
#: log.<method>(msg, event=..., ...) call, not just these 5 (a real "event=X"
#: with no positional message would bind fine, becoming the intended
#: structlog message name — never a leftover document field either way).
#: Pre-existing, unrelated to telemetry vocabulary; found during FRE-1219
#: (PR #904) and filed to Backlog by master rather than fixed here.
#:
#: "timestamp" (1 site, telemetry/metrics.py:217, "invalid_timestamp" warning)
#: — a genuine, ordinary, currently-live kwarg (no crash, no reserved-name
#: collision) that would violate Rule 1 if it reached validate_document(). A
#: real finding, not a false positive — surfaced by this same scan, on a
#: rarely-exercised path (a malformed-timestamp parse failure), which is
#: presumably why the ticket's own 24h production measurement showed zero
#: "timestamp" occurrences. Deliberately not fixed here: different name,
#: outside FRE-1219's 4-name scope, same ask-before-expanding-scope
#: discipline this ticket already applied to the Grafana dashboards — flagged
#: to master for a scope decision (most likely another Backlog ticket)
#: instead of being fixed unilaterally or silently dropped from this check.
_KNOWN_OUT_OF_SCOPE_OVERFLOW_REASONS: dict[str, str] = {
    "event": "es_logger.py's event=event_type collides with structlog's reserved "
    "'event' message parameter and crashes before any document is assembled — "
    "pre-existing bug, filed to Backlog separately, not a real emit site",
    "timestamp": "telemetry/metrics.py:217's invalid_timestamp warning emits a genuine, "
    "currently-live 'timestamp' kwarg — a real violation, but for a name outside "
    "FRE-1219's 4-name scope; flagged to master rather than fixed here",
}


class TestVocabularyIsTheAuthority:
    def test_no_retired_spelling_outside_this_tickets_scope_has_a_live_emit_site(self) -> None:
        """Scans src for all 11 RETIRED_SPELLINGS, not just this ticket's 4.

        The property: any live-emit-site violation found must be one of the
        four names FRE_1219_NAMES already tracks, or a documented exception
        above — the "overflow" (an *undocumented* violation for a name
        outside both sets) must be empty. This is what makes the vocabulary
        the authority per AC-1's own text ("cannot drift from the governed
        vocabulary"): if one of the other retired names (or a name added to
        RETIRED_SPELLINGS later) gains a *new, undocumented* live emit site,
        this test catches it even though FRE_1219_NAMES itself is unchanged.

        Literal equality against the full vocabulary is not the right check
        here (master gate finding #2) — RETIRED_SPELLINGS has 11 entries,
        FRE_1219_NAMES has 4, and the other seven are legitimately out of
        THIS ticket's scope (already clean per the ticket's own 24h
        measurement, not this ticket's job to re-verify their absence —
        that's ADR-0133's own ongoing job).

        Running this scan surfaced one genuinely new, real finding beyond the
        known/excluded "event" collision: telemetry/metrics.py:217 emits an
        ordinary "timestamp" kwarg on its "invalid_timestamp" warning (a
        rarely-exercised path, which is why the ticket's own measurement
        window missed it) — a real, currently-live vocabulary violation for a
        name outside FRE_1219_NAMES. Deliberately not fixed here (different
        name, different scope, same "ask before expanding scope" discipline
        this ticket already applied to the Grafana dashboards) — flagged to
        master instead of silently excluded, unlike the "event" case, which
        earns its exclusion because it can never actually violate anything.
        """
        all_names = frozenset(RETIRED_SPELLINGS.keys())
        violations = _scan_tree(SRC, names=all_names)
        overflow = [
            v
            for v in violations
            if not any(name in v for name in FRE_1219_NAMES)
            and not any(name in v for name in _KNOWN_OUT_OF_SCOPE_OVERFLOW_REASONS)
        ]
        assert not overflow, "\n".join(overflow)

    def test_rule_names_are_present_in_the_ast_grep_rule_text(self) -> None:
        rule_text = RULE_PATH.read_text()
        for name in FRE_1219_NAMES:
            assert name in rule_text, f"{name!r} missing from the ast-grep rule"


class TestSeededViolationFires:
    """Seeded, never inferred from the (already clean) real tree.

    The 6-case table is master gate's own verification set (FRE-1219 PR #904
    round 1) — 4 true positives (2 of which are the retired kwarg in
    *trailing* position, the shape the first ast-grep rule version missed)
    and 2 true negatives.
    """

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


class TestSeededViolationFiresAstGrep:
    """Master gate's exact 6-case verification set, run against the real
    ast-grep rule (not the independent Python scanner above) — this is what
    the first version of the rule silently failed: it executed, reported
    green, and missed 2 of 4 true positives (both trailing-kwarg-position
    cases) because its trailing ``$$$`` did not match zero args.
    """

    def _fixture(self, tmp_path: Path, source: str) -> Path:
        fixture = tmp_path / "seeded.py"
        fixture.write_text(source)
        return fixture

    def test_retired_kwarg_last_is_caught_log(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'log.info("e", duration_ms=5)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode != 0, result.stdout + result.stderr

    def test_retired_kwarg_last_is_caught_logger(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'logger.warning("e", latency_ms=2)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode != 0, result.stdout + result.stderr

    def test_retired_kwarg_surrounded_is_caught(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'log.info("e", foo=1, duration_ms=5, bar=2)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode != 0, result.stdout + result.stderr

    def test_retired_kwarg_followed_by_another_is_caught(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'log.debug("e", prompt_tokens=1, completion_tokens=2)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode != 0, result.stdout + result.stderr

    def test_canonical_names_are_not_flagged(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'log.info("e", input_tokens=5, output_tokens=6)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode == 0, result.stdout + result.stderr

    def test_unrelated_kwarg_is_not_flagged(self, tmp_path: Path) -> None:
        fixture = self._fixture(tmp_path, 'log.info("e", cost_usd=0.1)\n')
        result = _run_ast_grep(fixture)
        assert result.returncode == 0, result.stdout + result.stderr
