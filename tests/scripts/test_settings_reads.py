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


def test_appconfig_construction_alias() -> None:
    """`cfg = AppConfig()` then `cfg.<field>` resolves (config_guard.py pattern).

    A code-review pass found `settings = AppConfig()` was treated as a non-settings
    shadow; direct construction must count as a settings value.
    """
    source = (
        "from personal_agent.config.settings import AppConfig\n"
        "def f():\n"
        "    cfg = AppConfig()\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_aliased_factory_import() -> None:
    """`from ...config import get_settings as _gs; cfg = _gs(); cfg.<field>` resolves.

    Real at `captains_log/capture.py` — an aliased factory import the literal-name
    check missed, dropping a genuine read (wrong-deletion direction).
    """
    source = (
        "from personal_agent.config import get_settings as _gs\n"
        "def f():\n"
        "    cfg = _gs()\n"
        "    return cfg.second_brain_cpu_threshold\n"
    )
    assert "second_brain_cpu_threshold" in _reads(source)


def test_di_self_attribute_alias_without_factory_fallback() -> None:
    """`self._settings = config` (DI param, no `or get_settings()`) then a field read resolves.

    A code-review pass flagged this: a dependency-injected `config: AppConfig` stored on
    an instance attribute, read as `self._settings.<field>`, was missed.
    """
    source = (
        "from personal_agent.config.settings import AppConfig\n"
        "class C:\n"
        "    def __init__(self, config: AppConfig) -> None:\n"
        "        self._settings = config\n"
        "    def check(self):\n"
        "        return self._settings.neo4j_uri\n"
    )
    assert "neo4j_uri" in _reads(source)


def test_shadow_rebind_does_not_drop_sibling_reads() -> None:
    """A rebind of `settings` in one function must not drop a sibling function's read.

    The exact `scripts/study/run_ingest.py` wrong-deletion regression a code-review pass
    confirmed: file-global shadow suppression dropped a genuine `settings.<field>` read
    in a sibling function of the one doing `settings = OtherSettings()`.
    """
    source = (
        "from personal_agent.config import settings\n"
        "class OtherSettings:\n"
        "    pass\n"
        "def reads_singleton():\n"
        "    return settings.neo4j_uri\n"  # genuine AppConfig read
        "def rebinds():\n"
        "    settings = OtherSettings()\n"
        "    return settings\n"
    )
    assert "neo4j_uri" in _reads(source)


def test_appconfig_import_alias_typed_param() -> None:
    """`from ...settings import AppConfig as X` then an `X`-typed param resolves (FRE-907).

    A param name that is NOT `settings` proves this isn't resolving by naming coincidence
    (`self._settings` unconditionally seeds `settings` regardless of annotation) — the
    exact reproduction from the FRE-907 finding, real at `memory/freshness.py:34`.
    """
    source = (
        "from personal_agent.config.settings import AppConfig as Settings\n"
        "def f(cfg: Settings):\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_appconfig_import_alias_construction() -> None:
    """`from ...settings import AppConfig as X` then `cfg = X()` direct construction resolves."""
    source = (
        "from personal_agent.config.settings import AppConfig as Settings\n"
        "def f():\n"
        "    cfg = Settings()\n"
        "    return cfg.debug\n"
    )
    assert "debug" in _reads(source)


def test_short_appconfig_alias_does_not_pollute_string_annotation_matching() -> None:
    """A short `AppConfig` alias must not substring-match an unrelated forward-ref string.

    Regression guard for a code-review-confirmed precision defect: widening the
    string/forward-ref annotation branch to the full alias set (as opposed to the Name/
    Attribute/Call branches, which do exact-name matching) would let a single-letter
    alias like `import AppConfig as S` false-positive against ANY string annotation
    merely containing that letter — here `"SomeUnrelatedForwardRef"` contains `S` but is
    not an `AppConfig` reference.
    """
    source = (
        "from personal_agent.config.settings import AppConfig as S\n"
        'def f(x: "SomeUnrelatedForwardRef"):\n'
        "    return x.debug\n"
    )
    assert _reads(source) == set()


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
