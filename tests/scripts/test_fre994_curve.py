"""FRE-994 digest compression curve harness.

Covers the parts that must be right *before* any spend: the sampling frame, the
structural arm table, the response schema derived from the stored record, the
token decomposition that separates envelope overhead from instruction-following
failure, and the cost projection the owner authorises the run against.

The paid stages (generation, extraction, judging) are exercised here only through
their pure helpers — the calls themselves are Phase B/C and are not made by any
test.
"""

# ruff: noqa: D103

from __future__ import annotations

import pytest
from scripts.eval.fre994_digest_compression_curve import arms, corpus

# ── Sampling frame ──────────────────────────────────────────────────────────


def _bucket(key: str, docs: int, chars: float) -> dict[str, object]:
    return {
        "key": key,
        "doc_count": docs,
        "chars": {"value": chars},
        "first": {"value_as_string": "2026-07-20T10:00:00.000Z"},
        "last": {"value_as_string": "2026-07-20T11:00:00.000Z"},
    }


def _frame() -> list[dict[str, object]]:
    """40 real sessions spanning a wide size range, plus the noise to reject."""
    real = [
        _bucket(f"{i:08x}-1111-2222-3333-444455556666", 2 + (i % 8), 500.0 * (i + 1))
        for i in range(40)
    ]
    noise = [
        _bucket("test-session", 272, 14702.0),  # synthetic id
        _bucket("test-reasoning", 33, 3036.0),  # synthetic id
        _bucket("aaaaaaaa-1111-2222-3333-444455556666", 1, 900.0),  # below the floor
    ]
    return real + noise


def test_frame_rejects_synthetic_ids_and_single_turn_sessions() -> None:
    """Synthetic `test-*` ids are eval residue, not conversations, and a one-turn session is below MIN_TURNS_FOR_DIGEST — the producer would never digest it, so including it would calibrate against material the bound never sees."""
    eligible = corpus.eligible_sessions(_frame())

    keys = {s.session_id for s in eligible}
    assert "test-session" not in keys
    assert "test-reasoning" not in keys
    assert "aaaaaaaa-1111-2222-3333-444455556666" not in keys
    assert len(eligible) == 40


def test_sample_is_deterministic_for_a_seed() -> None:
    """The manifest records a seed; a run that cannot be reproduced from it is not evidence."""
    first = corpus.draw_sample(corpus.eligible_sessions(_frame()), fit_n=12, holdout_n=4, seed=7)
    again = corpus.draw_sample(corpus.eligible_sessions(_frame()), fit_n=12, holdout_n=4, seed=7)

    assert [s.session_id for s in first.fit] == [s.session_id for s in again.fit]
    assert [s.session_id for s in first.holdout] == [s.session_id for s in again.holdout]


def test_fit_and_holdout_are_disjoint() -> None:
    """The held-out confirmation is worthless if the rule was fitted on it."""
    sample = corpus.draw_sample(corpus.eligible_sessions(_frame()), fit_n=12, holdout_n=4, seed=7)

    assert not {s.session_id for s in sample.fit} & {s.session_id for s in sample.holdout}
    assert len(sample.fit) == 12
    assert len(sample.holdout) == 4


def test_sample_spans_every_size_quartile() -> None:
    """Stratification is what makes the absolute-vs-relative question answerable; a sample bunched at the median cannot distinguish the two shapes at all."""
    sample = corpus.draw_sample(corpus.eligible_sessions(_frame()), fit_n=12, holdout_n=4, seed=7)

    assert {s.quartile for s in sample.fit} == {1, 2, 3, 4}


def test_oversized_request_is_refused_not_silently_truncated() -> None:
    """Asking for more sessions than the frame holds must fail loudly: a run that silently returns fewer reports a sample size it did not measure."""
    with pytest.raises(ValueError, match="only 40 eligible"):
        corpus.draw_sample(corpus.eligible_sessions(_frame()), fit_n=60, holdout_n=12, seed=7)


# ── Arms ────────────────────────────────────────────────────────────────────


def test_arms_are_structural_not_a_global_token_budget() -> None:
    """ADR-0124 D3 bounds a rendered token count; the KG destination stores a JSON record and constrains shape instead.

    Arms carry the structural pair.
    """
    bounded = [a for a in arms.ARMS if not a.unbounded]

    assert bounded, "expected bounded arms"
    for arm in bounded:
        assert arm.max_items_per_slot > 0
        assert arm.max_tokens_per_item > 0


def test_unbounded_arm_removes_the_instruction_rather_than_widening_it() -> None:
    """A very large number is still an instruction.

    The unbounded arm measures what
    the generator writes when nothing constrains it, so the rule has to leave.
    """
    unbounded = [a for a in arms.ARMS if a.unbounded]

    assert len(unbounded) == 1
    prompt = arms.system_prompt_for(unbounded[0])
    assert "LENGTH" not in prompt
    assert "LIMITS" not in prompt


def test_bounded_arm_prompt_states_its_own_limits() -> None:
    arm = next(a for a in arms.ARMS if not a.unbounded)
    prompt = arms.system_prompt_for(arm)

    assert "LIMITS" in prompt
    assert str(arm.max_items_per_slot) in prompt
    assert str(arm.max_tokens_per_item) in prompt


# ── Response schema ─────────────────────────────────────────────────────────


def test_schema_is_derived_from_the_stored_record() -> None:
    """Hand-writing the schema would let it drift from the model the graph stores.

    It carries all four slots and the label.
    """
    schema = arms.digest_response_schema()
    props = schema["properties"]

    assert set(props) == {"label", "digest"}
    slots = schema["$defs"]["ResponseDigest"]["properties"]
    assert set(slots) == {"established", "decisions", "unresolved", "corrections"}


def test_schema_never_asks_the_model_for_computed_state() -> None:
    """`as_of` is stamped by the producer from the session's own ended_at (ADR-0124 D3, compute state / generate meaning).

    A schema that asked for it would invite
    exactly the hallucinated timestamp the design excludes.
    """
    assert "as_of" not in str(arms.digest_response_schema())


# ── Token decomposition (§4.4) ──────────────────────────────────────────────


def test_decomposition_separates_content_from_structure() -> None:
    """`output/rendered` conflates envelope overhead with the model ignoring its instruction.

    Splitting the JSON's value strings from its scaffolding tells the
    two apart, which is the question FRE-993 is blocked on.
    """
    raw = (
        '{"label": "Shard triage", "digest": {"established": '
        '[{"text": "The cluster is green", "basis": "assistant_reasoning"}], '
        '"decisions": [], "unresolved": [], "corrections": []}}'
    )

    parts = arms.decompose_tokens(raw, output_tokens=200)

    assert parts.content_tokens > 0
    assert parts.structural_tokens == 200 - parts.content_tokens
    assert parts.structural_tokens > 0


def test_unparsable_output_is_reported_not_dropped() -> None:
    """Truncated rows are the majority failure in production.

    Excluding them biases
    every ratio toward successes — which is precisely how the live defect stayed
    invisible for fourteen days.
    """
    parts = arms.decompose_tokens('{"label": "cut off mid-str', output_tokens=2048)

    assert parts.unusable is True
    assert parts.content_tokens is None
    assert parts.structural_tokens is None


# ── Cost projection (§AC-6) ─────────────────────────────────────────────────


def test_projection_prices_each_stage_at_its_own_model() -> None:
    """Generation runs on Sonnet and scoring on gpt-5.

    4-mini; pricing both at one
    rate would misstate the number the owner authorises.
    """
    projection = arms.project_cost(
        generation_input_tokens=1_000_000,
        generation_output_tokens=0,
        scoring_input_tokens=1_000_000,
        scoring_output_tokens=0,
    )

    assert projection.generation_usd == pytest.approx(3.00)
    assert projection.scoring_usd == pytest.approx(0.75)
    assert projection.total_usd == pytest.approx(3.75)


def test_projection_counts_output_at_the_output_rate() -> None:
    projection = arms.project_cost(
        generation_input_tokens=0,
        generation_output_tokens=1_000_000,
        scoring_input_tokens=0,
        scoring_output_tokens=1_000_000,
    )

    assert projection.generation_usd == pytest.approx(15.00)
    assert projection.scoring_usd == pytest.approx(4.50)


# ── AC-5: the producer stays disabled ───────────────────────────────────────


def test_harness_never_calls_the_live_producer() -> None:
    """The curve is generated out of band.

    Importing `generate_session_digest` would
    route through the settings gate and the sweep's cost lane; calling it would
    re-enable the feature this study runs alongside, not on.
    """
    import inspect

    for module in (arms, corpus):
        source = inspect.getsource(module)
        assert "generate_session_digest" not in source


# ── Offline output estimate (§AC-6) ─────────────────────────────────────────


def test_output_estimate_is_measured_not_guessed() -> None:
    """The projection the owner authorises must not rest on a guessed envelope factor.

    A fully-populated payload at an arm's own caps is constructible offline,
    so the structural overhead is measured rather than assumed.
    """
    small = next(a for a in arms.ARMS if a.name == "s1x25")
    large = next(a for a in arms.ARMS if a.name == "s6x55")

    assert arms.estimate_max_output_tokens(small) < arms.estimate_max_output_tokens(large)
    # Scaffolding is never free: a compliant payload always bills more than its prose.
    assert arms.estimate_max_output_tokens(small) > small.max_items_per_slot * (
        small.max_tokens_per_item * 4
    )


def test_output_estimate_is_capped_by_the_call_ceiling() -> None:
    """No arm can bill more than the ceiling the call sets — a projection that exceeded it would be projecting spend the provider cannot produce."""
    for arm in arms.ARMS:
        assert arms.estimate_max_output_tokens(arm) <= arms.CALL_OUTPUT_CEILING


def test_frame_excludes_documents_the_capture_model_cannot_parse() -> None:
    """1,169 of 2,787 live capture documents carry no `user_id`, which TaskCapture requires.

    A session drawn from that era arrives with part of its transcript
    missing while the read still looks healthy — and the curve would then charge the
    bound for conclusions the generator was never shown.
    """
    query = corpus.frame_query()["query"]

    assert {"exists": {"field": "user_id"}} in query["bool"]["filter"]
