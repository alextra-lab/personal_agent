"""Manifest-drift guard for the ADR-0112 AC-1 owner-storage-allowlist validator.

``AppConfig._validate_owner_storage_allowlist`` checks ``database_url``,
``database_admin_url``, ``sysgraph_database_url``, ``neo4j_uri``, and
``elasticsearch_url`` directly rather than resolving the ``private`` profile
through ``resolve_substrate()`` (see the FRE-819 plan for why). That shortcut is
only correct as long as ``config/substrate.yaml``'s ``private`` profile still
maps its three D3 store components to exactly those ``setting:`` sources. This
test fails loudly the moment that mapping drifts, so the shortcut doesn't
silently stop matching AC-1's "resolved target" semantics.
"""

from __future__ import annotations

from personal_agent.config.config_guard import load_substrate_manifest, repo_root


def test_private_profile_store_sources_match_validator_fields() -> None:
    """The private profile's postgres/neo4j/elasticsearch rows resolve from the
    exact AppConfig fields the AC-1 validator checks directly.
    """
    manifest = load_substrate_manifest(repo_root())
    private = manifest["profiles"]["private"]

    assert private["postgres"]["source"] == "setting:database_url"
    assert private["neo4j"]["source"] == "setting:neo4j_uri"
    assert private["elasticsearch"]["source"] == "setting:elasticsearch_url"
