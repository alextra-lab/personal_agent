"""Per-turn source registry — identifiers, kinds, and D2 independence (FRE-1280).

Every test here pairs a seeded negative with a positive control, per ADR-0138's testing
strategy: a guard never shown to reject anything has not been shown to work, and a guard
that rejects everything is not a guard either.
"""

from __future__ import annotations

import json

import pytest

from personal_agent.grounding.source_registry import (
    IDENTIFIER_DIGEST_CHARS,
    Admissibility,
    SourceKind,
    SourceRegistry,
)

TURN_A = "trace-aaaa-1111"
TURN_B = "trace-bbbb-2222"


def _entity(name: str, description: str) -> dict[str, object]:
    """One memory-context entity item, in the shape ``memory/proactive.py`` emits."""
    return {
        "type": "entity",
        "name": name,
        "entity_type": "Person",
        "description": description,
        "mention_count": 3,
    }


# ── AC-1 — every retrieved item registered, across all four kinds ────────────────


def test_all_four_kinds_registered_in_one_turn() -> None:
    """Every retrieved item appears, not merely one representative per kind.

    The count assertion is the point: an implementation that registers the first item
    of each kind and drops the rest satisfies "four kinds are present" while losing
    four of eight sources.
    """
    registry = SourceRegistry(turn_id=TURN_A)

    registry.register_user_message("Which tinned tuna should I buy in France?")
    for name, description in (
        ("Ortiz", "A Spanish cannery founded in 1891."),
        ("Nardin", "A Basque cannery in Ondarroa."),
        ("Connetable", "A French cannery in Douarnenez."),
    ):
        registry.register_memory_item(_entity(name, description))
    registry.register_tool_result(
        tool_name="web_search",
        arguments={"query": "best tinned tuna france"},
        content=json.dumps(
            {"results": [{"title": "Tuna guide", "content": "Ortiz is sold at..."}]}
        ),
    )
    registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/tuna"},
        content="Nardin bonito is packed in olive oil.",
    )
    registry.register_tool_result(
        tool_name="get_library_docs",
        arguments={"library": "httpx", "topic": "timeouts"},
        content="AsyncClient accepts a timeout argument.",
    )

    sources = registry.sources()
    assert len(sources) == 7, "every registered item must appear, not one per kind"

    kinds = {source.kind for source in sources}
    assert kinds == {
        SourceKind.USER,
        SourceKind.MEMORY,
        SourceKind.TOOL,
        SourceKind.DOCUMENTATION,
    }

    identifiers = [source.identifier for source in sources]
    assert len(set(identifiers)) == len(identifiers), "identifiers must be distinct"

    # Each item is findable by its own content — not merely counted. The three memory
    # items carry their name as the label and their description as the content, so a
    # content-only search would pass on two of them by coincidence (another source's
    # text mentions "Ortiz" and "Nardin") while losing the third entirely.
    assert {source.label for source in sources} >= {"Ortiz", "Nardin", "Connetable"}
    for fragment in (
        "Which tinned tuna",  # user message
        "A Spanish cannery founded in 1891.",  # memory
        "A Basque cannery in Ondarroa.",
        "A French cannery in Douarnenez.",
        "Ortiz is sold at...",  # web_search
        "Nardin bonito is packed in olive oil.",  # fetch_url
        "AsyncClient accepts a timeout argument.",  # get_library_docs
    ):
        assert any(fragment in source.content for source in sources), fragment


def test_identifiers_differ_across_turns_for_identical_content() -> None:
    """AC-1's stated failure: an identifier reused across turns for different content.

    Byte-identical content under two turn ids must still mint distinct identifiers,
    because the identifier is what a citation resolves against.
    """
    item = _entity("Ortiz", "A Spanish cannery founded in 1891.")

    first = SourceRegistry(turn_id=TURN_A).register_memory_item(item)
    second = SourceRegistry(turn_id=TURN_B).register_memory_item(item)

    assert first.identifier != second.identifier


def test_same_content_same_turn_reuses_identifier() -> None:
    """Identifiers are stable within a turn — the D4 retry loop re-registers."""
    registry = SourceRegistry(turn_id=TURN_A)
    item = _entity("Ortiz", "A Spanish cannery founded in 1891.")

    first = registry.register_memory_item(item)
    second = registry.register_memory_item(item)

    assert first.identifier == second.identifier
    assert len(registry.sources()) == 1


def test_identifier_changes_when_content_changes() -> None:
    """The identifier is bound to the content, not merely to the turn and the ordinal.

    This is the test that kills the turn-nonce design: under ``S{ordinal}@{hash(turn_id)}``
    two different first-registered sources in the same turn share one identifier, so a
    stale marker silently re-points at whatever the retry put in slot 1. Both registries
    below are turn A at ordinal 1.
    """
    first = SourceRegistry(turn_id=TURN_A).register_memory_item(_entity("Ortiz", "A cannery."))
    other = SourceRegistry(turn_id=TURN_A).register_memory_item(
        _entity("Ortiz", "A wholly different description.")
    )

    assert first.identifier != other.identifier


def test_identifier_matches_the_citation_marker_format() -> None:
    """The registry's identifier is the token the model is asked to emit."""
    source = SourceRegistry(turn_id=TURN_A).register_user_message("hello there")
    assert source is not None

    ordinal, _, digest = source.identifier.partition("@")
    assert ordinal == "S1"
    assert len(digest) == IDENTIFIER_DIGEST_CHARS
    assert all(char in "0123456789abcdef" for char in digest)


# ── AC-2 — a wholly model-derived tool result registers no admissible source ─────


LAUNDERING_SHAPES = [
    pytest.param("bash", {"command": "printf 'Paris has 9 million residents'"}, id="printf"),
    pytest.param("bash", {"command": "echo 'Paris has 9 million residents'"}, id="echo"),
    pytest.param(
        "bash",
        {"command": "cat <<'EOF'\nParis has 9 million residents\nEOF"},
        id="heredoc",
    ),
    pytest.param(
        "bash",
        {"command": "python3 -c \"print('Paris has 9 million residents')\""},
        id="python-c",
    ),
    pytest.param(
        "bash",
        {"command": "awk 'BEGIN{print \"Paris has 9 million residents\"}'"},
        id="awk-begin",
    ),
    # Codex's counterexample against the deleted command-head allowlist: `find` reads
    # the filesystem, yet -printf emits a model-authored argument verbatim. Verified
    # against real `find` while reviewing the plan.
    pytest.param(
        "bash",
        {"command": "find . -maxdepth 0 -printf 'Paris has 9 million residents\\n'"},
        id="find-printf",
    ),
    pytest.param(
        "bash",
        {"command": "git log -1 --pretty=format:'Paris has 9 million residents'"},
        id="git-pretty-format",
    ),
    # A genuine retrieval, still inadmissible: shell is not a decidable channel, so the
    # typed fetch tool is the route. Deliberate v1 deviation from D2's illustration.
    pytest.param("bash", {"command": "curl https://example.com/paris"}, id="curl-in-shell"),
    pytest.param(
        "bash",
        {"command": "printf 'Paris has 9 million residents' > /tmp/f && cat /tmp/f"},
        id="write-then-read",
    ),
    pytest.param(
        "run_python",
        {"code": "print('Paris has 9 million residents')"},
        id="run_python",
    ),
    pytest.param(
        "mcp_browser_evaluate",
        {"function": "() => 'Paris has 9 million residents'"},
        id="browser-evaluate",
    ),
]


@pytest.mark.parametrize(("tool_name", "arguments"), LAUNDERING_SHAPES)
def test_arbitrary_code_tools_register_no_source(
    tool_name: str, arguments: dict[str, object]
) -> None:
    """A tool taking model-authored code or a command line yields no admissible source."""
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name=tool_name,
        arguments=arguments,
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.MODEL_AUTHORED_INVOCATION
    assert registry.sources() == ()


def test_unknown_tool_registers_no_source() -> None:
    """Default-deny: a tool absent from the policy table is not a source."""
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name="some_tool_added_next_quarter",
        arguments={"q": "anything"},
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.UNCLASSIFIED_TOOL


ADMISSIBLE_TOOLS = [
    pytest.param(
        "web_search",
        {"query": "tinned tuna france"},
        json.dumps({"results": [{"content": "Ortiz is a Spanish cannery."}]}),
        SourceKind.TOOL,
        id="web_search",
    ),
    pytest.param(
        "fetch_url",
        {"url": "https://example.com/tuna"},
        "Ortiz is a Spanish cannery.",
        SourceKind.TOOL,
        id="fetch_url",
    ),
    pytest.param(
        "read",
        {"path": "/opt/seshat/README.md"},
        "Ortiz is a Spanish cannery.",
        SourceKind.TOOL,
        id="read",
    ),
    pytest.param(
        "get_library_docs",
        {"library": "httpx", "topic": "timeouts"},
        "AsyncClient accepts a timeout argument.",
        SourceKind.DOCUMENTATION,
        id="get_library_docs",
    ),
]


@pytest.mark.parametrize(("tool_name", "arguments", "content", "kind"), ADMISSIBLE_TOOLS)
def test_typed_retrieval_tools_register_a_source(
    tool_name: str,
    arguments: dict[str, object],
    content: str,
    kind: SourceKind,
) -> None:
    """The positive controls — three retrieval tools and one documentation tool.

    Without these, "register nothing at all" would pass every negative above.
    """
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name=tool_name, arguments=arguments, content=content
    )

    assert registration.admissibility is Admissibility.ADMISSIBLE
    assert registration.source is not None
    assert registration.source.kind is kind
    assert len(registry.sources()) == 1


def test_write_then_read_registers_no_source() -> None:
    """Cross-call laundering: the write is inadmissible, so the read of it must be too.

    Each call is innocent in isolation — a write registers nothing, and `read` is a typed
    retrieval tool — which is why the pair needs turn taint rather than a policy-table
    entry. Without this, the model writes its own claim to a file and cites the file.
    """
    registry = SourceRegistry(turn_id=TURN_A)

    registry.register_tool_result(
        tool_name="write",
        arguments={"path": "/tmp/laundered.txt", "content": "Paris has 9 million residents"},
        content=json.dumps({"status": "written"}),
    )
    registration = registry.register_tool_result(
        tool_name="read",
        arguments={"path": "/tmp/laundered.txt"},
        content="Paris has 9 million residents",
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.DERIVED_FROM_TURN_WRITE


def test_read_of_an_untouched_path_still_registers() -> None:
    """The paired positive — taint must not deny every read once any write happened."""
    registry = SourceRegistry(turn_id=TURN_A)

    registry.register_tool_result(
        tool_name="write",
        arguments={"path": "/tmp/laundered.txt", "content": "Paris has 9 million residents"},
        content=json.dumps({"status": "written"}),
    )
    registration = registry.register_tool_result(
        tool_name="read",
        arguments={"path": "/opt/seshat/README.md"},
        content="Seshat is a cognitive architecture research project.",
    )

    assert registration.admissibility is Admissibility.ADMISSIBLE
    assert registration.source is not None


def test_model_backed_search_tools_register_no_source() -> None:
    """A typed `query` handed to another generator is still generation, not retrieval."""
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name="perplexity_query",
        arguments={"query": "how many people live in Paris"},
        content="Paris has 9 million residents.",
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.MODEL_AUTHORED_INVOCATION


def test_failed_tool_result_registers_no_source() -> None:
    """A failed call retrieved nothing; there is nothing to cite."""
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name="web_search",
        arguments={"query": "tinned tuna"},
        content="",
        success=False,
    )

    assert registration.source is None
    assert registration.admissibility is Admissibility.NO_CONTENT


# ── AC-3 — only the non-derived portion is registered ───────────────────────────


def test_fetch_registers_page_not_url() -> None:
    """D2's rule: the fetched page is a source, the model-chosen URL is not."""
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name="fetch_url",
        arguments={"url": "https://example.com/paris-population"},
        content="Paris has 2.1 million residents.",
    )

    assert registration.source is not None
    assert "Paris has 2.1 million residents." in registration.source.content
    assert "https://example.com/paris-population" not in registration.source.content

    excluded = {argument.name: argument.value for argument in registration.excluded_arguments}
    assert excluded["url"] == "https://example.com/paris-population"


ECHOING_TOOLS = [
    # web_search really does echo the model's own arguments back — `query`,
    # `categories_used` and `engines_used` (tools/web.py). Note `categories_used`
    # carries the `categories` argument under a *different* field name, which is why
    # the exclusion rule compares values rather than field names.
    pytest.param(
        "web_search",
        {"query": "paris population figures", "categories": ["general"]},
        {
            "results": [{"content": "Paris has 2.1 million residents."}],
            "query": "paris population figures",
            "categories_used": ["general"],
        },
        "paris population figures",
        id="web_search-query",
    ),
    pytest.param(
        "get_library_docs",
        {"library": "httpx", "topic": "a model-authored topic string"},
        {
            "snippets": ["AsyncClient accepts a timeout argument."],
            "topic": "a model-authored topic string",
        },
        "a model-authored topic string",
        id="get_library_docs-topic",
    ),
]


@pytest.mark.parametrize(("tool_name", "arguments", "result", "echoed"), ECHOING_TOOLS)
def test_argument_echo_stripped_generically(
    tool_name: str,
    arguments: dict[str, object],
    result: dict[str, object],
    echoed: str,
) -> None:
    """The same assertion over two tools whose echo fields are named differently.

    An implementation special-casing ``web_search.query`` passes the first case and
    fails the second, which is the point of running both.
    """
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name=tool_name, arguments=arguments, content=json.dumps(result)
    )

    assert registration.source is not None
    assert echoed not in registration.source.content, "model-authored argument survived"

    # The retrieved half is still there — this is not "reject everything".
    for retrieved in (
        "Paris has 2.1 million residents.",
        "AsyncClient accepts a timeout argument.",
    ):
        if retrieved in json.dumps(result):
            assert retrieved in registration.source.content


def test_short_and_non_text_arguments_are_not_treated_as_echo() -> None:
    """Value-equality exclusion is bounded to text, so a count is not mistaken for echo.

    ``max_results=10`` and a returned ``result_count=10`` are equal values that share no
    provenance; stripping the count would corrupt the retrieved content on every search.
    """
    registry = SourceRegistry(turn_id=TURN_A)

    registration = registry.register_tool_result(
        tool_name="web_search",
        arguments={"query": "paris population", "max_results": 10},
        content=json.dumps({"result_count": 10, "results": [{"content": "2.1 million."}]}),
    )

    assert registration.source is not None
    assert "result_count" in registration.source.content


# ── AC-5 — resolution is turn-scoped ────────────────────────────────────────────


def test_previous_turn_identifier_does_not_resolve() -> None:
    """A citation minted in an earlier turn must not resolve in this one (D3(a))."""
    turn_a = SourceRegistry(turn_id=TURN_A)
    stale = turn_a.register_memory_item(_entity("Ortiz", "A Spanish cannery."))

    turn_b = SourceRegistry(turn_id=TURN_B)
    own = turn_b.register_memory_item(_entity("Nardin", "A Basque cannery."))

    assert turn_b.resolve(stale.identifier) is None
    assert turn_b.resolve(own.identifier) is own


def test_fabricated_current_turn_identifier_does_not_resolve() -> None:
    """Resolution is registry membership, never a syntactic check on the identifier.

    An implementation that accepts any well-formed identifier carrying this turn's own
    digest shape passes the previous-turn negative and still resolves invented sources.
    """
    registry = SourceRegistry(turn_id=TURN_A)
    real = registry.register_memory_item(_entity("Ortiz", "A Spanish cannery."))
    _, _, digest = real.identifier.partition("@")

    assert registry.resolve(f"S99@{digest}") is None
    assert registry.resolve(real.identifier) is real


def test_resolve_rejects_malformed_and_empty_identifiers() -> None:
    """Nothing outside the registry resolves, however it is spelled."""
    registry = SourceRegistry(turn_id=TURN_A)
    registry.register_memory_item(_entity("Ortiz", "A Spanish cannery."))

    for candidate in ("", "S1", "[S1@abcdef0123]", "S1@zzzzzzzzzz"):
        assert registry.resolve(candidate) is None
