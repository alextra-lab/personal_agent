"""Tests for the turn evidence contract primitives (ADR-0125 D3 items 5 and 6).

These assert the *outcome* the ADR's AC-3 demands — that the record names the memory
items actually admitted to the final serialized model input, distinguishes items the
budget or the renderer dropped, and never silently omits a record — rather than that
the pieces are wired together.
"""

from __future__ import annotations

import pytest

from personal_agent.captains_log.turn_evidence import (
    EVIDENCE_RECORD_KEYS,
    CandidatePopulation,
    CandidateSource,
    DropReason,
    EvidenceState,
    InlineOutcome,
    MemoryItemKind,
    RecallAdmissionRecord,
    RecallCandidateRecord,
    build_recall_candidates,
    build_turn_evidence,
    derive_evidence_presence,
    memory_item_identity,
)

TURN_CONTEXT_OPEN = "<turn_context>"


def _wire(fenced: bool = True, extra: list[dict] | None = None) -> list[dict]:
    """Build a minimal wire-form message list, optionally carrying the volatile fence."""
    user_content = f"{TURN_CONTEXT_OPEN}\nblock\n</turn_context>\n\nhello" if fenced else "hello"
    return [
        {"role": "system", "content": "sys"},
        *(extra or []),
        {
            "role": "user",
            "content": user_content,
            "trace_id": "t-cur",
            "timestamp": "2026-07-27T10:00:00Z",
        },
    ]


def _evidence(
    candidates,
    *,
    memory_context_present: bool = True,
    rendered: tuple[str, ...] = (),
    inline_outcome: InlineOutcome = InlineOutcome.INLINED,
    session_facts_injected: bool = False,
    wire: list[dict] | None = None,
    skill_bodies: tuple[str, ...] = (),
    candidate_population: CandidatePopulation = CandidatePopulation.POST_SELECTION,
):
    return build_turn_evidence(
        candidates=candidates,
        memory_context_present=memory_context_present,
        rendered_identities=rendered,
        inline_outcome=inline_outcome,
        session_facts_injected=session_facts_injected,
        wire_messages=wire if wire is not None else _wire(),
        system_prompt="sys",
        user_message="hello",
        skill_bodies=skill_bodies,
        call_index=0,
        primary_call_count=1,
        candidate_population=candidate_population,
    )


def _entity(name: str, description: str = "d") -> dict:
    return {"type": "entity", "name": name, "entity_type": "PERSON", "description": description}


def _entity_candidate(name: str, score: float | None = None) -> RecallCandidateRecord:
    """A plain surviving entity candidate — no producer-side drop."""
    return RecallCandidateRecord(kind=MemoryItemKind.ENTITY, identity=name, score=score)


# ── 1. identity ────────────────────────────────────────────────────────────────


class TestMemoryItemIdentity:
    """`memory_item_identity` is the single definition of identity for a memory item."""

    def test_entity_identity_is_the_name(self) -> None:
        assert memory_item_identity(_entity("Paris")) == (MemoryItemKind.ENTITY, "Paris")

    def test_declared_episode_prefers_conversation_id(self) -> None:
        item = {"type": "episode", "conversation_id": "turn-7", "turn_id": "turn-9"}
        assert memory_item_identity(item) == (MemoryItemKind.EPISODE, "turn-7")

    def test_declared_session_uses_session_id(self) -> None:
        item = {"type": "session", "session_id": "sess-3", "summary": "x"}
        assert memory_item_identity(item) == (MemoryItemKind.SESSION, "sess-3")

    def test_undeclared_shape_from_executor_entity_match_path(self) -> None:
        """executor.py:3437 emits conversation_id with no ``type`` key."""
        item = {"conversation_id": "turn-4", "summary": "s", "key_entities": []}
        assert memory_item_identity(item) == (MemoryItemKind.EPISODE, "turn-4")

    def test_unrecognised_shape_is_unknown_never_guessed(self) -> None:
        assert memory_item_identity({"nothing": "useful"}) == (MemoryItemKind.UNKNOWN, "")

    def test_non_mapping_is_unknown(self) -> None:
        assert memory_item_identity("not a dict") == (MemoryItemKind.UNKNOWN, "")

    def test_stance_identity_is_namespaced_by_target(self) -> None:
        """ADR-0126 T1 (FRE-1015): a stance's identity is 'stance:{target}', not the bare
        target name. A stance and its own target entity intentionally describe the same
        World concept, so a bare-name identity would collide in the admission record's
        rendered-budget counter (turn_evidence.py's _resolve_admission keys by identity
        string alone) -- one item's render could then satisfy the other's admission check.
        """
        item = {"type": "stance", "target": "Python", "affect": "prefers over Java"}
        assert memory_item_identity(item) == (MemoryItemKind.STANCE, "stance:Python")

    def test_stance_and_entity_sharing_a_target_have_distinct_identities(self) -> None:
        entity_kind, entity_id = memory_item_identity(_entity("Python"))
        stance_kind, stance_id = memory_item_identity(
            {"type": "stance", "target": "Python", "affect": "prefers over Java"}
        )
        assert (entity_kind, entity_id) != (stance_kind, stance_id)
        assert entity_id != stance_id

    def test_stance_with_blank_target_is_unguessed_empty_identity(self) -> None:
        item = {"type": "stance", "target": "   ", "affect": "prefers over Java"}
        assert memory_item_identity(item) == (MemoryItemKind.STANCE, "")


# ── 2-5. AC-3: admitted vs dropped ────────────────────────────────────────────


class TestAdmissionAgainstFinalInput:
    """AC-3: the record names the admitted set, not the candidate set, not a count."""

    def test_budget_trimmed_candidates_are_recorded_as_dropped(self) -> None:
        """The candidate set is deliberately larger than the admitted set.

        This is AC-3's own check: force budget trimming, then require exact set
        equality with the admitted set and require the dropped items to still be
        named — a count or a boolean cannot express this.
        """
        candidates = build_recall_candidates(
            [_entity("Paris"), _entity("Berlin"), _entity("Rome")], {}
        )
        ev = _evidence(candidates, memory_context_present=False)

        assert {i.identity for i in ev.recall.items if i.admitted} == set()
        assert {i.identity for i in ev.recall.items} == {"Paris", "Berlin", "Rome"}
        assert all(i.drop_reason is DropReason.BUDGET_TRIMMED for i in ev.recall.items)
        assert ev.recall.candidate_count == 3
        assert ev.recall.admitted_count == 0

    def test_admitted_set_equals_rendered_set_exactly(self) -> None:
        candidates = build_recall_candidates([_entity("Paris"), _entity("Berlin")], {})
        ev = _evidence(candidates, rendered=("Paris",))

        assert {i.identity for i in ev.recall.items if i.admitted} == {"Paris"}
        dropped = {i.identity: i.drop_reason for i in ev.recall.items if not i.admitted}
        assert dropped == {"Berlin": DropReason.NOT_RENDERED}

    def test_rank_cap_drops_the_sixteenth_entity_distinguishably(self) -> None:
        """executor.py:2112 renders the first 15 entities only."""
        items = [_entity(f"E{i}") for i in range(16)]
        candidates = build_recall_candidates(items, {})
        ev = _evidence(candidates, rendered=tuple(f"E{i}" for i in range(15)))

        assert ev.recall.admitted_count == 15
        last = next(i for i in ev.recall.items if i.identity == "E15")
        assert last.admitted is False
        assert last.drop_reason is DropReason.NOT_RENDERED

    def test_blank_description_entity_is_dropped_not_missing(self) -> None:
        """executor.py:2112 silently drops blank-description entities today."""
        candidates = build_recall_candidates([_entity("Paris"), _entity("Ghost", "")], {})
        ev = _evidence(candidates, rendered=("Paris",))

        ghost = next(i for i in ev.recall.items if i.identity == "Ghost")
        assert ghost.admitted is False
        assert ghost.drop_reason is DropReason.NOT_RENDERED

    def test_rendered_but_never_inlined_is_not_admitted(self) -> None:
        """The block was built but the inliner had no target (executor.py:1219)."""
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(
            candidates,
            rendered=("Paris",),
            inline_outcome=InlineOutcome.NO_TARGET,
            wire=_wire(fenced=False),
        )

        item = ev.recall.items[0]
        assert item.admitted is False
        assert item.drop_reason is DropReason.ABSENT_FROM_FINAL_INPUT

    def test_inlined_but_fence_absent_from_wire_is_not_admitted(self) -> None:
        """A structural check on the wire form, not a search of rendered content."""
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(candidates, rendered=("Paris",), wire=_wire(fenced=False))

        assert ev.recall.items[0].admitted is False
        assert ev.recall.items[0].drop_reason is DropReason.ABSENT_FROM_FINAL_INPUT

    def test_already_wrapped_at_call_zero_means_our_block_did_not_land(self) -> None:
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(
            candidates, rendered=("Paris",), inline_outcome=InlineOutcome.ALREADY_WRAPPED
        )

        assert ev.recall.items[0].drop_reason is DropReason.ABSENT_FROM_FINAL_INPUT


# ── 6. scores ──────────────────────────────────────────────────────────────────


class TestScores:
    """AC-3 requires identities *with their scores* — context.py:194 discards them today."""

    def test_scores_survive_into_the_record(self) -> None:
        candidates = build_recall_candidates([_entity("Paris"), _entity("Berlin")], {"Paris": 0.82})
        ev = _evidence(candidates, rendered=("Paris", "Berlin"))

        by_id = {i.identity: i.score for i in ev.recall.items}
        assert by_id["Paris"] == pytest.approx(0.82)
        assert by_id["Berlin"] is None


# ── 7. item 6 ──────────────────────────────────────────────────────────────────


class TestAssembledContextRecord:
    """Item 6 at item-identity granularity, not the nine-category taxonomy."""

    def test_records_specific_turns_and_specific_skills(self) -> None:
        prior = {
            "role": "assistant",
            "content": "earlier answer",
            "trace_id": "t-prev",
            "timestamp": "2026-07-27T09:00:00Z",
        }
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(
            candidates,
            rendered=("Paris",),
            wire=_wire(extra=[prior]),
            skill_bodies=("bash", "query-elasticsearch"),
        )
        ac = ev.assembled_context

        assert ac.state is EvidenceState.PRESENT
        assert [m.origin_trace_id for m in ac.conversation_slice] == [None, "t-prev", "t-cur"]
        assert [m.role for m in ac.conversation_slice] == ["system", "assistant", "user"]
        assert ac.skill_bodies == ["bash", "query-elasticsearch"]
        assert ac.memory_identities == ["Paris"]
        assert ac.message_count == 3
        assert ac.system_prompt_chars == 3

    def test_resolves_finer_than_the_nine_category_prompt_taxonomy(self) -> None:
        """The FRE-1000 finding: a longer checklist of categories is the wrong shape.

        The discriminating property is *resolution*, not vocabulary. The existing
        taxonomy can only ever say "memory_section was present" and "skill_bodies were
        present" — one flag each, no matter how much was admitted. So the test feeds a
        turn that admits two distinct memory items, two distinct skills and two distinct
        prior turns, and requires the record to separate them. A category checklist
        collapses each of those pairs to a single flag and cannot pass this.
        """
        from personal_agent.llm_client.prompt_identity import PROMPT_COMPONENT_TAXONOMY

        prior_a = {"role": "assistant", "content": "a", "trace_id": "t-a"}
        prior_b = {"role": "assistant", "content": "b", "trace_id": "t-b"}
        candidates = build_recall_candidates([_entity("Paris"), _entity("Berlin")], {})
        ev = _evidence(
            candidates,
            rendered=("Paris", "Berlin"),
            wire=_wire(extra=[prior_a, prior_b]),
            skill_bodies=("bash", "query-elasticsearch"),
        )
        ac = ev.assembled_context

        # Resolution: each pair is separated, where a category flag would collapse it.
        assert ac.memory_identities == ["Paris", "Berlin"]
        assert ac.skill_bodies == ["bash", "query-elasticsearch"]
        assert [m.origin_trace_id for m in ac.conversation_slice if m.origin_trace_id] == [
            "t-a",
            "t-b",
            "t-cur",
        ]
        # And the record is made of instance identities, not category labels.
        assert not ({*ac.memory_identities, *ac.skill_bodies} & set(PROMPT_COMPONENT_TAXONOMY))
        assert "memory_section" not in ac.memory_identities

    def test_chars_counts_block_content_without_crashing(self) -> None:
        wire = [{"role": "user", "content": [{"type": "text", "text": "abcd"}]}]
        ev = _evidence(build_recall_candidates([], {}), wire=wire)
        assert ev.assembled_context.conversation_slice[0].chars == 4


# ── session facts ─────────────────────────────────────────────────────────────


class TestSessionFactCandidates:
    """context.py:327-337 injects recall-controller facts bypassing memory_context."""

    def test_injected_session_facts_are_admitted(self) -> None:
        cands = (
            RecallCandidateRecord(
                kind=MemoryItemKind.SESSION_FACT,
                identity="turn:3",
                score=0.7,
                source=CandidateSource.SESSION_FACT_SECTION,
            ),
        )
        ev = _evidence(cands, session_facts_injected=True)

        assert ev.recall.items[0].admitted is True
        assert ev.recall.items[0].score == pytest.approx(0.7)

    def test_uninjected_session_facts_are_dropped_not_omitted(self) -> None:
        cands = (
            RecallCandidateRecord(
                kind=MemoryItemKind.SESSION_FACT,
                identity="turn:3",
                score=0.7,
                source=CandidateSource.SESSION_FACT_SECTION,
            ),
        )
        ev = _evidence(cands, session_facts_injected=False)

        assert ev.recall.items[0].admitted is False
        assert ev.recall.items[0].drop_reason is DropReason.NOT_RENDERED

    def test_session_facts_are_unaffected_by_memory_context_budget_drop(self) -> None:
        """They ride a system message, which budget trimming preserves."""
        cands = (
            RecallCandidateRecord(
                kind=MemoryItemKind.SESSION_FACT,
                identity="turn:3",
                score=None,
                source=CandidateSource.SESSION_FACT_SECTION,
            ),
        )
        ev = _evidence(cands, memory_context_present=False, session_facts_injected=True)

        assert ev.recall.items[0].admitted is True


# ── 8. explicit absence ───────────────────────────────────────────────────────


class TestExplicitAbsence:
    """ADR-0125 D3: an implicitly missing field is indistinguishable from a capture gap."""

    def _presence(self, **overrides):
        kwargs = {
            "user_message": "hi",
            "assistant_response": "hello",
            "tool_results": [],
            "llm_call_count": 1,
            "turn_evidence": _evidence(
                build_recall_candidates([_entity("Paris")], {}), rendered=("Paris",)
            ),
            "trace_id": "t",
            "session_id": "s",
            "user_id": "u",
        }
        kwargs.update(overrides)
        return derive_evidence_presence(**kwargs)

    def test_all_eight_records_are_always_marked(self) -> None:
        presence = self._presence()
        assert set(presence) == set(EVIDENCE_RECORD_KEYS)
        assert len(EVIDENCE_RECORD_KEYS) == 8

    def test_no_tool_called_is_empty_not_missing(self) -> None:
        assert self._presence(tool_results=[])["tool_calls"] is EvidenceState.EMPTY

    def test_tool_called_is_present(self) -> None:
        assert self._presence(tool_results=[{"tool_name": "bash"}])["tool_calls"] is (
            EvidenceState.PRESENT
        )

    def test_empty_is_never_equal_to_not_recorded(self) -> None:
        """The discriminator the contract exists to provide."""
        no_tools = self._presence(tool_results=[])["tool_calls"]
        never_built = self._presence(turn_evidence=None)["assembled_context"]
        assert no_tools is EvidenceState.EMPTY
        assert never_built is EvidenceState.NOT_RECORDED
        assert no_tools != never_built

    def test_reasoning_trace_is_an_explicit_gap(self) -> None:
        """TaskCapture has no reasoning field; item 3 is out of scope for FRE-1004.

        Marking it not_recorded makes the gap machine-visible rather than implicit.
        """
        assert self._presence()["reasoning_trace"] is EvidenceState.NOT_RECORDED

    def test_recall_states_distinguish_nothing_found_from_never_run(self) -> None:
        found_nothing = _evidence(build_recall_candidates([], {}))
        assert self._presence(turn_evidence=found_nothing)["recalled_memory"] is (
            EvidenceState.EMPTY
        )
        assert self._presence(turn_evidence=None)["recalled_memory"] is (EvidenceState.NOT_RECORDED)

    def test_missing_identifier_is_flagged(self) -> None:
        assert self._presence(user_id=None)["identifiers"] is EvidenceState.NOT_RECORDED
        assert self._presence()["identifiers"] is EvidenceState.PRESENT

    def test_no_model_call_is_empty(self) -> None:
        assert self._presence(llm_call_count=0)["model_and_params"] is EvidenceState.EMPTY
        assert self._presence(llm_call_count=2)["model_and_params"] is EvidenceState.PRESENT

    def test_absent_assistant_response_is_empty(self) -> None:
        assert self._presence(assistant_response=None)["assistant_response"] is (
            EvidenceState.EMPTY
        )


# ── the record names the call it describes ────────────────────────────────────


class TestRecordCoherence:
    def test_both_halves_describe_the_same_named_call(self) -> None:
        ev = _evidence(build_recall_candidates([_entity("Paris")], {}), rendered=("Paris",))
        assert ev.primary_call_index == 0
        assert ev.primary_call_count == 1
        assert ev.assembled_context.memory_identities == [
            i.identity for i in ev.recall.items if i.admitted
        ]


# ── durability: the record must survive both sinks ────────────────────────────


class TestCaptureDurability:
    """The record is worthless if it does not survive the disk and ES paths."""

    def _capture(self, **overrides):
        from datetime import datetime, timezone
        from uuid import uuid4

        from personal_agent.captains_log.capture import TaskCapture

        ev = _evidence(
            build_recall_candidates([_entity("Paris")], {"Paris": 0.9}), rendered=("Paris",)
        )
        kwargs = {
            "trace_id": "t-1",
            "session_id": "s-1",
            "timestamp": datetime.now(timezone.utc),
            "user_message": "hi",
            "assistant_response": "hello",
            "outcome": "completed",
            "user_id": uuid4(),
            "recall_admission": ev.recall,
            "assembled_context": ev.assembled_context,
            "evidence_presence": derive_evidence_presence(
                user_message="hi",
                assistant_response="hello",
                tool_results=[],
                llm_call_count=1,
                turn_evidence=ev,
                trace_id="t-1",
                session_id="s-1",
                user_id="u",
            ),
        }
        kwargs.update(overrides)
        return TaskCapture(**kwargs)

    def test_survives_the_disk_write_serialization(self) -> None:
        """write_capture uses orjson over a python-mode model_dump."""
        import orjson

        capture = self._capture()
        payload = orjson.loads(orjson.dumps(capture.model_dump()))

        assert payload["recall_admission"]["items"][0]["identity"] == "Paris"
        assert payload["recall_admission"]["items"][0]["score"] == pytest.approx(0.9)
        assert payload["recall_admission"]["items"][0]["admitted"] is True
        assert payload["assembled_context"]["memory_identities"] == ["Paris"]
        assert payload["evidence_presence"]["reasoning_trace"] == "not_recorded"

    def test_survives_the_es_json_mode_dump_and_normalizer(self) -> None:
        from personal_agent.captains_log.es_indexer import normalize_capture_doc_for_es

        capture = self._capture()
        doc = normalize_capture_doc_for_es(capture.model_dump(mode="json"))

        assert doc["assembled_context"]["state"] == "present"
        assert doc["recall_admission"]["state"] == "present"
        assert set(doc["evidence_presence"]) == set(EVIDENCE_RECORD_KEYS)

    def test_round_trips_back_into_the_model(self) -> None:
        import orjson

        from personal_agent.captains_log.capture import TaskCapture

        capture = self._capture()
        restored = TaskCapture(**orjson.loads(orjson.dumps(capture.model_dump(mode="json"))))

        assert restored.recall_admission is not None
        assert restored.recall_admission.items[0].identity == "Paris"
        assert restored.assembled_context is not None
        assert restored.evidence_presence["tool_calls"] is EvidenceState.EMPTY

    def test_legacy_capture_without_the_fields_still_reads(self) -> None:
        """Pre-FRE-1004 files on disk carry none of these keys."""
        from datetime import datetime, timezone
        from uuid import uuid4

        from personal_agent.captains_log.capture import TaskCapture

        legacy = TaskCapture(
            trace_id="t",
            session_id="s",
            timestamp=datetime.now(timezone.utc),
            user_message="hi",
            outcome="completed",
            user_id=uuid4(),
        )
        assert legacy.recall_admission is None
        assert legacy.assembled_context is None
        assert legacy.evidence_presence == {}


class TestFenceScopingCannotOverClaim:
    """A previous turn's fence must never stand in for this turn's block."""

    def test_prior_turn_fence_does_not_make_this_turn_admitted(self) -> None:
        """The sanitiser can drop back to an earlier user turn on an orphaned history.

        If that happens the current block never reached the model, and the earlier
        turn's own fence must not be mistaken for it.
        """
        prior_fenced = {
            "role": "user",
            "content": f"{TURN_CONTEXT_OPEN}\nlast week's memory\n</turn_context>\n\nolder question",
            "trace_id": "t-old",
        }
        wire = [
            {"role": "system", "content": "sys"},
            prior_fenced,
            {"role": "assistant", "content": "older answer"},
        ]
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(candidates, rendered=("Paris",), wire=wire)

        assert ev.recall.items[0].admitted is False
        assert ev.recall.items[0].drop_reason is DropReason.ABSENT_FROM_FINAL_INPUT
        assert ev.assembled_context.memory_identities == []

    def test_current_turn_fence_on_the_last_user_message_admits(self) -> None:
        prior_plain = {"role": "user", "content": "older question", "trace_id": "t-old"}
        wire = [
            {"role": "system", "content": "sys"},
            prior_plain,
            {"role": "assistant", "content": "older answer"},
            {
                "role": "user",
                "content": f"{TURN_CONTEXT_OPEN}\nmem\n</turn_context>\n\nhello",
                "trace_id": "t-cur",
            },
        ]
        candidates = build_recall_candidates([_entity("Paris")], {})
        ev = _evidence(candidates, rendered=("Paris",), wire=wire)

        assert ev.recall.items[0].admitted is True


class TestCollidingIdentitiesCannotOverClaim:
    """Two candidates sharing an identity must not both ride one render."""

    def test_only_as_many_as_were_rendered_are_admitted(self) -> None:
        """Five anonymous episodes, three rendered — two must still read as dropped.

        The realistic instance: a producer that supplies no identity yields the empty
        identity for every item, so a plain set-membership test would admit all five as
        soon as one rendered, hiding the render cap entirely.
        """
        anonymous = [{"type": "episode", "summary": f"s{i}"} for i in range(5)]
        candidates = build_recall_candidates(anonymous, {})
        assert {c.identity for c in candidates} == {""}

        ev = _evidence(candidates, rendered=("", "", ""))

        assert ev.recall.admitted_count == 3
        assert [i.admitted for i in ev.recall.items] == [True, True, True, False, False]
        assert all(
            i.drop_reason is DropReason.NOT_RENDERED for i in ev.recall.items if not i.admitted
        )

    def test_two_entities_sharing_a_name_are_resolved_one_for_one(self) -> None:
        candidates = build_recall_candidates([_entity("Python"), _entity("Python")], {})
        ev = _evidence(candidates, rendered=("Python",))

        assert [i.admitted for i in ev.recall.items] == [True, False]
        assert ev.recall.items[1].drop_reason is DropReason.NOT_RENDERED


class TestSkillBodiesAreGatedOnTheBlockLanding:
    """Skill bodies ride the same volatile block as the memory section."""

    def test_not_listed_when_the_block_never_landed(self) -> None:
        """A vision turn's user content is a block list, which the inliner declines."""
        ev = _evidence(
            build_recall_candidates([], {}),
            inline_outcome=InlineOutcome.NO_TARGET,
            wire=_wire(fenced=False),
            skill_bodies=("bash", "query-elasticsearch"),
        )
        assert ev.assembled_context.skill_bodies == []

    def test_listed_when_the_block_landed(self) -> None:
        ev = _evidence(
            build_recall_candidates([], {}),
            skill_bodies=("bash", "query-elasticsearch"),
        )
        assert ev.assembled_context.skill_bodies == ["bash", "query-elasticsearch"]


class TestPreDroppedCandidates:
    """FRE-1060 — a candidate its own producer discarded before context assembly.

    The proactive recall path applies eight gates of its own and used to return only
    their survivors, so those losses were absences rather than drops with a reason. A
    pre-dropped candidate arrives already resolved, and the one thing that must not
    happen is for it to consume a rendered slot it never occupied.
    """

    def test_the_pre_drop_reason_is_reported_verbatim(self) -> None:
        """The gate the producer named is the gate the record states."""
        candidates = (
            _entity_candidate("Paris"),
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Melon",
                score=0.563,
                pre_drop_reason=DropReason.RECALL_ITEM_CAP,
            ),
        )

        ev = _evidence(candidates, rendered=("Paris",))

        assert [i.admitted for i in ev.recall.items] == [True, False]
        assert ev.recall.items[1].drop_reason is DropReason.RECALL_ITEM_CAP
        assert ev.recall.items[1].score == pytest.approx(0.563)

    def test_a_pre_drop_does_not_consume_a_rendered_slot(self) -> None:
        """The multiset hazard: a pre-drop must not steal a survivor's render slot.

        Two candidates share the identity ``Paris`` and the renderer emitted it once. If
        the pre-dropped one decremented the count, the surviving one would resolve to
        NOT_RENDERED — the record would claim the renderer dropped an item it actually
        emitted, inverting the very fact it exists to establish.
        """
        candidates = (
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Paris",
                score=0.4,
                pre_drop_reason=DropReason.RECALL_TOKEN_BUDGET,
            ),
            _entity_candidate("Paris"),
        )

        ev = _evidence(candidates, rendered=("Paris",))

        assert ev.recall.items[0].admitted is False
        assert ev.recall.items[0].drop_reason is DropReason.RECALL_TOKEN_BUDGET
        assert ev.recall.items[1].admitted is True, "the survivor keeps its rendered slot"
        assert ev.recall.admitted_count == 1

    def test_the_empty_identity_case(self) -> None:
        """The same hazard at its worst: every producer supplying no identity yields "".

        A membership test would admit all of them at once; a shared counter would let a
        pre-drop consume the single slot the renderer emitted.
        """
        candidates = (
            RecallCandidateRecord(
                kind=MemoryItemKind.UNKNOWN,
                identity="",
                pre_drop_reason=DropReason.RECALL_SCORE_THRESHOLD,
            ),
            RecallCandidateRecord(kind=MemoryItemKind.UNKNOWN, identity=""),
        )

        ev = _evidence(candidates, rendered=("",))

        assert ev.recall.items[0].drop_reason is DropReason.RECALL_SCORE_THRESHOLD
        assert ev.recall.items[1].admitted is True

    def test_a_pre_drop_stays_dropped_even_when_the_block_never_landed(self) -> None:
        """Its own gate removed it; a later failure cannot re-attribute the reason."""
        candidates = (
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Melon",
                pre_drop_reason=DropReason.RECALL_SCORE_THRESHOLD,
            ),
        )

        ev = _evidence(
            candidates,
            memory_context_present=False,
            inline_outcome=InlineOutcome.NO_TARGET,
        )

        assert ev.recall.items[0].drop_reason is DropReason.RECALL_SCORE_THRESHOLD

    def test_pre_dropped_identities_are_not_listed_as_admitted_context(self) -> None:
        """Item 6 must not claim the model was given something that never reached it."""
        candidates = (
            _entity_candidate("Paris"),
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Melon",
                pre_drop_reason=DropReason.RECALL_ITEM_CAP,
            ),
        )

        ev = _evidence(candidates, rendered=("Paris",))

        assert ev.assembled_context.memory_identities == ["Paris"]


class TestCandidatePopulationClaim:
    """FRE-1060 §2.5b — the record says whether it names the population or the survivors."""

    def test_the_conservative_claim_is_the_default(self) -> None:
        """A caller that does not state completeness must not have it assumed.

        This is also what makes historical documents read back correctly: the field is
        absent from every capture written before FRE-1060, and those records genuinely
        hold survivors only.
        """
        ev = _evidence((_entity_candidate("Paris"),), rendered=("Paris",))

        assert ev.recall.candidate_population is CandidatePopulation.POST_SELECTION

    def test_a_complete_population_can_be_declared(self) -> None:
        ev = _evidence(
            (_entity_candidate("Paris"),),
            rendered=("Paris",),
            candidate_population=CandidatePopulation.OFFERED,
        )

        assert ev.recall.candidate_population is CandidatePopulation.OFFERED

    def test_a_legacy_record_deserialises_to_the_conservative_claim(self) -> None:
        """A stored document with no such field is survivors-only, and says so."""
        legacy = RecallAdmissionRecord.model_validate(
            {"state": "present", "candidate_count": 5, "admitted_count": 5, "items": []}
        )

        assert legacy.candidate_population is CandidatePopulation.POST_SELECTION


class TestStateIsNotCollapsedByDiscards:
    """FRE-1060 — adding discards to `items` must not destroy the EMPTY signal.

    `evidence_presence.recalled_memory` inherits `recall.state`, and consumers filter it on
    EMPTY to enumerate turns where recall delivered nothing to the model — the melon-class
    failure. Keying `state` on `items` (which now holds producer discards) made EMPTY
    effectively unreachable: a turn that retrieved twelve rows and discarded all twelve read
    PRESENT. Confirmed by code review; `state` is keyed on the delivered candidates instead.
    """

    def test_all_discarded_still_reads_empty(self) -> None:
        """Twelve retrieved, twelve gated away, nothing delivered: EMPTY."""
        candidates = tuple(
            RecallCandidateRecord(
                kind=MemoryItemKind.EPISODE,
                identity=f"turn-{i}",
                score=0.2,
                pre_drop_reason=DropReason.RECALL_SCORE_THRESHOLD,
            )
            for i in range(12)
        )

        ev = _evidence(candidates, memory_context_present=False)

        assert ev.recall.state is EvidenceState.EMPTY
        assert ev.recall.candidate_count == 12, "the discards are still named"
        assert ev.recall.admitted_count == 0

    def test_the_derived_presence_key_follows(self) -> None:
        """The field consumers actually query is the one that must not lie."""
        candidates = (
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Melon",
                pre_drop_reason=DropReason.RECALL_ITEM_CAP,
            ),
        )
        ev = _evidence(candidates, memory_context_present=False)

        presence = derive_evidence_presence(
            user_message="hello",
            assistant_response="hi",
            tool_results=[],
            llm_call_count=1,
            turn_evidence=ev,
            trace_id="t",
            session_id="s",
            user_id=object(),
        )

        assert presence["recalled_memory"] is EvidenceState.EMPTY

    def test_one_delivered_candidate_is_present(self) -> None:
        """The other side of the boundary: something reached assembly, so PRESENT."""
        candidates = (
            _entity_candidate("Paris"),
            RecallCandidateRecord(
                kind=MemoryItemKind.ENTITY,
                identity="Melon",
                pre_drop_reason=DropReason.RECALL_ITEM_CAP,
            ),
        )

        ev = _evidence(candidates, rendered=("Paris",))

        assert ev.recall.state is EvidenceState.PRESENT
        assert ev.recall.admitted_count == 1

    def test_no_candidates_at_all_is_still_empty(self) -> None:
        """The pre-existing meaning is unchanged when there are no discards either."""
        assert _evidence(()).recall.state is EvidenceState.EMPTY
