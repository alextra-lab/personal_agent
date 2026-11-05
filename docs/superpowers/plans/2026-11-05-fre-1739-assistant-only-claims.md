# FRE-1739 — Drop claims that only the assistant stated

Ticket: FRE-1739 (Defect A of the FRE-1728 pilot). Backing design: ADR-0098 D2/D6 (claims, supersession, co-authorship).
Diff class: escalated (memory write path).

## Finding (AC-1 evidence)

Run `full-pilot-20261105`, user_015 (simulated user data).

1. **Where the assistant text became a user claim.** The extraction prompt receives both speakers (`User:` and
   `Assistant:`) and has no rule about who stated a fact. The model read the assistant sentence "since you're on
   Europe/Paris I'll assume a French agence under IDCC 2120" and emitted claim facet `jurisdiction_assumption`.
   `_finalize_extraction` (`second_brain/entity_extraction.py`) kept it and stamped `asserted_by="agent"`.
   Later the consolidator called `_build_claim` and `MemoryService.assert_claim`, which stored it as a current claim
   on the owner's Person node, worded "The user wants…". Nothing in the chain removes a claim.
2. **Marking the source does not work.** `query_claims` already returns `asserted_by`. The later reply still said that
   the user framed the work as French. The fix must stop the write.
3. **Why the correction did not supersede.** The correction is stored as facet `current_country` ("The user is in the
   United States.", user-grounded). The old claim has facet `jurisdiction_assumption`. The facets differ, so
   `_pair_threshold` requires cosine >= 0.95 (`DIFF_FACET_FLOOR`). Two sentences with different content do not reach it.
   The extractor receives no stored claims, so it names each fact with a new facet. The correction matched nothing and
   was stored as FRESH. The adjudication step never ran for the pair.
4. **The same stage wrote the later claims.** `jurisdiction_conflict` and `jurisdiction_status` come from episode 8.
   The simulated user said nothing about jurisdiction there. The assistant text raised it.

## Choice: drop on the extractor's speaker label

- **Mark:** rejected. A mark exists and fails (finding 2).
- **Word-overlap drop:** tried and rejected in codex plan review. Tokens are exact and not stemmed. A user paraphrase
  ("meetings" in the text, "meeting" in the claim) scored 0.25 for the user and 0.75 for the assistant, and was dropped.
  Synonyms, plurals and French have the same fault. A hard drop on a weak signal loses real user facts.
- **Speaker label (chosen):** the prompt asks for `stated_by` (`user` or `assistant`) on each claim. Python removes a
  claim labelled `assistant`. The label can only remove a claim, never raise trust, so ADR-0098 AC-9 holds. A missing or
  unknown label keeps the claim (fail open).

Cost effect: the prompt template grows by 1094 characters (about 270 tokens) per extraction call, and each claim adds about 4 output tokens. No extra call.
The prompt hash that the FRE-630 harness reads changes.

Known limit: the offline tests prove the drop, not the model's labelling. A replay of the pilot pair (episode 2, turn 2)
through the live extractor shows the labelling. That is a paid call, so it waits for master's preflight.

## Steps (done)

1. Tests first: `tests/personal_agent/memory/test_assistant_only_claims.py` (AC-2 tests failed on old code), AC-3 on the
   real writer in `test_claims_stance_storage.py` (isolated test Neo4j).
2. Implement: prompt field, rule 15, BAD example and schema in `entity_extraction.py`. `_drop_assistant_stated_claims`.
3. Gates: `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.
4. Self-review on the committed diff, then PR and handoff comment.

## Out of scope (stated in the handoff)

- Stances use the same extraction input. The same defect may apply to stance `affect`. Not in the pilot evidence.
- The number of dropped claims is the number of `claim_dropped_assistant_stated` log events. No separate counter.
- Facet drift between two user-stated claims can still block supersession. Not seen in the pilot. A fix changes
  ADR-0098 D2 matching and needs an ADR.
- The FRE-630 harness scores claim-emission recall with this prompt. It was not rerun (paid).
