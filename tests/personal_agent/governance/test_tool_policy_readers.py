"""Every ``ToolPolicy`` field has a reader in ``src/`` (FRE-1719 AC-2).

The settings menu shows every ``ToolPolicy`` field for every tool. A field that no code reads
shows a control that changes nothing (FRE-1701 applied the same rule to ``AppConfig``).

A reader is ``<name>.<field>`` or ``getattr(<name>, "<field>")`` in a production module, where
``<name>`` holds a ``ToolPolicy``. A name holds one when the module binds it from
``<expr>.tools.get(...)``, ``<expr>.tools[...]`` or a loop over ``<expr>.tools.values()`` /
``.items()``, or annotates it with ``ToolPolicy``. A direct ``<expr>.tools[...].<field>`` also
counts. A read of the same attribute name on another type does not count:
``ToolDefinition.category`` is read, and ``ToolPolicy.category`` was not.

Two modules do not count: ``governance/models.py`` defines the fields, and
``service/settings_menu.py`` only displays them.

Limits. The matcher does not see a read through an alias, ``self.<attr>``, a helper's return
value or a ``getattr`` with a variable name. Such a read makes this test fail: extend the matcher
or add an allowlist entry. The matcher is not scope-aware, and it treats any ``.tools`` mapping
as governance. These two limits can make an unread field look read.
"""

from __future__ import annotations

import ast
from functools import lru_cache
from pathlib import Path

from personal_agent.governance.models import ToolPolicy

_SRC = Path(__file__).resolve().parents[3] / "src" / "personal_agent"
_NOT_A_READER = frozenset(
    {_SRC / "governance" / "models.py", _SRC / "service" / "settings_menu.py"}
)

#: Fields allowed to have no reader, each with the reason. Keep it empty if you can.
_NO_READER: dict[str, str] = {}


def _is_tools_mapping(node: ast.expr) -> bool:
    return isinstance(node, ast.Attribute) and node.attr == "tools"


def _yields_policy(value: ast.expr) -> bool:
    """Whether ``value`` is ``<expr>.tools.get(...)`` or ``<expr>.tools[...]``."""
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Attribute):
        return value.func.attr == "get" and _is_tools_mapping(value.func.value)
    return isinstance(value, ast.Subscript) and _is_tools_mapping(value.value)


def _annotated_policy(annotation: ast.expr | None) -> bool:
    return annotation is not None and "ToolPolicy" in ast.unparse(annotation)


def _policy_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and _yields_policy(node.value):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            if _annotated_policy(node.annotation) or (
                node.value is not None and _yields_policy(node.value)
            ):
                names.add(node.target.id)
        elif isinstance(node, ast.arg) and _annotated_policy(node.annotation):
            names.add(node.arg)
        elif isinstance(node, ast.For) and isinstance(node.iter, ast.Call):
            func = node.iter.func
            if isinstance(func, ast.Attribute) and _is_tools_mapping(func.value):
                target = node.target
                if func.attr == "values" and isinstance(target, ast.Name):
                    names.add(target.id)
                elif func.attr == "items" and isinstance(target, ast.Tuple):
                    if len(target.elts) == 2 and isinstance(target.elts[1], ast.Name):
                        names.add(target.elts[1].id)
    return names


def _policy_reads(tree: ast.AST) -> set[str]:
    """The ToolPolicy field names that ``tree`` reads from a name holding a ToolPolicy."""
    holders = _policy_names(tree)
    reads: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and isinstance(node.ctx, ast.Load):
            value = node.value
            if (isinstance(value, ast.Name) and value.id in holders) or _yields_policy(value):
                reads.add(node.attr)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[0], ast.Name)
            and node.args[0].id in holders
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            reads.add(node.args[1].value)
    return reads


@lru_cache(maxsize=1)
def _src_policy_reads() -> frozenset[str]:
    reads: set[str] = set()
    for path in _SRC.rglob("*.py"):
        if path not in _NOT_A_READER:
            reads |= _policy_reads(ast.parse(path.read_text(encoding="utf-8")))
    return frozenset(reads)


def _has_reader(field: str) -> bool:
    return field in _src_policy_reads()


def test_every_tool_policy_field_has_a_reader() -> None:
    """AC-2: a ToolPolicy field with no reader in src/ fails, unless the allowlist names it."""
    unread = {name for name in ToolPolicy.model_fields if not _has_reader(name)}
    assert unread == set(_NO_READER), (
        f"no reader in src/: {sorted(unread - set(_NO_READER))}; "
        f"stale allowlist entries: {sorted(set(_NO_READER) - unread)}"
    )
    assert all(reason.strip() for reason in _NO_READER.values())


def test_has_reader_is_false_for_a_name_nothing_reads() -> None:
    """Seeded negative: a name that no module loads has no reader."""
    assert _has_reader("fre_1719_field_that_nothing_reads") is False


def test_has_reader_finds_a_known_reader() -> None:
    """Seeded positive: orchestrator/executor.py reads tool_policy.loop_max_per_signature."""
    assert _has_reader("loop_max_per_signature") is True


def test_a_read_of_the_same_name_on_another_type_is_not_a_reader() -> None:
    """``tool_def.category`` reads ToolDefinition, not ToolPolicy."""
    tree = ast.parse("def f(tool_def, registry):\n    return tool_def.category\n")
    assert _policy_reads(tree) == set()


def test_each_binding_form_is_seen() -> None:
    """Every form that binds a ToolPolicy makes its field reads count."""
    source = (
        "a = cfg.tools.get(name)\n"
        "b = cfg.tools[name]\n"
        "c: ToolPolicy = make()\n"
        "def f(d: ToolPolicy | None) -> None:\n"
        "    return d.f4\n"
        "for e in cfg.tools.values():\n"
        "    e.f5\n"
        "for _, g in cfg.tools.items():\n"
        "    g.f6\n"
        "a.f1, b.f2, c.f3, getattr(a, 'f7'), cfg.tools[name].f8\n"
    )
    assert _policy_reads(ast.parse(source)) == {"f1", "f2", "f3", "f4", "f5", "f6", "f7", "f8"}
