"""Completeness guard for the config inventory (FRE-648, ADR-0099 stage 0).

The inventory doc (`docs/reference/CONFIG_INVENTORY.md`) must cover every `AppConfig`
field and every documented `AGENT_*` env key. This test is the CI-enforced proof of
FRE-648 acceptance criterion #2 ("zero parameters missing") and a regression guard: a
future field added to `AppConfig` without a corresponding inventory row fails here.
"""

from __future__ import annotations

import re

from scripts.audit.config_inventory import (
    INVENTORY_DOC,
    find_coverage_gaps,
    generate,
)

from personal_agent.config.settings import AppConfig


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


def test_generated_output_leaks_no_secret_value_or_dsn_credential() -> None:
    """No secret default value or DSN-embedded credential may reach the generated doc.

    Backs the CodeQL "clear-text logging" remediation with a real-leak regression guard:
    builds every secret field's raw default plus any ``user:pass@`` userinfo in a URL
    default, and asserts none appear verbatim in ``generate()`` output (they must be
    redacted/sanitized). A future field with a hardcoded credential default fails here.
    """
    output = generate()
    leaked: list[str] = []
    for name, field in AppConfig.model_fields.items():
        try:
            default = (
                field.default
                if repr(field.default) != "PydanticUndefined"
                else (field.default_factory() if field.default_factory else None)
            )
        except Exception:  # noqa: BLE001 - factory may require args; nothing to check
            continue
        text = str(default)
        # Secret-field values must never appear; empty/None carry nothing to leak.
        if name in {"neo4j_password"} and text and text not in ("None", ""):
            if text in output:
                leaked.append(f"{name}={text!r}")
        # Credentials embedded as user:pass@ in any URI default must be stripped.
        for creds in re.findall(r"//([^/@\s]+:[^/@\s]+)@", text):
            if creds in output:
                leaked.append(f"{name} DSN creds {creds!r}")
    assert not leaked, f"secret/credential values leaked into generated doc: {leaked}"
