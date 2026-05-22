"""Substrate fingerprint helpers for environment isolation (FRE-375).

These pure functions detect whether a URI/URL matches the *default* production
fingerprint — i.e., ``localhost`` (or ``127.0.0.1``) on the canonical service
port.  They are intentionally conservative: a URI only matches when *both* host
and port agree with the default.

Placement rationale: this module lives in ``personal_agent.config`` (not in
``personal_agent.memory``) to break the potential circular import chain:
``memory.service`` → ``config.settings`` → ``config._substrate_fingerprint``.
Placing it in ``memory/`` would create a cycle.
"""

from urllib.parse import urlparse

_LOCAL_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1"})

_PROD_NEO4J_PORT: int = 7687
_PROD_ELASTICSEARCH_PORT: int = 9200
_PROD_POSTGRES_PORT: int = 5432


def is_prod_neo4j_uri(uri: str) -> bool:
    """Return True when *uri* matches the default production Neo4j fingerprint.

    The default production fingerprint is ``bolt://localhost:7687`` or the
    equivalent with ``127.0.0.1``.  Any deviation in host or port (e.g. a
    test-stack ``bolt://localhost:7688``) returns False.

    Args:
        uri: Neo4j connection URI, e.g. ``bolt://localhost:7687``.

    Returns:
        True when the URI resolves to localhost (or 127.0.0.1) on port 7687.
    """
    parsed = urlparse(uri)
    host = parsed.hostname or ""
    port = parsed.port
    return host in _LOCAL_HOSTS and port == _PROD_NEO4J_PORT


def is_prod_elasticsearch_url(url: str) -> bool:
    """Return True when *url* matches the default production Elasticsearch fingerprint.

    The default production fingerprint is ``http://localhost:9200`` or the
    equivalent with ``127.0.0.1``.

    Args:
        url: Elasticsearch base URL, e.g. ``http://localhost:9200``.

    Returns:
        True when the URL resolves to localhost (or 127.0.0.1) on port 9200.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    return host in _LOCAL_HOSTS and port == _PROD_ELASTICSEARCH_PORT


def is_prod_postgres_url(url: str) -> bool:
    """Return True when *url* matches the default production PostgreSQL fingerprint.

    The default production fingerprint is a URL containing localhost (or
    127.0.0.1) on port 5432.

    Args:
        url: PostgreSQL connection URL, e.g.
            ``postgresql+asyncpg://agent:pw@localhost:5432/personal_agent``.

    Returns:
        True when the URL resolves to localhost (or 127.0.0.1) on port 5432.
    """
    parsed = urlparse(url)
    host = parsed.hostname or ""
    port = parsed.port
    return host in _LOCAL_HOSTS and port == _PROD_POSTGRES_PORT
