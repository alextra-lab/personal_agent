"""Unit tests for the block-aware message-content helpers (ADR-0101 §2, FRE-664)."""

from __future__ import annotations

from personal_agent.llm_client.message_content import (
    IMAGE_BLOCK_TOKEN_ESTIMATE,
    count_content_tokens,
    get_text_content,
    merge_content,
)
from personal_agent.llm_client.token_counter import estimate_tokens

_TEXT_BLOCK = {"type": "text", "text": "look at this"}
_IMAGE_BLOCK = {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA"}}
_BLOCK_LIST = [_TEXT_BLOCK, _IMAGE_BLOCK]


class TestGetTextContent:
    def test_str_passthrough(self) -> None:
        assert get_text_content("hello") == "hello"

    def test_extracts_text_blocks(self) -> None:
        assert get_text_content(_BLOCK_LIST) == "look at this"

    def test_image_only_list_returns_empty_string(self) -> None:
        assert get_text_content([_IMAGE_BLOCK]) == ""

    def test_multiple_text_blocks_joined(self) -> None:
        content = [{"type": "text", "text": "first"}, {"type": "text", "text": "second"}]
        assert get_text_content(content) == "first\n\nsecond"

    def test_none_returns_empty_string(self) -> None:
        assert get_text_content(None) == ""

    def test_empty_list_returns_empty_string(self) -> None:
        assert get_text_content([]) == ""

    def test_malformed_text_value_skipped_not_crashed(self) -> None:
        content = [{"type": "text", "text": 123}, _TEXT_BLOCK]
        assert get_text_content(content) == "look at this"

    def test_non_dict_block_skipped(self) -> None:
        content = ["not-a-dict", _TEXT_BLOCK]
        assert get_text_content(content) == "look at this"


class TestMergeContent:
    def test_str_plus_str_matches_historical_behavior(self) -> None:
        assert merge_content("old", "new") == "old\n\nnew"

    def test_empty_old_returns_new(self) -> None:
        assert merge_content("", "new") == "new"

    def test_empty_new_returns_old(self) -> None:
        assert merge_content("old", "") == "old"

    def test_both_empty_returns_empty_string(self) -> None:
        assert merge_content("", "") == ""

    def test_list_plus_str_preserves_blocks_in_order(self) -> None:
        result = merge_content(_BLOCK_LIST, "trailing text")
        assert result == [_TEXT_BLOCK, _IMAGE_BLOCK, {"type": "text", "text": "trailing text"}]

    def test_str_plus_list_preserves_blocks_in_order(self) -> None:
        result = merge_content("leading text", _BLOCK_LIST)
        assert result == [{"type": "text", "text": "leading text"}, _TEXT_BLOCK, _IMAGE_BLOCK]

    def test_list_plus_list_concatenates(self) -> None:
        other_image = {"type": "image_url", "image_url": {"url": "data:image/png;base64,BBBB"}}
        result = merge_content([_IMAGE_BLOCK], [other_image])
        assert result == [_IMAGE_BLOCK, other_image]

    def test_list_plus_empty_str_drops_nothing(self) -> None:
        assert merge_content(_BLOCK_LIST, "") == _BLOCK_LIST


class TestCountContentTokens:
    def test_str_content_matches_estimate_tokens(self) -> None:
        assert count_content_tokens("hello world") == estimate_tokens("hello world")

    def test_block_list_counts_text_plus_fixed_image_estimate(self) -> None:
        text_tokens = estimate_tokens("look at this")
        assert count_content_tokens(_BLOCK_LIST) == text_tokens + IMAGE_BLOCK_TOKEN_ESTIMATE

    def test_image_only_list_counts_only_fixed_estimate(self) -> None:
        assert count_content_tokens([_IMAGE_BLOCK]) == IMAGE_BLOCK_TOKEN_ESTIMATE

    def test_multiple_images_sum_independently(self) -> None:
        assert count_content_tokens([_IMAGE_BLOCK, _IMAGE_BLOCK]) == 2 * IMAGE_BLOCK_TOKEN_ESTIMATE

    def test_none_returns_zero(self) -> None:
        assert count_content_tokens(None) == 0

    def test_malformed_text_value_does_not_crash_counts_zero_for_that_block(self) -> None:
        content = [{"type": "text", "text": 123}]
        assert count_content_tokens(content) == 0

    def test_huge_base64_does_not_inflate_estimate_beyond_fixed_cost(self) -> None:
        huge_data_uri = "data:image/png;base64," + ("A" * 100_000)
        block = {"type": "image_url", "image_url": {"url": huge_data_uri}}
        assert count_content_tokens([block]) == IMAGE_BLOCK_TOKEN_ESTIMATE
