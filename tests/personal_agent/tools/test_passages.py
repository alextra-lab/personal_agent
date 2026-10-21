"""Tests for the passage split and the ranker arms (ADR-0158 D3.2, D4; FRE-1641)."""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from personal_agent.tools.passages import (
    Passage,
    RerankCall,
    bm25_order,
    focus_matched,
    rank_passages,
    select_within_budget,
    split_passages,
    tokenize,
)


def _para(n: int, word: str = "lorem") -> str:
    """A block of about ``n`` characters with sentence ends every ~60 characters."""
    sentence = f"The {word} sentence holds a few plain words here. "
    return (sentence * (n // len(sentence) + 1))[:n].strip()


def test_passages_are_verbatim_slices() -> None:
    text = "\n".join([_para(300), "short line", _para(250), _para(1700, "ipsum"), "tail"])
    passages = split_passages(text)
    assert passages
    for i, p in enumerate(passages):
        assert p.index == i
        assert p.text == text[p.start : p.end]


def test_short_blocks_merge_with_the_next_block() -> None:
    text = "Title\nby someone\n" + _para(300)
    passages = split_passages(text)
    assert len(passages) == 1
    assert passages[0].text.startswith("Title\nby someone\n")


def test_long_blocks_split_at_a_sentence_end_within_800() -> None:
    text = _para(2500)
    passages = split_passages(text)
    assert len(passages) >= 4
    for p in passages:
        assert len(p.text) <= 800
    # Every passage but the last ends at a sentence end.
    for p in passages[:-1]:
        assert p.text.rstrip().endswith(".")


def test_a_block_with_no_sentence_end_still_splits_at_800() -> None:
    text = "x" * 2000
    passages = split_passages(text)
    assert [len(p.text) for p in passages] == [800, 800, 400]


def test_a_trailing_short_block_is_kept() -> None:
    text = _para(400) + "\nend"
    passages = split_passages(text)
    assert passages[-1].text.endswith("end")
    assert "".join(p.text for p in passages).replace("\n", "") == text.replace("\n", "")


def test_empty_text_has_no_passages() -> None:
    assert split_passages("") == []
    assert split_passages("\n\n") == []


def test_tokenize_drops_stop_words_and_case() -> None:
    assert tokenize("What is the Price of the M4 Max?") == ["price", "m4", "max"]


def _passages(*texts: str) -> list[Passage]:
    out: list[Passage] = []
    pos = 0
    for i, t in enumerate(texts):
        out.append(Passage(index=i, start=pos, end=pos + len(t), text=t))
        pos += len(t) + 1
    return out


def test_bm25_puts_the_matching_passage_first() -> None:
    ps = _passages(
        "Weather in Lyon is mild in spring.",
        "The Nemotron model decodes 159 tokens per second on the M4 Max.",
        "Cooking pasta takes ten minutes.",
    )
    order = bm25_order("Nemotron decode speed on M4 Max", ps)
    assert order[0] == 1
    assert sorted(order) == [0, 1, 2]


def test_bm25_ties_keep_page_order() -> None:
    ps = _passages("alpha", "beta", "gamma")
    assert bm25_order("delta", ps) == [0, 1, 2]


class _FakeRerank:
    """Scores a document by its position from the end, and records each call."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    async def __call__(self, query: str, documents: Sequence[str]) -> RerankCall:
        self.calls.append(list(documents))
        n = len(documents)
        return RerankCall(scores=[float(i) for i in range(n)], tokens=10 * n)


def _many(n: int) -> list[Passage]:
    return _passages(*[f"passage number {i} " + ("match " if i % 7 == 0 else "") for i in range(n)])


@pytest.mark.asyncio
async def test_r1_is_local() -> None:
    rr = _FakeRerank()
    out = await rank_passages("R1", "match", _many(30), rr)
    assert rr.calls == []
    assert out.sent_to_vendor is False
    assert out.rerank_tokens == 0
    assert sorted(out.order) == list(range(30))


@pytest.mark.asyncio
async def test_r2_reranks_only_the_first_100() -> None:
    rr = _FakeRerank()
    out = await rank_passages("R2", "match", _many(130), rr)
    assert len(rr.calls) == 1
    assert len(rr.calls[0]) == 100
    assert set(out.order) == set(range(100))
    # The fake scores the last document highest.
    assert out.order[0] == 99
    assert out.sent_to_vendor is True
    assert out.rerank_tokens == 1000


@pytest.mark.asyncio
async def test_r3_reranks_the_bm25_top_20() -> None:
    rr = _FakeRerank()
    ps = _many(60)
    out = await rank_passages("R3", "match", ps, rr)
    top20 = bm25_order("match", ps)[:20]
    assert len(rr.calls) == 1
    assert rr.calls[0] == [ps[i].text for i in top20]
    assert set(out.order) == set(top20)
    assert out.order[0] == top20[-1]


@pytest.mark.asyncio
async def test_r4_is_r1_up_to_50_passages_and_r3_above() -> None:
    rr = _FakeRerank()
    small = await rank_passages("R4", "match", _many(50), rr)
    assert rr.calls == []
    assert small.sent_to_vendor is False
    large = await rank_passages("R4", "match", _many(51), rr)
    assert len(rr.calls) == 1
    assert len(rr.calls[0]) == 20
    assert large.sent_to_vendor is True


@pytest.mark.asyncio
async def test_no_passages_makes_no_vendor_call() -> None:
    rr = _FakeRerank()
    out = await rank_passages("R2", "match", [], rr)
    assert out.order == []
    assert rr.calls == []
    assert out.sent_to_vendor is False


def test_focus_matched_is_lexical_and_ignores_stop_words() -> None:
    ps = _passages("The price is low.", "Shipping takes a week.")
    assert focus_matched("what is the price", ps) is True
    assert focus_matched("what is the", ps) is False
    assert focus_matched("battery life", ps) is False
    assert focus_matched("price", []) is False


def test_select_within_budget_skips_what_does_not_fit_and_returns_page_order() -> None:
    ps = _passages("a" * 500, "b" * 700, "c" * 300, "d" * 600)
    chosen = select_within_budget([1, 3, 0, 2], ps, 1500)
    # 1 (700) + 3 (600) = 1300; 0 (500) does not fit; 2 (300) does not fit (1600).
    assert [p.index for p in chosen] == [1, 3]
    chosen = select_within_budget([1, 2, 3, 0], ps, 1700)
    assert [p.index for p in chosen] == [1, 2, 3]
    assert sum(len(p.text) for p in chosen) <= 1700
