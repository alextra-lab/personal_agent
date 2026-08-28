"""Unit tests for the shared tool_code block scanner (FRE-1309)."""

from personal_agent.llm_client.tool_code_blocks import iter_tool_code_blocks


def test_no_tags_yields_nothing() -> None:
    assert list(iter_tool_code_blocks("just some plain text")) == []


def test_single_block() -> None:
    content = "before <tool_code>fn()</tool_code> after"
    blocks = list(iter_tool_code_blocks(content))
    assert len(blocks) == 1
    start, end, body = blocks[0]
    assert body == "fn()"
    assert content[start:end] == "<tool_code>fn()</tool_code>"


def test_multiple_blocks() -> None:
    content = "<tool_code>a()</tool_code> mid <tool_code>b()</tool_code>"
    blocks = list(iter_tool_code_blocks(content))
    assert [b for _, _, b in blocks] == ["a()", "b()"]


def test_adjacent_blocks_no_gap() -> None:
    content = "<tool_code>a()</tool_code><tool_code>b()</tool_code>"
    blocks = list(iter_tool_code_blocks(content))
    assert [b for _, _, b in blocks] == ["a()", "b()"]


def test_unclosed_open_yields_nothing() -> None:
    assert list(iter_tool_code_blocks("<tool_code>a() with no closer")) == []


def test_closer_with_no_preceding_opener_yields_nothing() -> None:
    assert list(iter_tool_code_blocks("</tool_code> stray closer, no opener before it")) == []


def test_closer_then_unclosed_open_yields_nothing() -> None:
    content = "</tool_code><tool_code>a() unclosed"
    assert list(iter_tool_code_blocks(content)) == []


def test_case_insensitive_tags() -> None:
    content = "<TOOL_CODE>fn()</Tool_Code>"
    blocks = list(iter_tool_code_blocks(content))
    assert [b for _, _, b in blocks] == ["fn()"]


def test_body_preserves_original_case() -> None:
    content = "<tool_code>Fn(Arg=Value)</tool_code>"
    blocks = list(iter_tool_code_blocks(content))
    assert blocks[0][2] == "Fn(Arg=Value)"


def test_valid_block_then_many_unmatched_opens_finds_only_the_valid_one() -> None:
    content = "<tool_code>a()</tool_code>" + "<tool_code>" * 5_000
    blocks = list(iter_tool_code_blocks(content))
    assert [b for _, _, b in blocks] == ["a()"]


def test_length_changing_lowercase_codepoint_does_not_desync_offsets() -> None:
    """U+0130 (İ) expands to two characters under str.lower() ("i̇") — a naive
    str.find(content.lower(), ...) scanner desyncs its offsets from the original
    string on any input containing it, corrupting every block boundary that
    follows. The scanner must not use a length-changing case fold.
    """
    content = "İ" * 5 + "<tool_code>REALCALL(1)</tool_code>SUFFIX"
    blocks = list(iter_tool_code_blocks(content))
    assert len(blocks) == 1
    start, end, body = blocks[0]
    assert body == "REALCALL(1)"
    assert content[start:end] == "<tool_code>REALCALL(1)</tool_code>"
