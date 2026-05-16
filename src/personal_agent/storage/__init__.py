"""Storage clients for byte-level persistence.

This package holds the substrate-level wrappers around object stores used by
the artifact substrate (ADR-0069 / FRE-227). The metadata canon lives in
Postgres (table ``artifacts``); these modules own the bytes side of the
contract.
"""

from personal_agent.storage.artifact_store import (
    ALLOWED_ARTIFACT_TYPES,
    ArtifactKeyError,
    ArtifactStoreError,
    R2ArtifactStore,
    build_r2_key,
    get_artifact_store,
)

__all__ = [
    "ALLOWED_ARTIFACT_TYPES",
    "ArtifactKeyError",
    "ArtifactStoreError",
    "R2ArtifactStore",
    "build_r2_key",
    "get_artifact_store",
]
