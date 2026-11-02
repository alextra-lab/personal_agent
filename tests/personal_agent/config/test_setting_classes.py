"""Every menu parameter has a class, and a ``next_turn`` class matches its reads (FRE-1724 AC-2)."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from personal_agent.config.setting_classes import (
    NEXT_RESTART_SETTINGS,
    NEXT_TURN_SETTINGS,
    READ_ONLY_SETTINGS,
    row_class,
    setting_class,
)
from personal_agent.config.settings import AppConfig

_SRC = Path(__file__).resolve().parents[3] / "src" / "personal_agent"
_RECEIVERS = frozenset(
    {
        "settings",
        "current_settings",
        "_cl_settings",
        "_s",
        "_cfg",
        "cfg",
        "_settings",
        "app_settings",
    }
)
_ONE_SHOT_FUNCTIONS = frozenset({"__init__", "__post_init__", "lifespan"})


def _one_shot_reads(source: str, fields: frozenset[str]) -> list[tuple[str, int]]:
    """Return ``(field, line)`` for each read at import, in ``__init__`` or in ``lifespan``.

    A read inside a lambda runs at the call, so it does not count as an import read.
    """
    tree = ast.parse(source)
    parents = {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
    found: list[tuple[str, int]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Attribute) or node.attr not in fields:
            continue
        receiver = ast.unparse(node.value)
        if receiver.split(".")[-1] not in _RECEIVERS and not receiver.endswith("get_settings()"):
            continue
        scopes: list[ast.AST] = []
        current: ast.AST = node
        while current in parents:
            current = parents[current]
            if isinstance(current, ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda):
                scopes.append(current)
        named = {s.name for s in scopes if isinstance(s, ast.FunctionDef | ast.AsyncFunctionDef)}
        if not scopes or named & _ONE_SHOT_FUNCTIONS:
            found.append((node.attr, node.lineno))
    return found


def test_every_appconfig_field_has_one_class() -> None:
    fields = set(AppConfig.model_fields)
    classes = [NEXT_TURN_SETTINGS, NEXT_RESTART_SETTINGS, frozenset(READ_ONLY_SETTINGS)]

    assert fields == set().union(*classes), "add the new setting to config/setting_classes.py"
    assert sum(len(names) for names in classes) == len(fields), "a setting has two classes"


def test_a_field_with_no_entry_raises() -> None:
    with pytest.raises(KeyError):
        setting_class("a_setting_that_the_table_does_not_name")
    with pytest.raises(KeyError):
        row_class("setting.a_setting_that_the_table_does_not_name")
    with pytest.raises(KeyError):
        row_class("a_new_family.row")


def test_a_read_only_setting_states_its_reason() -> None:
    for field in READ_ONLY_SETTINGS:
        result = setting_class(field)
        assert result.applies == "read_only"
        assert result.reason


def test_the_scan_finds_a_seeded_read_at_each_one_shot_place() -> None:
    """A seeded negative: the guard below is only as good as this scan."""
    source = """
from personal_agent.config import settings, get_settings

TIMEOUT = settings.alpha
SECOND = get_settings().bravo

class Job:
    def __init__(self):
        self.value = settings.charlie

async def lifespan(app):
    print(settings.delta)

def per_call():
    return settings.echo

DEFERRED = lambda: settings.foxtrot
"""
    fields = frozenset({"alpha", "bravo", "charlie", "delta", "echo", "foxtrot"})

    assert {field for field, _ in _one_shot_reads(source, fields)} == {
        "alpha",
        "bravo",
        "charlie",
        "delta",
    }


def test_the_scan_sees_a_known_one_shot_read_in_the_real_source() -> None:
    """A positive control on the real tree, so that an empty guard result means something."""
    source = (_SRC / "brainstem" / "consumers" / "mode_controller.py").read_text()

    assert _one_shot_reads(source, frozenset({"mode_evaluation_interval_seconds"}))


def test_a_next_turn_setting_has_no_read_at_import_in_init_or_in_lifespan() -> None:
    found: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        relative = path.relative_to(_SRC).as_posix()
        if relative in {"config/settings.py", "service/settings_menu.py"}:
            continue
        for field, line in _one_shot_reads(path.read_text(), NEXT_TURN_SETTINGS):
            found.append(f"{field}: {relative}:{line}")

    assert not found, "move these fields to NEXT_RESTART_SETTINGS:\n" + "\n".join(found)
