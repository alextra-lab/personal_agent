# tests/personal_agent/memory/test_lucene_escape.py
"""Tests for the Lucene query-string escaper (ADR-0104 / FRE-723 lexical arm).

Pure unit test — no substrate. Free-text query strings hit Neo4j's Lucene-backed
full-text index parser; unescaped punctuation throws a parse error on ordinary
sentences, not just adversarial input.
"""

from __future__ import annotations

from personal_agent.memory.service import _escape_lucene_query


class TestEscapeLuceneQuery:
    """Tests for _escape_lucene_query."""

    def test_plain_text_passes_through_unchanged(self) -> None:
        assert _escape_lucene_query("vision perception eyesight") == ("vision perception eyesight")

    def test_each_special_char_is_escaped(self) -> None:
        specials = '+-!(){}[]^"~*?:\\/&|'
        for char in specials:
            text = f"a{char}b"
            escaped = _escape_lucene_query(text)
            assert escaped == f"a\\{char}b", f"char {char!r} not escaped: {escaped!r}"

    def test_colon_in_realistic_query_is_escaped(self) -> None:
        assert _escape_lucene_query("trace_id: abc123") == "trace_id\\: abc123"

    def test_empty_string_returns_empty(self) -> None:
        assert _escape_lucene_query("") == ""
