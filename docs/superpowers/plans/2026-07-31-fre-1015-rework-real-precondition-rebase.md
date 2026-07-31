# FRE-1015 rework — real entity-selection precondition + rebase onto FRE-1041/1061/1062

Supersedes nothing in `2026-07-28-fre-1015-topic-scoped-stance-push.md` (the original
design stands); this is the rework master's 2026-07-31 04:30 UTC gate ordered, pushed onto
the same branch/PR (`fre-1015-topic-scoped-stance-enrichment`, PR #738 — stays open, do
NOT open a fresh branch).

## Why this bounced, restated precisely

Master's two standing blockers, and the one that's now resolved:

1. **Tautological precondition (stands).** `_stub_entity_recall()` monkeypatches
   `MemoryService.query_memory` to always return the target entity. The precondition
   helper `_assert_entity_candidacy_or_skip` then checks the admission record for that
   same entity — which is guaranteed present because the stub put it there three lines
   above. The precondition can never fail, so it can never emit INCONCLUSIVE, which is
   the one behaviour ADR-0126 D2's amendment exists to produce.
2. **65-commit drift, same-file conflicts (stands, worsened).** FRE-1041 (graph-anchored
   entity-hint resolution), FRE-1061 (entity/episode candidate split), and FRE-1062
   (episode floor + mention pins) all touch the entity-resolution path this PR's two hook
   points read from: `memory/protocol.py`, `protocol_adapter.py`, `memory/service.py`,
   `request_gateway/context.py`, `orchestrator/executor.py`.
3. **Resolved.** FRE-1041 is deployed and verified live (2026-07-30). The mention-pin
   mechanism (FRE-1062) means a turn that literally names the stance target now has a
   real (conditional, not unconditional) shot at surviving into the admitted set — see
   "What the mention-pin guarantee actually is" below. The precondition can now be
   satisfied by the *real* path rather than fabricated, so the rework is achievable.

## What actually changed on `main` (researched 2026-07-31, cited)

Contrary to what the 65-commit-behind count suggests, the actual **logical** collision is
narrow. Two of five touched files are additive-only; one file needs a real edit; two are
essentially untouched by 1041/1061/1062.

- **`captains_log/turn_evidence.py` — additive, low risk.** The branch's diff adds
  `MemoryItemKind.STANCE` and one new `if declared == "stance":` branch to
  `memory_item_identity`. Main added unrelated new `MemoryItemKind`/`DropReason` members
  and `RecallCandidateRecord.pre_drop_reason` / `RecallAdmissionRecord.candidate_population`
  fields, but never touched the stance branch (it doesn't exist there). `build_recall_candidates`
  iterates `memory_context` generically via `_candidate_for(item, score)` — a stance item
  appended to `memory_context` automatically gets a `RecallCandidateRecord` with
  `pre_drop_reason=None`, no special-casing needed. **Expect a clean textual merge.**
- **`memory/protocol.py`, `protocol_adapter.py`, `memory/service.py` — additive, low
  risk.** FRE-1015 adds `get_current_stances`/`query_current_stances` (batch
  targets→rows). FRE-1041 adds `resolve_message_entities`/`resolve_message_entity_names`
  (message→names). Different shapes, non-overlapping methods, same house style
  (async, `trace_id`+`authenticated` kwargs, fail-closed-empty-on-exception at the
  adapter). **Expect insertion-point conflicts only, no logic conflict.**
- **`request_gateway/context.py` — real edit required.** `_query_memory_for_intent` was a
  2-tuple (`context, scores`) on the branch; on `main` it is a 3-tuple
  (`context, scores, RecallDiscardReport`) because of FRE-1060's discard-naming work.
  `assemble_context()`'s call site must unpack three values, and
  `recall_candidates = (*build_recall_candidates(...), *build_discarded_candidates(...), ...)`
  now exists. **The fix:** call `_enrich_with_stances(memory_context, memory_adapter,
  trace_id, authenticated)` immediately after the 3-tuple unpack, *before*
  `recall_candidates` is built — same insertion point conceptually as before, just after
  the extra value. `_entity_names_from_memory_context` still reads `item["type"] ==
  "entity"` / `item["name"]`, which is unchanged in every `memory_context`-producing path
  on `main` (verified: entity-match fallback, `_split_row_payloads`'s entity branch, and
  broad-recall formatter all still emit that shape). `session_entity_names` at the
  `suggest_relevant` call site is now `list(entity_names)` (from `resolve_message_entities`)
  instead of `_capitalized_entity_hints(user_message)` — irrelevant to the stance hook,
  which reads `memory_context` after the fact, not the resolver's input.
- **`orchestrator/executor.py` — essentially untouched by 1041/1061/1062.** The branch's
  stance hook lives inside the **broad-recall** branch (`is_memory_recall_query`), not the
  entity-name-match branch — because only the broad-recall formatter
  (`_format_broad_recall`) emits `type: "entity"` items on this path; the entity-match
  branch emits conversation-summary dicts with no entity items at all, on `main` exactly as
  on the branch. FRE-1041/1061/1062 never touched this file. **Expect the hook to rebase
  with only line-number drift.**

## What the mention-pin guarantee actually is (do not overstate this in the tests)

`build_proactive_suggestions` (`memory/proactive.py:343-391`) pins up to
`_MENTIONED_ENTITY_PIN_LIMIT = 2` mentioned entities ahead of the rank window and the
score-floor/score-gap diminishing-returns cut. It does **not** bypass:
`proactive_memory_min_score` (filtering happens before `scored` exists), the token-budget/
oversize skip, `max_injected_items`, or prior existence as a scored candidate at all (the
entity must already be known to the graph and returned by `resolve_message_entities`).
Also: this pin mechanism only fires when `settings.proactive_memory_enabled` is `True`
(**default `False`**). The acceptance tests below run with proactive memory at its
default-disabled setting, so they exercise the **entity-name-match fallback path**
(`context.py` "Entity-name matching" branch → `memory_adapter.recall(query)`), not the
proactive/mention-pin path at all — the mention-pin discussion above explains why master's
gate comment is right that the precondition is *now satisfiable by the real path*, but the
mechanism the tests actually rely on is the simpler one: `resolve_message_entities` finds
the literally-named entity via the live `turn_entity_fulltext` index, and the fallback path
recalls it via a real (not stubbed) Cypher query. No proactive-specific fixture (session
history volume, competing episodes, embedding vectors) is required.

## The fix to the tautological test — REVISED after codex plan-review (2026-07-31)

**Codex's first-pass review (`codex:rescue`) found the version below was itself broken**
before any code was written — a real BLOCKER-class catch, not a nitpick. The section below
is the corrected design; the four findings that drove the correction are recorded after it.

Remove `_stub_entity_recall` and all `monkeypatch.setattr(service, "query_memory", ...)`
calls entirely — this part of the original plan stands. But the replacement fixture is
**not** "just create a bare `:Entity` node and let the message name it" — verified by
reading `MemoryService.query_memory`'s legacy (non-multipath) Cypher on `main`
(`memory/service.py:3796-3980`): with `multipath_recall_enabled` at its default `False`,
entity recall runs

```cypher
MATCH (c:Turn)-[:DISCUSSES]->(e:Entity) WHERE ... AND e.name IN $entity_names ...
```

— it requires a real `:Turn` connected to the entity via `:DISCUSSES`. A bare
`MERGE (:Entity {name: ...})` (the original `_create_entity` helper) has no such edge, so
`query_memory` would return zero entities for it regardless of what
`resolve_message_entities` resolves from the message text. Every positive/control case
would have silently degraded to `pytest.skip` (INCONCLUSIVE) — passing for the wrong
reason (nothing ever ran), the same failure class as the stub, just one layer further out.

**Corrected fixture: seed through the real production write path, not a hand-rolled
Cypher shape.** `MemoryService.create_conversation(TurnNode(..., key_entities=[name]),
user_id=..., visibility="group")` (`memory/service.py:1136-1296`) already does exactly
what the legacy recall query needs in one real call: `MERGE`s the `:Turn`, `MERGE`s the
`:Entity` (via the `key_entities` loop), and `MERGE`s the `:Turn-[:DISCUSSES]->:Entity`
edge — the same write path a real conversation turn uses. Replace `_create_entity` with a
`_seed_discussed_entity(service, name, *, turn_id, user_message)` helper built on this.
Order per test: seed the conversation (creates entity + Turn + DISCUSSES) → `assert_stance`
(now has a real `:Entity` to `MATCH`) → run the probe turn.

1. `_seed_discussed_entity(...)` replaces `_create_entity(...)`.
2. `assert_stance(...)` — unchanged, now `MATCH`es an entity that genuinely has recall
   provenance behind it.
3. No stub. The probe message literally names the entity — `resolve_message_entities`'s
   full-text-then-literal-mention filter resolves it for real against the live test Neo4j
   (`:7688`), and the legacy `query_memory` Cypher now has a real `DISCUSSES` edge to match.
4. `_run_turn` / `assemble_context()` unchanged — runs the real resolver → (proactive
   disabled by default) → real entity-match fallback → real `memory_adapter.recall(query)`
   → real Cypher → real stance join.
5. `_assert_entity_candidacy_or_skip` gets one addition: also require
   `item.pre_drop_reason is None` (not just kind+identity match). In this specific
   fallback-path design `discards` is structurally always `()` when
   `proactive_memory_enabled` is `False` (confirmed: `_query_memory_for_intent` only
   populates `discards` inside the `if settings.proactive_memory_enabled:` branch), so this
   can't currently fire — but it closes the real gap codex found (see Finding 2) and costs
   nothing to add now rather than after it bites a future proactive-path consumer of this
   same helper.
6. **Negative-half (AC-1), and the token-collision fix.** Use two entities with **no shared
   substring** — not `FRE1015_Python` / `FRE1015_Unrelated` sharing the `FRE1015_` prefix.
   Codex flagged (Finding 3, labeled explicitly as inference, not confirmed) that Neo4j's
   full-text analyzer tokenization of a shared prefix token could cause `resolve_message_
   entities` to surface both names off one turn's text. Rather than resolve the analyzer
   question empirically, remove the shared substring entirely — pick fully distinct opaque
   names per test. Also, **assert the unrelated entity's own candidacy before asserting the
   target's absence** — the current/old test never did this, so "target absent" was
   previously indistinguishable from "recall found nothing at all, including the control."
7. **Cleanup marker.** Distinct opaque names remove the `STARTS WITH 'FRE1015_'` cleanup
   hook the current fixture uses. Replace it with `originating_session_id` (a property
   `create_conversation` already stamps on both the `:Turn` and every `:Entity` it touches)
   set to a fixed test marker, e.g. `"fre1015-seed"` — cleanup becomes
   `MATCH (n) WHERE n.originating_session_id = 'fre1015-seed' DETACH DELETE n`, covering
   both node types from one real write path instead of a name-prefix scan.
8. **Index readiness (Finding 4).** Do not assume the test Neo4j substrate already created
   `turn_entity_fulltext`. Call `await owner_service.ensure_fulltext_index()`
   (`memory/service.py:3130`, idempotent — `CREATE FULLTEXT INDEX ... IF NOT EXISTS`) once
   in the `owner_service` fixture, right after `connect()`. Do not add sleeps or retries
   around it — if the resolver still can't see a freshly-seeded entity after this, that is
   a real finding to investigate (see Risks), not something to paper over with timing hacks.

This changes the module docstring's "Deterministic entity recall, real stance retrieval"
framing: it becomes "real entity recall through the real Turn-DISCUSSES-Entity write path
and the live full-text resolver, real stance retrieval" — update the docstring accordingly.

## Codex plan-review findings that drove the revision above (verdict: needed another pass)

1. **BLOCKER** — under the test's default config (multipath off), the original "just
   create a bare entity" fallback design could never produce an entity candidate at all
   (no `:Turn-[:DISCUSSES]->:Entity` edge), so every case would merely skip rather than
   genuinely pass or fail. **Fixed**: seed through `create_conversation`, see above.
2. **BLOCKER** — after FRE-1060, `RecallAdmissionRecord.items` can contain
   producer-discarded candidates (a `pre_drop_reason` set, never reaching
   `_enrich_with_stances`); a precondition checking only kind+identity could pass on an
   entity the stance mechanism never saw. **Fixed**: precondition now also requires
   `pre_drop_reason is None` (currently unreachable in this fixture design, but correct and
   future-proof).
3. **MAJOR** — the negative-half test never asserted the control entity's own candidacy,
   and a shared-prefix name pair (`FRE1015_Python`/`FRE1015_Unrelated`) risked a full-text
   tokenizer cross-match neither confirmed nor ruled out. **Fixed**: assert the control's
   candidacy explicitly; use token-disjoint opaque names.
4. **MAJOR** — the fixture didn't guarantee `turn_entity_fulltext` exists/is ONLINE before
   relying on it. **Fixed**: `ensure_fulltext_index()` in the fixture setup.

## Post-implementation correction: which recall path actually works, live (2026-07-31)

TDD surfaced a second layer this plan did not anticipate, beyond the DISCUSSES-edge fix
above. Empirical trace (a throwaway diagnostic script against the live test Neo4j)
found:

1. **The legacy entity-match fallback (`multipath_recall_enabled=False`, the default)
   never populates `MemoryQueryResult.entities` at all** — reading `query_memory`'s
   legacy branch to completion shows it returns `MemoryQueryResult(conversations=...,
   relevance_scores=...)` with no `entities=` argument. This is confirmed independently
   by `_multipath_query_memory`'s own docstring: "entity items previously expanded into
   their most-recent turns instead, so `entities` was always empty regardless of fusion
   rank" (FRE-1021). A real DISCUSSES edge was necessary but not sufficient — this branch
   structurally cannot produce an entity-kind `memory_context` item, full stop, regardless
   of graph content. Pre-existing fact about `main`, unrelated to this ticket.
2. **The proactive path fails before it ever reaches mention-pinning.** This environment
   has no `managed_embedding_token` credential; `suggest_relevant` hits `zero_embedding`
   and returns empty immediately.
3. **What actually works, live:** `multipath_recall_enabled=True` +
   `lexical_arm_enabled=True` together. This routes `query_memory` through
   `_multipath_query_memory` — the actual FRE-1021 fix that resolves fused items back to
   real `EntityNode`s. Its dense arm fails open to empty (same missing credential); its
   lexical arm runs a real `db.index.fulltext.queryNodes('turn_entity_fulltext', ...)`
   call and found the seeded entity for real (`hit_count=50`, entity resolved into
   `.entities`, confirmed via the diagnostic before committing to this design).

Both flags are themselves real ADR-0104/FRE-723/FRE-724 production code paths, currently
flag-dark pending their own separate rollout gate (FRE-489/670) — enabling them in this
integration test is a real settings choice exercising real code, not a stub of recall
logic. The acceptance test file now sets both via an autouse `monkeypatch` fixture, with
the reasoning recorded in the module docstring so a future reader (including master's
gate) does not mistake it for an arbitrary flag flip.

This also produced a secondary, correctly-real side effect: `_seed_discussed_entity`
creates a genuine Turn, which the lexical arm also recalls as a legitimate **episode**
(the "## Relevant Past Conversations" section), rendering the entity's name as ordinary
conversation metadata ("Entities: ..."). AC-6's "no entry in any form" check was narrowed
to the entity+stance sections specifically (`_entity_and_stance_sections_of`, excluding
the episode section) — the episode section's mention is real, correct recall behaviour
unrelated to the D6 empty-affect-filter this criterion actually tests, not a defect to
suppress.

## Risks / things to verify empirically during TDD, not to guess at in this plan

- **Full-text index timing.** `turn_entity_fulltext` is a Neo4j schema index; incremental
  writes to an already-created fulltext index apply transactionally in Neo4j, not with
  Elasticsearch-style eventual consistency (codex's review found no repository
  configuration overriding this to `eventually_consistent=true`) — but this has not been
  exercised by a live integration test yet (existing FRE-1041 coverage is fake-driver only,
  `test_service_entity_resolution.py`). If a genuine lag surfaces despite
  `ensure_fulltext_index()` running first, investigate why rather than adding a wait/retry
  — a real lag would be a live-system correctness gap, not just a test flake.
- **`session_entity_names` dual role.** `resolve_message_entities`'s output feeds both
  `session_entity_names` (score-nudge) and `mentioned_entity_names` (pin) at the
  `suggest_relevant` call site — since proactive is disabled in these tests this is inert,
  but if a test flips `proactive_memory_enabled=True` for some reason later, both roles
  need the same resolved list, not two different fixtures.

## Rebase execution order

1. `git fetch origin && git rebase origin/main` on `fre-1015-topic-scoped-stance-enrichment`.
2. Resolve `memory/protocol.py`, `protocol_adapter.py`, `memory/service.py`,
   `captains_log/turn_evidence.py` as pure additive merges (keep both sides' additions).
3. Resolve `request_gateway/context.py`: adopt main's 3-tuple `_query_memory_for_intent`
   signature and `RecallDiscardReport`/`build_discarded_candidates` wiring; re-insert the
   `_enrich_with_stances` call after the 3-tuple unpack, before `recall_candidates` is built.
4. Resolve `orchestrator/executor.py`: re-apply the stance hook inside the broad-recall
   branch at its new line location; no logic change expected.
5. Rewrite `tests/personal_agent/memory/test_adr_0126_topic_scoped_stance_push.py` per
   "The fix to the tautological test" above.
6. Update the four unit test files that mock the changed signatures
   (`test_context.py`, `test_executor.py`, `test_protocol.py` for the `MemoryProtocol`
   surface; `test_memory_section_render.py`/`test_turn_evidence.py` likely unaffected
   since their inputs are already-built dicts, not call sites — confirm during TDD, don't
   assume).
7. `make test` (module, then full), `make mypy`, `make ruff-check`/`format`, `pre-commit`.
8. Re-run `code-review` (high effort — src/ memory read path, security-adjacent) and
   `security-review` per the original PR's self-review, since the rebase touches every
   file they covered.
9. Rewrite the PR body / test plan section to reflect the real (not stubbed) fixture; push
   onto **PR #738**, keep it drafted until this comment's rework is what master reviews.
10. Post a fresh Linear comment (not a new ticket) with updated AC proof — same AC-1/AC-5/
    AC-6 evidence shape as the 2026-07-29 comment, but naming the real-path fixture instead
    of the stubbed one, and confirming the rebase against FRE-1041/1061/1062.
