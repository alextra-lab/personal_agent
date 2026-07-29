"""Tests for literal entity-mention verification (FRE-1041).

The resolver's precision half: the full-text index supplies recall, and these functions
decide which of its hits the message *literally* mentions. Every case below is drawn
from the FRE-1041 census over real turns.
"""

from __future__ import annotations

from personal_agent.memory.entity_mentions import mentions, tokenize, verify_mentions


class TestTokenize:
    """Tokenisation must split on punctuation, not just whitespace."""

    def test_splits_on_slash(self) -> None:
        """The decisive FRE-1041 case: ``melon/canteloupe`` is two tokens, not one."""
        assert tokenize("a melon/canteloupe ice cream") == [
            "a",
            "melon",
            "canteloupe",
            "ice",
            "cream",
        ]

    def test_casefolds(self) -> None:
        """Matching is case-insensitive, so tokens are casefolded."""
        assert tokenize("Melon ICE Cream") == ["melon", "ice", "cream"]

    def test_strips_punctuation_and_underscores(self) -> None:
        """Hyphens, possessives and underscores are separators, not name characters."""
        assert tokenize("well-known: the owner's snake_case") == [
            "well",
            "known",
            "the",
            "owner",
            "s",
            "snake",
            "case",
        ]

    def test_normalises_unicode(self) -> None:
        """NFKC folding keeps compatibility forms matchable."""
        assert tokenize("ﬁle") == tokenize("file")

    def test_empty_message(self) -> None:
        """An empty message tokenises to nothing rather than raising."""
        assert tokenize("") == []


class TestMentions:
    """Containment is a contiguous token run — never a substring."""

    def test_single_token_match(self) -> None:
        """A lowercase mention matches a capitalised graph name."""
        assert mentions("Melon", tokenize("i want a melon/canteloupe ice cream"))

    def test_multi_word_name_must_be_contiguous(self) -> None:
        """``Ice cream`` matches only when the words are adjacent."""
        assert mentions("Ice cream", tokenize("a melon ice cream"))
        assert not mentions("Ice cream", tokenize("ice cold whipped cream"))

    def test_rejects_substring_of_a_longer_word(self) -> None:
        """``Ice`` must not match inside ``nice`` — the substring trap."""
        assert not mentions("Ice", tokenize("that is a nice idea"))

    def test_rejects_absent_name(self) -> None:
        """A name the message never says is not a mention."""
        assert not mentions("Cantaloupe", tokenize("i would like a sorbet"))

    def test_misspelling_does_not_match(self) -> None:
        """No fuzzy matching: ``canteloupe`` does not reach ``Cantaloupe``.

        A stated FRE-1041 limitation — morphological or fuzzy matching would
        reintroduce the false-positive class the design removes.
        """
        assert not mentions("Cantaloupe", tokenize("a melon/canteloupe ice cream"))

    def test_plural_does_not_match(self) -> None:
        """No stemming: ``melons`` does not reach ``Melon`` (stated limitation)."""
        assert not mentions("Melon", tokenize("i bought two melons"))

    def test_empty_name_is_never_a_mention(self) -> None:
        """A blank or punctuation-only entity name cannot match anything."""
        assert not mentions("", tokenize("anything at all"))
        assert not mentions("   ", tokenize("anything at all"))

    def test_name_longer_than_message(self) -> None:
        """A name with more tokens than the message cannot match."""
        assert not mentions("Cantaloupe ice cream recipe", tokenize("cantaloupe"))


class TestVerifyMentions:
    """The public entry point the service calls on full-text output."""

    def test_keeps_only_literally_mentioned_names(self) -> None:
        """Full-text fuzz is dropped; literal mentions survive."""
        message = "I would like to make a melon/canteloupe ice cream"
        candidates = ["Melon", "Ice cream", "Ice cream maker", "Yogurt ice cream"]
        assert verify_mentions(message, candidates) == ["Melon", "Ice cream"]

    def test_preserves_candidate_order(self) -> None:
        """Order is the caller's rank order, so the cap keeps the best hits."""
        message = "the melon and the sorbet"
        assert verify_mentions(message, ["Sorbet", "Melon"]) == ["Sorbet", "Melon"]

    def test_deduplicates_while_preserving_order(self) -> None:
        """A name repeated by the index is returned once."""
        assert verify_mentions("a melon", ["Melon", "Melon"]) == ["Melon"]

    def test_returns_graph_canonical_casing(self) -> None:
        """Names come back exactly as the graph stores them.

        ``_overlap_subscore`` intersects case-sensitively, so a re-cased name
        would silently score zero.
        """
        assert verify_mentions("i ate a melon", ["Melon"]) == ["Melon"]

    def test_empty_inputs(self) -> None:
        """No message or no candidates yields no mentions."""
        assert verify_mentions("", ["Melon"]) == []
        assert verify_mentions("a melon", []) == []
