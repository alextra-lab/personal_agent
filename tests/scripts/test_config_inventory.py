"""Completeness guard for the config inventory (FRE-648, ADR-0099 stage 0).

The inventory doc (`docs/reference/CONFIG_INVENTORY.md`) must cover every `AppConfig`
field and every documented `AGENT_*` env key. This test is the CI-enforced proof of
FRE-648 acceptance criterion #2 ("zero parameters missing") and a regression guard: a
future field added to `AppConfig` without a corresponding inventory row fails here.
"""

from __future__ import annotations

from scripts.audit.config_inventory import (
    INVENTORY_DOC,
    find_coverage_gaps,
)


def test_inventory_doc_exists() -> None:
    """The committed inventory deliverable is present."""
    assert INVENTORY_DOC.exists(), f"{INVENTORY_DOC} is missing"


def test_inventory_has_autogen_block() -> None:
    """The machine-generated AppConfig section is present and delimited."""
    doc = INVENTORY_DOC.read_text(encoding="utf-8")
    assert "<!-- AUTOGEN:AppConfig START" in doc
    assert "<!-- AUTOGEN:AppConfig END -->" in doc


def test_inventory_covers_every_appconfig_field_and_env_key() -> None:
    """AC#2: zero AppConfig fields and zero documented AGENT_ keys absent from the doc.

    Also a regression guard — a field added to AppConfig without an inventory row fails.
    """
    missing_fields, missing_env = find_coverage_gaps()
    assert not missing_fields, f"AppConfig fields absent from inventory: {missing_fields}"
    assert not missing_env, f".env.example AGENT_ keys absent from inventory: {missing_env}"
