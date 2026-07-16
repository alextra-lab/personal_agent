r"""AST alias-aware read detection for the config-usage audit (FRE-896, ADR-0099 hygiene).

FRE-893's audit detected reads of ``AppConfig`` fields with a line-oriented ``git grep``
for ``settings.<field>`` / ``getattr(settings, "<field>")``. Three real read patterns
evade a per-line literal grep and were systematically false-flagged as dead config:

1. **Local alias** — ``cfg = settings`` / ``cfg = get_settings()`` then ``cfg.<field>``
   (e.g. the 11 ``proactive_memory_*`` fields in ``memory/proactive.py``).
2. **Factory chain** — ``get_settings().<field>`` (the grep wants ``settings.`` but the
   text is ``settings().``) (e.g. ``insights_wiring_enabled`` in
   ``events/pipeline_handlers.py``).
3. **Multi-line ``getattr``** — ``getattr(\n    settings, "<field>", ...)`` split across
   lines, so ``getattr(`` and ``settings`` never share a line (e.g. the
   ``quality_monitor_*`` fields in ``brainstem/scheduler.py``).

A codex plan-review pass on FRE-896 added a fourth, higher-stakes pattern:

4. **Self-attribute alias** — ``self._settings = config or get_settings()`` then
   ``self._settings.<field>`` (real at ``brainstem/optimizer.py``). Missing this is the
   *dangerous* direction: a field read only through an instance-attribute alias would be
   misclassified never-read and become a wrong-deletion candidate.

This module resolves all four with an AST pass instead of a regex. Granularity is
file→field (the audit only asks *which fields a file reads*), so alias resolution is a
file-level union rather than a per-scope dataflow — a conservative choice: a stray reuse
of an alias name can only *keep* a field (mark it read), never wrongly delete one. The
one exception handled explicitly is the literal name ``settings`` being *shadowed* by a
non-AppConfig binding (``settings = StudySettings()`` in ``scripts/study/sweep.py``): the
seed is withheld when the file rebinds ``settings`` to a non-settings value, so an
unrelated object's attributes are not attributed to same-named AppConfig fields.
"""

from __future__ import annotations

import ast
from collections.abc import Iterable

# The AppConfig singleton's canonical name, its factory, and its type — the three
# anchors every alias ultimately resolves back to.
_SINGLETON_NAME = "settings"
_FACTORY_NAME = "get_settings"
_APPCONFIG_TYPES: frozenset[str] = frozenset({"AppConfig"})
# Receivers whose attribute assignments may hold a settings alias (``self._settings``).
_SELF_RECEIVERS: frozenset[str] = frozenset({"self", "cls"})
# The package the singleton is imported from — ``from personal_agent.config import settings``.
_CONFIG_MODULE = "personal_agent.config"


def _is_settings_value(node: ast.expr | None) -> bool:
    """Whether an expression evaluates to the AppConfig singleton.

    Recognizes the singleton name ``settings``, a ``get_settings()`` call, and any
    ``BoolOp`` containing one (covers the ``settings or get_settings()`` idiom).
    """
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == _SINGLETON_NAME
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == _FACTORY_NAME
    if isinstance(node, ast.BoolOp):
        return any(_is_settings_value(value) for value in node.values)
    return False


def _annotation_is_appconfig(annotation: ast.expr | None) -> bool:
    """Whether a type annotation names ``AppConfig`` (bare, unioned, subscripted, or string)."""
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name):
        return annotation.id in _APPCONFIG_TYPES
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in _APPCONFIG_TYPES
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_appconfig(annotation.left) or _annotation_is_appconfig(
            annotation.right
        )
    if isinstance(annotation, ast.Subscript):  # Optional[AppConfig] / Union[..., AppConfig]
        return _annotation_is_appconfig(annotation.slice) or (
            isinstance(annotation.slice, ast.Tuple)
            and any(_annotation_is_appconfig(elt) for elt in annotation.slice.elts)
        )
    if isinstance(annotation, ast.Constant) and isinstance(annotation.value, str):
        return any(t in annotation.value for t in _APPCONFIG_TYPES)
    return False


def _function_params(node: ast.FunctionDef | ast.AsyncFunctionDef) -> Iterable[ast.arg]:
    args = node.args
    yield from args.posonlyargs
    yield from args.args
    yield from args.kwonlyargs
    if args.vararg is not None:
        yield args.vararg
    if args.kwarg is not None:
        yield args.kwarg


def _settings_is_shadowed(tree: ast.AST) -> bool:
    """Whether the file rebinds the name ``settings`` to a non-AppConfig value.

    ``settings = StudySettings()`` (``scripts/study/sweep.py``) shadows the singleton
    name; seeding ``settings`` as an alias there would wrongly attribute that unrelated
    object's attributes to same-named AppConfig fields. A rebind to a genuine settings
    value (``settings = get_settings()``) is not shadowing.
    """
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign):
            targets_settings = any(
                isinstance(t, ast.Name) and t.id == _SINGLETON_NAME for t in node.targets
            )
            if targets_settings and not _is_settings_value(node.value):
                return True
        elif isinstance(node, ast.AnnAssign):
            if (
                isinstance(node.target, ast.Name)
                and node.target.id == _SINGLETON_NAME
                and not _is_settings_value(node.value)
                and not _annotation_is_appconfig(node.annotation)
            ):
                return True
    return False


def settings_alias_names(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve the ``(name_aliases, attr_aliases)`` bound to the AppConfig singleton.

    ``name_aliases`` are plain names (locals, params, module globals) that hold the
    singleton; ``attr_aliases`` are ``self.<attr>`` / ``cls.<attr>`` attribute names that
    hold it. Both are file-level unions (see the module docstring for why per-scope
    precision is unnecessary and conservative).

    Args:
        tree: A parsed module AST.

    Returns:
        A ``(name_aliases, attr_aliases)`` pair of frozensets.
    """
    name_aliases: set[str] = set()
    attr_aliases: set[str] = set()

    if not _settings_is_shadowed(tree):
        name_aliases.add(_SINGLETON_NAME)

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            # `from personal_agent.config import settings as X` binds X to the singleton.
            if node.module == _CONFIG_MODULE:
                for alias in node.names:
                    if alias.name == _SINGLETON_NAME and alias.asname:
                        name_aliases.add(alias.asname)
        elif isinstance(node, ast.Assign):
            if _is_settings_value(node.value):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        name_aliases.add(target.id)
                    elif (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id in _SELF_RECEIVERS
                    ):
                        attr_aliases.add(target.attr)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and (
                _is_settings_value(node.value) or _annotation_is_appconfig(node.annotation)
            ):
                name_aliases.add(node.target.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in _function_params(node):
                if _annotation_is_appconfig(arg.annotation):
                    name_aliases.add(arg.arg)

    return frozenset(name_aliases), frozenset(attr_aliases)


def _value_is_settings(
    node: ast.expr, name_aliases: frozenset[str], attr_aliases: frozenset[str]
) -> bool:
    """Whether a receiver expression refers to the AppConfig singleton (name/factory/attr)."""
    if isinstance(node, ast.Name):
        return node.id in name_aliases
    if isinstance(node, ast.Call):
        return isinstance(node.func, ast.Name) and node.func.id == _FACTORY_NAME
    if isinstance(node, ast.BoolOp):
        return any(_value_is_settings(v, name_aliases, attr_aliases) for v in node.values)
    if (
        isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in _SELF_RECEIVERS
    ):
        return node.attr in attr_aliases
    return False


def collect_field_reads(tree: ast.AST, field_names: frozenset[str]) -> list[tuple[str, int]]:
    """Every ``(field, lineno)`` where an ``AppConfig`` field is read via any alias.

    Resolves attribute reads (``<settings-value>.<field>``, including the
    ``get_settings().<field>`` chain and ``self.<attr-alias>.<field>``) and
    ``getattr(<settings-value>, "<field>")`` string-literal reads. Multi-line ``getattr``
    calls resolve naturally because the AST does not care about physical line breaks.

    Args:
        tree: A parsed module AST.
        field_names: The set of ``AppConfig`` field names to look for.

    Returns:
        A list of ``(field_name, lineno)`` reads, in AST walk order.
    """
    name_aliases, attr_aliases = settings_alias_names(tree)
    reads: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in field_names:
            if _value_is_settings(node.value, name_aliases, attr_aliases):
                reads.append((node.attr, node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _value_is_settings(node.args[0], name_aliases, attr_aliases)
        ):
            key = node.args[1]
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in field_names
            ):
                reads.append((key.value, node.lineno))

    return reads
