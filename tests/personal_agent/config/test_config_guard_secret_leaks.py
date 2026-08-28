"""Regression test: Finding objects never leak detected secret values.

Tests FRE-1313 — a guard that produces Finding objects must never interpolate
a detected secret's value into the message, even though the value is in scope
during parsing. This is a ratchet test: temporary addition of the value to the
message proves the seeded negative (AC-1).

CodeQL alert #25 (`py/clear-text-logging-sensitive-data`, high) is a false
positive — this test converts dismissal into a durable guarantee.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

from pydantic.fields import FieldInfo

from personal_agent.config.config_guard import (
    Finding,
    check_committed_secrets,
    check_secret_field_plaintext_defaults,
)


def test_committed_secret_never_leaks_value():
    """AC-1/AC-2: check_committed_secrets does not interpolate the value.

    Seeded negative: a distinctive canary (not a real API key) appears in the
    YAML but NOT in the Finding.__str__() output. Temporarily add the value to
    the message and confirm the test fails to demonstrate the seeded negative
    is real, not decorative.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        config_dir = root / "config"
        config_dir.mkdir()

        # Seeded secret: a distinctive canary not used elsewhere
        canary = "CANARY_SECRET_VALUE_FRE1313"

        # Create a config YAML with a committed secret
        config_yaml = config_dir / "models.yaml"
        config_yaml.write_text(
            f"""\
models:
  claude-sonnet:
    id: claude-sonnet-5

# Simulate a committed secret: AGENT_LINEAR_API_KEY (secret-marked field)
AGENT_LINEAR_API_KEY: {canary}
""",
            encoding="utf-8",
        )

        # Run the check
        findings = check_committed_secrets(root)

        # Verify a finding was produced (guard detected the secret)
        assert any(f.check == "committed_secret" for f in findings), (
            f"Expected a committed_secret finding, got: {findings}"
        )

        # AC-1 / AC-3: Assert the canary does NOT appear in the printed form
        for finding in findings:
            if finding.check == "committed_secret":
                printed = str(finding)
                assert canary not in printed, (
                    f"Secret value LEAKED in Finding.__str__(): {printed}\n"
                    "The canary value must not appear in the message."
                )
                # Verify the message contains field name and path/line (the safe parts)
                assert "linear_api_key" in printed.lower(), (
                    f"Expected field name in message, got: {printed}"
                )


def test_plaintext_default_never_leaks_value():
    """AC-1/AC-2: check_secret_field_plaintext_defaults does not interpolate.

    Seeded negative: a secret-marked field with a plaintext default is detected,
    but the default value does NOT appear in Finding.__str__().
    """
    # Create a mock FieldInfo with a secret-marked field carrying a plaintext default
    canary = "CANARY_DEFAULT_VALUE_FRE1313"

    secret_field = FieldInfo(
        annotation=str,
        default=canary,
        description="A secret field with a plaintext default",
        json_schema_extra={"secret": True},
    )

    fields = {"my_secret": secret_field}

    # Run the check
    findings = check_secret_field_plaintext_defaults(fields)

    # Verify a finding was produced (guard detected the plaintext default)
    assert any(f.check == "secret_field_plaintext_default" for f in findings), (
        f"Expected a secret_field_plaintext_default finding, got: {findings}"
    )

    # AC-1 / AC-3: Assert the canary does NOT appear in the printed form
    for finding in findings:
        if finding.check == "secret_field_plaintext_default":
            printed = str(finding)
            assert canary not in printed, (
                f"Secret value LEAKED in Finding.__str__(): {printed}\n"
                "The canary default value must not appear in the message."
            )
            # Verify the message contains field name (the safe part)
            assert "my_secret" in printed, f"Expected field name in message, got: {printed}"


def test_finding_str_format():
    """AC-3: Verify str(Finding) renders [severity] check: message."""
    finding = Finding(
        check="test_check",
        severity="safety",
        message="test message",
    )
    assert str(finding) == "[safety] test_check: test message"
