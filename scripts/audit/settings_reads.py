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

A codex plan-review pass and a high-effort code-review pass on FRE-896 added four more,
all higher-stakes because each is the *wrong-deletion* direction — a genuine read dropped:

4. **Self-attribute alias** — ``self._settings = config or get_settings()`` (or a plain
   DI ``self._settings = config`` where ``config: AppConfig``) then ``self._settings.<field>``
   (real at ``brainstem/optimizer.py``).
5. **Direct construction** — ``settings = AppConfig()`` / ``cfg = AppConfig()`` then
   ``cfg.<field>`` (real at ``config/config_guard.py``).
6. **Aliased factory import** — ``from personal_agent.config import get_settings as _gs``
   then ``_gs()`` / ``cfg = _gs()`` (real at ``captains_log/capture.py``).
7. **Settings-import alias** — ``from personal_agent.config import settings as X`` then
   ``X.<field>`` (real at ``orchestrator/executor.py``, 7× across ``src/``).

FRE-907 found an eighth, in the same wrong-deletion direction: the checks above compared
an annotation or a call's callee name against the literal string ``"AppConfig"``, so:

8. **AppConfig type alias** — ``from personal_agent.config.settings import AppConfig as X``
   then an ``X``-typed param (``def f(cfg: X): cfg.<field>``) or direct ``X()``
   construction — silently dropped the read regardless of the bound parameter's own name.

This module resolves all of them with an AST pass instead of a regex. Granularity is
file→field (the audit only asks *which fields a file reads*), so alias resolution is a
file-level union rather than a per-scope dataflow — a deliberate bias to the *keep*
direction: a stray reuse of an alias name can only *keep* a field (mark it read), never
wrongly delete one. For the same reason the literal name ``settings`` is **always**
seeded even in a file that rebinds it (``settings = StudySettings()``): a code-review
pass showed a file-global "shadow" suppression dropped genuine ``settings.<field>`` reads
in sibling functions of the rebinding one — over-counting a same-named non-AppConfig read
(cosmetic, and only ever a ``scripts``/``tests``-root hit, never a production ``src``
read) is the safe trade; dropping a real read is not.
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
# The modules the singleton / factory are imported from —
# ``from personal_agent.config import settings`` and ``...config.settings import get_settings``.
_CONFIG_MODULES: frozenset[str] = frozenset(
    {"personal_agent.config", "personal_agent.config.settings"}
)


def _collect_import_aliases(tree: ast.AST, target_names: frozenset[str]) -> frozenset[str]:
    """Every name that resolves to one of ``target_names`` in this module (incl. import aliases).

    Seeded with ``target_names`` themselves; extended with
    ``from personal_agent.config[.settings] import <target> as X`` alias imports, so a
    name reached only under an alias is still recognized. Shared by
    ``_collect_factory_names`` (target: ``get_settings`` — real at ``captains_log/capture.py``:
    ``import get_settings as _get_settings``) and ``_collect_appconfig_type_names`` (target:
    ``AppConfig`` — FRE-907: an aliased ``AppConfig`` import, e.g. ``import AppConfig as
    Settings``, silently dropped the read when the check compared against the literal
    string ``"AppConfig"``).
    """
    names = set(target_names)
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module in _CONFIG_MODULES:
            for alias in node.names:
                if alias.name in target_names and alias.asname:
                    names.add(alias.asname)
    return frozenset(names)


def _collect_factory_names(tree: ast.AST) -> frozenset[str]:
    """Every name that resolves to ``get_settings`` in this module (incl. import aliases)."""
    return _collect_import_aliases(tree, frozenset({_FACTORY_NAME}))


def _collect_appconfig_type_names(tree: ast.AST) -> frozenset[str]:
    """Every name that resolves to the ``AppConfig`` class in this module (incl. import aliases)."""
    return _collect_import_aliases(tree, _APPCONFIG_TYPES)


def _is_settings_value(
    node: ast.expr | None,
    factory_names: frozenset[str],
    name_aliases: frozenset[str],
    appconfig_type_names: frozenset[str],
) -> bool:
    """Whether an expression evaluates to an AppConfig instance.

    Recognizes the singleton name ``settings`` (and any already-known ``name_aliases``,
    which lets a DI param flow into a ``self._x = config`` attribute alias), a factory
    call (``get_settings()`` or an aliased factory in ``factory_names``), a direct
    ``AppConfig()`` construction (or an aliased type in ``appconfig_type_names``), and
    any ``BoolOp`` containing one of the above (covers ``config or get_settings()``).
    """
    if node is None:
        return False
    if isinstance(node, ast.Name):
        return node.id == _SINGLETON_NAME or node.id in name_aliases
    if isinstance(node, ast.Call):
        func = node.func
        if isinstance(func, ast.Name):
            return func.id in factory_names or func.id in appconfig_type_names
        if isinstance(func, ast.Attribute):
            return func.attr in factory_names or func.attr in appconfig_type_names
        return False
    if isinstance(node, ast.BoolOp):
        return any(
            _is_settings_value(v, factory_names, name_aliases, appconfig_type_names)
            for v in node.values
        )
    return False


def _annotation_is_appconfig(
    annotation: ast.expr | None, appconfig_type_names: frozenset[str]
) -> bool:
    """Whether a type annotation names ``AppConfig`` (bare, unioned, subscripted, or string).

    ``appconfig_type_names`` widens the ``Name``/``Attribute`` match to import aliases of
    ``AppConfig`` — see ``_collect_appconfig_type_names``. The string/forward-ref branch
    deliberately does NOT widen: it substring-matches, and a short alias (e.g. a
    single-letter ``import AppConfig as S``) would false-positive against any unrelated
    string annotation merely containing that letter (a code-review pass on this ticket
    found this precision regression) — safe against the canonical name (10 characters,
    low collision risk), not safe against an arbitrary alias.
    """
    if annotation is None:
        return False
    if isinstance(annotation, ast.Name):
        return annotation.id in appconfig_type_names
    if isinstance(annotation, ast.Attribute):
        return annotation.attr in appconfig_type_names
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _annotation_is_appconfig(
            annotation.left, appconfig_type_names
        ) or _annotation_is_appconfig(annotation.right, appconfig_type_names)
    if isinstance(annotation, ast.Subscript):  # Optional[AppConfig] / Union[..., AppConfig]
        return _annotation_is_appconfig(annotation.slice, appconfig_type_names) or (
            isinstance(annotation.slice, ast.Tuple)
            and any(
                _annotation_is_appconfig(elt, appconfig_type_names) for elt in annotation.slice.elts
            )
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


def settings_alias_names(
    tree: ast.AST,
    factory_names: frozenset[str] | None = None,
    appconfig_type_names: frozenset[str] | None = None,
) -> tuple[frozenset[str], frozenset[str]]:
    """Resolve the ``(name_aliases, attr_aliases)`` bound to an AppConfig instance.

    ``name_aliases`` are plain names (locals, params, module globals) that hold an
    AppConfig instance; ``attr_aliases`` are ``self.<attr>`` / ``cls.<attr>`` attribute
    names that hold one. Both are file-level unions (see the module docstring — per-scope
    precision is unnecessary and biased to the *keep* direction).

    The name ``settings`` is **always** seeded. An earlier version withheld the seed when
    the file rebound ``settings`` to a non-AppConfig value (``settings = StudySettings()``)
    to avoid cosmetic evidence pollution, but a code-review pass found that check was
    file-global: one function's rebind suppressed genuine ``settings.<field>`` reads in
    *sibling* functions — a wrong-deletion-direction false negative. Over-counting a
    same-named non-AppConfig read (cosmetic, only ever a `scripts`/`tests`-root hit, never
    a production `src` read) is the safe trade; dropping a real read is not.

    Args:
        tree: A parsed module AST.
        factory_names: Names resolving to ``get_settings`` (computed if omitted).
        appconfig_type_names: Names resolving to the ``AppConfig`` class, incl. import
            aliases (computed if omitted).

    Returns:
        A ``(name_aliases, attr_aliases)`` pair of frozensets.
    """
    factories = factory_names if factory_names is not None else _collect_factory_names(tree)
    appconfig_types = (
        appconfig_type_names
        if appconfig_type_names is not None
        else _collect_appconfig_type_names(tree)
    )
    name_aliases: set[str] = {_SINGLETON_NAME}
    assigns: list[ast.Assign | ast.AnnAssign] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in _CONFIG_MODULES:  # `import settings as X`
                for alias in node.names:
                    if alias.name == _SINGLETON_NAME and alias.asname:
                        name_aliases.add(alias.asname)
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            assigns.append(node)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in _function_params(node):
                if _annotation_is_appconfig(arg.annotation, appconfig_types):
                    name_aliases.add(arg.arg)

    # Pass 1 — name aliases from assignments, to a fixpoint so alias→alias chains
    # (`cfg = settings; alt = cfg`) resolve without ordering assumptions.
    changed = True
    while changed:
        changed = False
        for node in assigns:
            if not _is_settings_value(
                node.value, factories, frozenset(name_aliases), appconfig_types
            ):
                continue
            for name in _assign_name_targets(node):
                if name not in name_aliases:
                    name_aliases.add(name)
                    changed = True
        # AnnAssign whose annotation is AppConfig binds regardless of RHS.
        for node in assigns:
            if isinstance(node, ast.AnnAssign) and _annotation_is_appconfig(
                node.annotation, appconfig_types
            ):
                if isinstance(node.target, ast.Name) and node.target.id not in name_aliases:
                    name_aliases.add(node.target.id)
                    changed = True

    frozen_names = frozenset(name_aliases)

    # Pass 2 — self/cls attribute aliases, now that name aliases are complete (so a
    # DI ``self._settings = config`` where ``config: AppConfig`` is recognized).
    attr_aliases: set[str] = set()
    for node in assigns:
        if not isinstance(node, ast.Assign):
            continue
        if not _is_settings_value(node.value, factories, frozen_names, appconfig_types):
            continue
        for tgt in node.targets:
            if (
                isinstance(tgt, ast.Attribute)
                and isinstance(tgt.value, ast.Name)
                and tgt.value.id in _SELF_RECEIVERS
            ):
                attr_aliases.add(tgt.attr)

    return frozen_names, frozenset(attr_aliases)


def _assign_name_targets(node: ast.Assign | ast.AnnAssign) -> Iterable[str]:
    targets = node.targets if isinstance(node, ast.Assign) else [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            yield target.id


def _value_is_settings(
    node: ast.expr,
    factory_names: frozenset[str],
    name_aliases: frozenset[str],
    attr_aliases: frozenset[str],
    appconfig_type_names: frozenset[str],
) -> bool:
    """Whether a receiver expression refers to an AppConfig instance (name/factory/attr)."""
    if isinstance(node, ast.Name):
        return node.id in name_aliases
    if isinstance(node, ast.Call) or isinstance(node, ast.BoolOp):
        return _is_settings_value(node, factory_names, name_aliases, appconfig_type_names)
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
    factory_names = _collect_factory_names(tree)
    appconfig_types = _collect_appconfig_type_names(tree)
    name_aliases, attr_aliases = settings_alias_names(tree, factory_names, appconfig_types)
    reads: list[tuple[str, int]] = []

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr in field_names:
            if _value_is_settings(
                node.value, factory_names, name_aliases, attr_aliases, appconfig_types
            ):
                reads.append((node.attr, node.lineno))
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and _value_is_settings(
                node.args[0], factory_names, name_aliases, attr_aliases, appconfig_types
            )
        ):
            key = node.args[1]
            if (
                isinstance(key, ast.Constant)
                and isinstance(key.value, str)
                and key.value in field_names
            ):
                reads.append((key.value, node.lineno))

    return reads
