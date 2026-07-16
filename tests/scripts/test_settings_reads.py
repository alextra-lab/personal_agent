r"""AST alias-aware settings-read resolver tests (FRE-896, ADR-0099 hygiene).

Backs FRE-896's AC1: the audit's read detection must resolve reads that reach an
``AppConfig`` field through an alias, not only literal ``settings.<field>``. FRE-893's
line-oriented ``git grep`` missed three real patterns — a local alias
(``cfg = settings; cfg.<field>``), a factory chain (``get_settings().<field>``), and a
multi-line ``getattr(settings,\\n "<field>")`` — plus a self-attribute alias
(``self._settings = get_settings(); self._settings.<field>``, real at
``brainstem/optimizer.py``) that a codex plan-review pass flagged as a wrong-deletion
risk. Each pattern gets a regression test here; the shadow-narrowing test guards against
a stray ``settings = OtherSettings()`` binding being mistaken for the AppConfig singleton.
"""

from __future__ import annotations

import ast

from scripts.audit.settings_reads import collect_field_reads, settings_alias_names

# A representative slice of real AppConfig field names used across the fixtures.
_FIELDS = frozenset(
    {
        "proactive_memory_w_embedding",
        "insights_wiring_enabled",
        "quality_monitor_daily_run_hour_utc",
        "second_brain_cpu_threshold",
        "neo4j_uri",
        "debug",
    }
)


def _reads(source: str) -> set[str]:
    return {field for field, _lineno in collect_field_reads(ast.parse(source), _FIELDS)}


def test_direct_settings_read() -> None:
    """The baseline `settings.<field>` read still resolves under the AST path."""
    source = "from personal_agent.config import settings\nx = settings.debug\n"
    assert "debug" in _reads(source)


def test_local_name_alias() -> None:
    """`cfg = settings` then `cfg.<field>` resolves (proactive_memory_* pattern)."""
    source = (
        "from personal_agent.config import settings\n"
        "def f():\n"
        "    cfg = settings\n"
        "    return cfg.proactive_memory_w_embedding\n"
    )
    assert "proactive_memory_w_embedding" in _reads(source)


def test_factory_chain_read() -> None:
    """`get_settings().<field>` resolves (insights_wiring_enabled pattern)."""
    source = (
        "from personal_agent.config.settings import get_settings\n"
        "if get_settings().insights_wiring_enabled:\n"
        "    pass\n"
    )
    assert "insights_wiring_enabled" in _reads(source)


def test_factory_assigned_alias() -> None:
    """`cfg = get_settings()` then `cfg.<field>` resolves."""
    source = (
        "from personal_agent.config.settings import get_settings\n"
        "def f():\n"
        "    cfg = get_settings()\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_boolop_alias() -> None:
    """`cfg = settings or get_settings()` (BoolOp) resolves."""
    source = (
        "from personal_agent.config import settings\n"
        "from personal_agent.config.settings import get_settings\n"
        "def f():\n"
        "    cfg = settings or get_settings()\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_multiline_getattr() -> None:
    r"""A `getattr(settings,\n "<field>", default)` split across lines resolves.

    The exact quality_monitor_* pattern at `brainstem/scheduler.py:160` that a
    line-oriented grep cannot see.
    """
    source = (
        "from personal_agent.config import settings\n"
        "x = getattr(\n"
        '    settings, "quality_monitor_daily_run_hour_utc", 5\n'
        ")\n"
    )
    assert "quality_monitor_daily_run_hour_utc" in _reads(source)


def test_appconfig_typed_param() -> None:
    """A function param annotated `AppConfig` is a settings alias for its body."""
    source = (
        "from personal_agent.config.settings import AppConfig\n"
        "def f(cfg: AppConfig | None = None) -> bool:\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_import_alias() -> None:
    """`from personal_agent.config import settings as X` then `X.<field>` resolves.

    A real, common pattern (7× across `src/`, e.g. `orchestrator/executor.py:4198`
    `import settings as _s`); missing it dropped genuine reads into never-read.
    """
    source = "from personal_agent.config import settings as _s\nx = _s.debug\n"
    assert "debug" in _reads(source)


def test_self_attribute_alias() -> None:
    """`self._settings = get_settings()` then `self._settings.<field>` resolves.

    The codex-flagged wrong-deletion hole: a field read only through an instance
    attribute alias (real at `brainstem/optimizer.py:98/153`) must count as read.
    """
    source = (
        "from personal_agent.config.settings import get_settings\n"
        "class C:\n"
        "    def __init__(self, config=None):\n"
        "        self._settings = config or get_settings()\n"
        "    def check(self):\n"
        "        return self._settings.second_brain_cpu_threshold\n"
    )
    assert "second_brain_cpu_threshold" in _reads(source)


def test_shadowed_settings_name_is_not_a_read() -> None:
    """A file that rebinds `settings` to a non-AppConfig value does not seed `settings`.

    `scripts/study/sweep.py` binds `settings = StudySettings()`; its `settings.neo4j_uri`
    read must NOT be attributed to the AppConfig field of the same name.
    """
    source = (
        "class StudySettings:\n"
        "    neo4j_uri = 'x'\n"
        "settings = StudySettings()\n"
        "y = settings.neo4j_uri\n"
    )
    assert "neo4j_uri" not in _reads(source)


def test_unrelated_alias_is_not_a_read() -> None:
    """An attribute access on a name never bound to settings is not a read."""
    source = "cfg = object()\nx = cfg.debug\n"
    assert _reads(source) == set()


def test_alias_names_reports_name_and_attr_sets() -> None:
    """`settings_alias_names` returns the (name_aliases, attr_aliases) pair it resolved."""
    source = (
        "from personal_agent.config.settings import get_settings\n"
        "cfg = get_settings()\n"
        "class C:\n"
        "    def __init__(self):\n"
        "        self._s = get_settings()\n"
    )
    name_aliases, attr_aliases = settings_alias_names(ast.parse(source))
    assert "cfg" in name_aliases
    assert "settings" in name_aliases  # seeded (not shadowed)
    assert "_s" in attr_aliases
