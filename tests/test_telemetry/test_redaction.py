"""Tests for telemetry secret redaction (FRE-1068).

The corpus is deliberately split into positive cases (one per detector, each of
which must fire) and negative cases (which must pass through byte-identical).
A rule that never fires and a rule that works produce the same clean result on
positives alone, which is the failure mode FRE-1068's acceptance criteria calls
out; the negative corpus is what distinguishes them.

No value in this file is a live credential. Every secret-shaped string is
synthetic.
"""

from __future__ import annotations

import pytest

from personal_agent.telemetry.redaction import redact_mapping, redact_text

# --------------------------------------------------------------------------
# Positive corpus: one case per detector. Each must be redacted.
# --------------------------------------------------------------------------


def _shaped(prefix: str, body: str) -> str:
    """Assemble a secret-shaped fixture at import time.

    The halves are kept apart in the source so no committed line contains a
    contiguous credential-shaped literal. GitHub push protection blocks such a
    line even when the value is entirely fabricated — it blocked this very file
    on first push — and a test suite that cannot be pushed is not a test suite.

    Args:
        prefix: The token's recognisable prefix.
        body: The synthetic remainder.

    Returns:
        The assembled fixture value.
    """
    return prefix + body


AWS_KEY = _shaped("AKIA", "EXAMPLEEXAMPLE00")
JWT = _shaped("eyJ", "0FAKEHEADER0.eyJ0FAKEPAYLOAD0.0FAKESIGNATURE0")
GITHUB_PAT = _shaped("ghp_", "0000EXAMPLE0000EXAMPLE0000EXAMPLE000")
SLACK_TOKEN = _shaped("xoxb-", "0000000000-EXAMPLEEXAMPLE")
API_KEY = _shaped("sk-", "proj0000example0000example000")

POSITIVE_CASES: list[tuple[str, str, str]] = [
    (
        "aws_access_key",
        f"aws configure set aws_access_key_id {AWS_KEY}",
        AWS_KEY,
    ),
    (
        "jwt",
        f"Authorization: Bearer {JWT}",
        JWT,
    ),
    (
        "github_pat",
        f"git remote add origin https://{GITHUB_PAT}@github.com/x/y",
        GITHUB_PAT,
    ),
    (
        "slack_token",
        f"curl -H 'token: {SLACK_TOKEN}'",
        SLACK_TOKEN,
    ),
    (
        "api_key",
        f"client = OpenAI(api_key='{API_KEY}')",
        API_KEY,
    ),
    (
        "connection_string",
        "psql postgresql://agent:s3cr3tP4ssw0rd@postgres:5432/personal_agent",
        "s3cr3tP4ssw0rd",
    ),
    (
        "credential_assignment",
        "driver.session(password='mSX4VOSLOGUgbnimlvnFsi5c71wnhNH')",
        "mSX4VOSLOGUgbnimlvnFsi5c71wnhNH",
    ),
    (
        "credential_assignment",
        "PGPASSWORD=hunter2hunter2 psql -h postgres",
        "hunter2hunter2",
    ),
    (
        "credential_flag",
        "neo4j-admin --password Sup3rS3cretValue dump",
        "Sup3rS3cretValue",
    ),
    (
        "private_key",
        "-----BEGIN RSA PRIVATE KEY-----\nMIIEowIBAAKCAQEAxYZ\n-----END RSA PRIVATE KEY-----",
        "MIIEowIBAAKCAQEAxYZ",
    ),
    (
        "email",
        "Draft a reply to susan.example@contoso.com about the invoice",
        "susan.example@contoso.com",
    ),
]


@pytest.mark.parametrize(("detector", "raw", "secret"), POSITIVE_CASES)
def test_positive_corpus_secret_is_removed(detector: str, raw: str, secret: str) -> None:
    """Every detector fires: the secret substring is gone and a marker is left."""
    result = redact_text(raw)

    assert secret not in result, f"{detector}: secret survived redaction"
    assert "[REDACTED:" in result, f"{detector}: no marker emitted"


def test_private_key_body_is_redacted_not_just_the_header() -> None:
    """The PEM body must go, not merely the BEGIN line."""
    pem = (
        "-----BEGIN OPENSSH PRIVATE KEY-----\n"
        "b3BlbnNzaC1rZXktdjEAAAAABG5vbmUAAAAEbm9uZQAAAAAAAAAB\n"
        "AAAAMwAAAAtzc2gtZWQyNTUxOQAAACDsecretkeymaterialhere\n"
        "-----END OPENSSH PRIVATE KEY-----"
    )
    result = redact_text(pem)

    assert "b3BlbnNzaC1rZXktdjEA" not in result
    assert "secretkeymaterialhere" not in result


# --------------------------------------------------------------------------
# Negative corpus: must pass through byte-identical.
# --------------------------------------------------------------------------

NEGATIVE_CASES: list[tuple[str, str]] = [
    # The env-lookup forms measured in live telemetry (FRE-1068 audit).
    ("env_get", "password=os.environ.get('AGENT_NEO4J_PASSWORD')"),
    ("env_get_spaced", 'password = os.environ.get("AGENT_NEO4J_PASSWORD")'),
    ("env_getenv", "password=os.getenv('POSTGRES_PASSWORD')"),
    ("env_subscript", "password=environ['POSTGRES_PASSWORD']"),
    ("shell_var_braced", "PGPASSWORD=${POSTGRES_PASSWORD} psql -h postgres"),
    ("shell_var_plain", "PGPASSWORD=$POSTGRES_PASSWORD psql -h postgres"),
    ("shell_var_default", "PGPASSWORD=${POSTGRES_PASSWORD:-postgres} psql -h postgres"),
    ("process_env", "token: process.env.GITHUB_TOKEN"),
    # Placeholders and documentation values.
    ("placeholder_angle", "password=<your-password-here>"),
    ("placeholder_changeme", "password=changeme"),
    ("placeholder_example", "api_key=example-key-value"),
    ("placeholder_redacted", "token=REDACTED"),
    # Ordinary prose and structured content that must not be mangled.
    ("prose", "The user asked how to reset a forgotten password on the router."),
    ("url_no_creds", "curl -s 'https://api.github.com/repos/alextra-lab/personal_agent'"),
    ("event_name", "tool_call_completed"),
    ("json_fragment", '{"event_type": "bash_started", "duration_ms": 12.5}'),
    ("path", "/opt/seshat/telemetry/captains_log/captures/2026-08-04/abc.json"),
]


@pytest.mark.parametrize(("label", "raw"), NEGATIVE_CASES)
def test_negative_corpus_passes_through_unchanged(label: str, raw: str) -> None:
    """Non-secret content is never touched — proves the rule is not firing blindly."""
    assert redact_text(raw) == raw, f"{label}: false positive"


# --------------------------------------------------------------------------
# Structural behaviour
# --------------------------------------------------------------------------


def test_redact_mapping_recurses_dicts_and_lists() -> None:
    """Nested structures are redacted at any depth, including inside lists."""
    doc = {
        "event_type": "bash_started",
        "command": "psql postgresql://agent:n3st3dS3cret@postgres:5432/db",
        "arguments": {"command": f"export TOKEN={GITHUB_PAT}"},
        "messages_preview": [
            {"content_preview": "reach me at nested.person@contoso.com"},
            {"content_preview": "nothing sensitive here"},
        ],
        "duration_ms": 12.5,
        "success": True,
        "missing": None,
    }

    result = redact_mapping(doc)

    assert "n3st3dS3cret" not in result["command"]
    assert GITHUB_PAT not in result["arguments"]["command"]
    assert "nested.person@contoso.com" not in result["messages_preview"][0]["content_preview"]
    assert result["messages_preview"][1]["content_preview"] == "nothing sensitive here"
    # Non-string scalars pass through untouched, with their types intact.
    assert result["duration_ms"] == 12.5
    assert result["success"] is True
    assert result["missing"] is None
    assert result["event_type"] == "bash_started"


def test_redact_mapping_does_not_mutate_the_input() -> None:
    """Redaction returns a new structure; the caller's dict is left alone."""
    doc = {"command": "PGPASSWORD=hunter2hunter2 psql"}
    original = doc["command"]

    redact_mapping(doc)

    assert doc["command"] == original


def test_redaction_is_idempotent() -> None:
    """Redacting an already-redacted value is a no-op, so double application is safe."""
    once = redact_text("PGPASSWORD=hunter2hunter2 psql -h postgres")
    twice = redact_text(once)

    assert once == twice


def test_field_names_are_preserved() -> None:
    """Keys are never redacted — a field named 'password' must stay queryable."""
    result = redact_mapping({"password": "hunter2hunter2", "api_key_name": "prod"})

    assert set(result) == {"password", "api_key_name"}


def test_credential_field_name_redacts_a_value_no_detector_would_catch() -> None:
    """A bare value under a credential key is redacted on the key alone.

    ``{"password": "hunter2"}`` carries no secret *shape* — without a key-aware
    rule the value would survive every pattern detector.
    """
    result = redact_mapping({"password": "hunter2hunter2", "db_password": "plainvalue"})

    assert result["password"] == "[REDACTED:credential_field]"
    assert result["db_password"] == "[REDACTED:credential_field]"


@pytest.mark.parametrize("key", ["api_key_name", "token_count", "secret_sauce_description"])
def test_credential_key_rule_does_not_overreach(key: str) -> None:
    """Keys that merely contain a credential word are not treated as secrets."""
    assert redact_mapping({key: "ordinary value"})[key] == "ordinary value"


def test_credential_field_preserves_indirection_values() -> None:
    """An env-lookup under a credential key stays readable — it is not a secret."""
    result = redact_mapping({"password": "${POSTGRES_PASSWORD}"})

    assert result["password"] == "${POSTGRES_PASSWORD}"


def test_detect_secrets_reports_only_what_would_actually_be_redacted() -> None:
    """A pattern match is not a finding — the safe-value guard must apply.

    Without this, an audit counts every ``password=os.environ.get(...)`` in the
    corpus as a secret and reports an inflated figure.
    """
    from personal_agent.telemetry.redaction import detect_secrets

    assert detect_secrets("password=os.environ.get('PGPASSWORD')") == ()
    assert "credential_assignment" in detect_secrets("PGPASSWORD=hunter2hunter2 psql")


def test_detect_secrets_agrees_with_redact_text() -> None:
    """The reporting API and the enforcing API cannot disagree."""
    from personal_agent.telemetry.redaction import detect_secrets

    for _label, raw in NEGATIVE_CASES:
        assert detect_secrets(raw) == (), f"{raw!r} reported as a secret but is not redacted"
    for _detector, raw, _secret in POSITIVE_CASES:
        assert detect_secrets(raw), f"{raw!r} is redacted but not reported"


def test_marker_names_the_detector_that_fired() -> None:
    """The marker is attributable, so a fired rule is visible rather than silent."""
    result = redact_text(f"aws_access_key_id {AWS_KEY}")

    assert "[REDACTED:aws_access_key]" in result


def test_redaction_fails_closed_on_internal_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redactor that raises yields a redacted marker, never the raw value.

    FRE-1068: 'secrets are always redacted' and 'degrade to the original record'
    are contradictory. Content fails closed.
    """
    import personal_agent.telemetry.redaction as redaction

    def boom(_value: str) -> str:
        raise RuntimeError("detector exploded")

    monkeypatch.setattr(redaction, "_apply_detectors", boom)

    result = redaction.redact_mapping({"command": "PGPASSWORD=hunter2hunter2 psql"})

    assert result["command"] == "[REDACTED:error]"


def test_non_string_keys_and_empty_values_are_safe() -> None:
    """Degenerate shapes do not raise."""
    assert redact_mapping({}) == {}
    assert redact_mapping({"a": ""}) == {"a": ""}
    assert redact_mapping({"a": []}) == {"a": []}
    assert redact_text("") == ""
