"""FRE-994 digest compression curve harness.

Covers the parts that must be right *before* any spend: the sampling frame, the arm
table, the production contract the arms send, the token decomposition that separates
envelope overhead from instruction-following failure, and the cost projection the owner
authorises the run against.

The paid stages (generation, extraction, judging) are exercised here only through
their pure helpers — the calls themselves are Phase B/C and are not made by any
test.
"""

# ruff: noqa: D103

from __future__ import annotations

import pytest
from scripts.eval.fre994_digest_compression_curve import arms, corpus

from personal_agent.memory.session_digest_wire import DIGEST_TOOL_NAME

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
    first = corpus.draw_sample(corpus.eligible_sessions(_frame()), n=16, seed=7)
    again = corpus.draw_sample(corpus.eligible_sessions(_frame()), n=16, seed=7)

    assert [s.session_id for s in first.sessions] == [s.session_id for s in again.sessions]


def test_a_larger_draw_extends_a_smaller_one_rather_than_replacing_it() -> None:
    """The budget affords a precommitted N with an extension if measured spend leaves room.

    That extension is only honest if the larger draw is the smaller draw plus more —
    otherwise "we extended the sample" is really "we redrew it after seeing results".
    """
    small = corpus.draw_sample(corpus.eligible_sessions(_frame()), n=12, seed=7)
    large = corpus.draw_sample(corpus.eligible_sessions(_frame()), n=24, seed=7)

    assert [s.session_id for s in large.sessions][:12] == [s.session_id for s in small.sessions]


def test_sample_spans_every_size_quartile() -> None:
    """Stratification is what makes the absolute-vs-relative question answerable; a sample bunched at the median cannot distinguish the two shapes at all."""
    sample = corpus.draw_sample(corpus.eligible_sessions(_frame()), n=16, seed=7)

    assert {s.quartile for s in sample.sessions} == {1, 2, 3, 4}


def test_oversized_request_is_refused_not_silently_truncated() -> None:
    """Asking for more sessions than the frame holds must fail loudly: a run that silently returns fewer reports a sample size it did not measure."""
    with pytest.raises(ValueError, match="only 40 eligible"):
        corpus.draw_sample(corpus.eligible_sessions(_frame()), n=72, seed=7)


def test_frame_excludes_documents_the_capture_model_cannot_parse() -> None:
    """`TaskCapture` requires `user_id`, so a document without it silently shortens the transcript while the read still reports itself complete.

    Master's 2026-07-26 cleanup emptied that class, but the filter stays: the
    failure it prevents is invisible, so its absence would not be noticed.
    """
    query = corpus.frame_query()["query"]

    assert {"exists": {"field": "user_id"}} in query["bool"]["filter"]


# ── Arms ────────────────────────────────────────────────────────────────────


def test_the_curve_varies_the_prompts_stated_token_policy() -> None:
    """FRE-996 §5 measured that per-slot item ceilings move rendered length by three tokens, because item text is unbounded and the schema dialect has no `maxLength`.

    The prompt's LENGTH rule is the only lever left, so it is the one the curve moves.
    """
    curve = [a for a in arms.ARMS if not a.unbounded and not a.bounded_schema]

    assert len(curve) >= 3, "a curve needs at least three points"
    for arm in curve:
        prompt = arms.system_prompt_for(arm)
        assert str(arm.max_tokens) in prompt
        assert str(arm.target_tokens) in prompt


def test_the_deployed_policy_is_one_of_the_arms() -> None:
    """A curve that does not contain today's setting cannot say whether today's setting is wrong — it can only describe alternatives to something it never measured."""
    incumbent = arms.ARMS_BY_NAME["t250"]

    assert incumbent.max_tokens == 250
    assert incumbent.target_tokens == 180


def test_unbounded_arm_removes_the_instruction_rather_than_widening_it() -> None:
    """A very large number is still an instruction.

    The unbounded arm measures what the generator writes when nothing constrains
    it — it is the reference the loss endpoint is differenced against — so the
    rule has to leave the prompt entirely.
    """
    unbounded = [a for a in arms.ARMS if a.unbounded]

    assert len(unbounded) == 1
    assert "LENGTH" not in arms.system_prompt_for(unbounded[0])


def test_arms_send_the_production_contract_not_a_local_copy() -> None:
    """FRE-996 shipped the wire contract and the producer sends it on every call.

    A schema declared in the harness would calibrate a contract that is not
    deployed, and would drift from it on the next edit.
    """
    arm = arms.ARMS_BY_NAME["t250"]
    tools, choice = arms.tools_for(arm)

    assert tools[0]["function"]["name"] == DIGEST_TOOL_NAME
    assert choice["function"]["name"] == DIGEST_TOOL_NAME


def test_only_the_completion_arm_carries_per_slot_ceilings() -> None:
    """Item ceilings are the wrong lever for length and a candidate lever for completion (FRE-996 §5.1), so they belong to one arm answering that question — not to the curve, where they would confound it."""
    bounded = [a for a in arms.ARMS if a.bounded_schema]

    assert [a.name for a in bounded] == ["t250_bounded"]
    # Same stated length policy as the incumbent, so the contrast isolates the schema.
    assert bounded[0].max_tokens == arms.ARMS_BY_NAME["t250"].max_tokens
    assert "maxItems" in str(arms.tools_for(bounded[0])[0])
    assert "maxItems" not in str(arms.tools_for(arms.ARMS_BY_NAME["t250"])[0])


def test_the_prompt_never_asks_the_model_for_computed_state() -> None:
    """`as_of` is stamped by the producer from the session's own ended_at (ADR-0124 D3, compute state / generate meaning).

    A contract that asked for it would invite exactly the hallucinated timestamp
    the design excludes.
    """
    tools, _ = arms.tools_for(arms.ARMS_BY_NAME["t250"])

    assert "as_of" not in str(tools)


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

    assert parts.content_tokens is not None
    assert parts.content_tokens > 0
    assert parts.structural_tokens == 200 - parts.content_tokens


def test_unparsable_output_is_reported_not_dropped() -> None:
    """Truncated rows are the majority failure in production.

    Excluding them biases every ratio toward successes — which is precisely how
    the live defect stayed invisible for fourteen days.
    """
    parts = arms.decompose_tokens('{"label": "cut off mid-str', output_tokens=2048)

    assert parts.unusable is True
    assert parts.content_tokens is None
    assert parts.structural_tokens is None


# ── Cost projection (AC-6) ──────────────────────────────────────────────────


def test_projection_prices_each_stage_at_its_own_model() -> None:
    """Generation runs on Sonnet and scoring on a cross-family mini; pricing both at one rate would misstate the number the owner authorises."""
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


def test_input_projection_corrects_the_estimator_and_adds_the_tool_definition() -> None:
    """Rev 2 of this plan priced the run on the cl100k estimate alone and under-stated billed input by roughly a third.

    The estimator is systematically low against Anthropic's tokeniser (measured
    1.535× over FRE-996's 30 sessions) and the contract's tool definition adds a
    further 1,663 tokens to every single call — neither is optional to count.
    """
    billed = arms.projected_input_tokens(10_000)

    assert billed == round(10_000 * arms.PROVIDER_TOKEN_RATIO) + arms.TOOL_DEFINITION_TOKENS
    assert billed > 10_000 * 1.5


def test_output_projection_rises_with_the_stated_bound() -> None:
    """The projection has to respond to the knob the arms move, or it is pricing something other than the run."""
    assert arms.projected_output_tokens(arms.ARMS_BY_NAME["t120"]) < arms.projected_output_tokens(
        arms.ARMS_BY_NAME["t400"]
    )


def test_output_projection_is_an_upper_bound_not_a_median() -> None:
    """A median-priced run overspends half the time.

    FRE-996 measured a rendered p90 of 341–389 against a stated 250, so the
    projection prices the overshoot rather than the typical case.
    """
    t250 = arms.ARMS_BY_NAME["t250"]

    # FRE-996's largest observed contract output at this policy was 1,050 tokens.
    assert arms.projected_output_tokens(t250) >= 1_000


def test_output_projection_is_capped_by_the_call_ceiling() -> None:
    """No arm can bill more than the ceiling the call sets — a projection that exceeded it would be projecting spend the provider cannot produce."""
    for arm in arms.ARMS:
        assert arms.projected_output_tokens(arm) <= arms.CALL_OUTPUT_CEILING


# ── AC-5: the producer stays disabled ───────────────────────────────────────


def test_harness_never_calls_the_live_producer() -> None:
    """The curve is generated out of band.

    Importing `generate_session_digest` would route through the settings gate and
    the sweep's cost lane; calling it would re-enable the feature this study runs
    alongside, not on.
    """
    import inspect

    for module in (arms, corpus):
        source = inspect.getsource(module)
        assert "generate_session_digest" not in source
