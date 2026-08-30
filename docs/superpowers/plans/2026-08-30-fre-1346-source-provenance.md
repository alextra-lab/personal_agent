# FRE-1346 — ADR-0098 Amendment A · T2: provenance reaches the knowledge

**Ticket:** FRE-1346 (Approved, `stream:build2`, `Tier-1:Opus`) ·
**Backing ADR:** ADR-0098 Amendment A — A3, A4, A4b, A5 (D2/D3/D6 unchanged) ·
**Prior slice:** FRE-1345 shipped `ToolDefinition.referent_parameter` (A2), merged as PR #1007 ·
**Blocks:** FRE-1347 (T3 entitlement), FRE-1348 (T4 backfill).

## Scope (5 bullets)

1. A `:Source` **node in Core** — `source_id`, `referent`, `authority`, `retrieved_at`,
   `content_hash`, `content_hash_scope`, `retained_pointer`. Metadata only; the bytes stay in the
   capture (Docs layer), so no hot query traverses Docs (D3 preserved).
2. **Association by containment, in Python at write time** — an extracted item is provenanced to the
   source(s) whose retrieved content contains its attribution string at
   `ContainmentOutcome.CONTAINED` **only**, via `grounding.containment.check_containment`.
   Attribution string: entity → name · claim → content · relationship → `source predicate target`.
3. **Nodes carry an edge**: `(:Entity|:Claim)-[:SOURCED_FROM]->(:Source)`, append-only, MERGEd in the
   **same Cypher statement** as the node write.
4. **Relationships carry a property**: `source_ids`, appended and de-duplicated on the `YIELD rel`
   the existing `apoc.merge.relationship` statement already returns. Never an edge.
5. `provenance_state` ∈ {`'provenanced'`, `'none'`} on every item written; `none → provenanced` is
   one-way, falling out of append-only set union rather than being special-cased.

## Codex plan review — findings and disposition

| # | Finding | Disposition |
|---|---|---|
| 1.1 | One shared Cypher fragment cannot serve the relationship path — no `rel` alias exists before `YIELD rel`, and A4b forbids the edge there | **Accepted.** Two fragments: `_SOURCE_MERGE_WITH_EDGE` (nodes) and `_SOURCE_MERGE_ONLY` (relationships, mints Sources before the apoc call). |
| 1.2 | No uniqueness constraint on `:Source(source_id)`; concurrent MERGE can duplicate the identity node | **Accepted.** `ensure_source_id_constraint()` following the `person_user_id_unique` precedent, called at startup beside the other `ensure_*` calls. |
| 1.3 | `SourceRecord` dataclasses cannot be passed to the driver as Cypher values | **Accepted.** Explicit `to_cypher_map()` emitting primitives only — and deliberately **excluding** `content`, which must never reach Core. |
| 1.4 / 1.5 | No defect in the `CALL { WITH e … }` placement or the two-`SET` sequencing on `YIELD rel` | Confirms the shape. `assert_claim:2716` already runs this idiom in production. |
| 2.1 | `check_containment(...) is CONTAINED` misstates the API — it returns `ContainmentResult` | **Accepted, and it was the dangerous one.** Implemented as `.outcome is ContainmentOutcome.CONTAINED`. Written literally it is always false — i.e. exactly the universal-`none` implementation AC-1(a) exists to reject. |
| 2.2 / 3.6 | A5 legacy reconstruction and the AC-A5 sentinel are absent | **Rejected for this ticket.** FRE-1348 (T4) *is* "provenance_state backfill: reconstruct from captures before marking none", and this ticket blocks it. Reviewing against the whole Amendment rather than the slice; one phase = one PR. Flagged in the handoff so master can confirm the split. |
| 2.3 / 4.3 | `create_conversation`'s inline bare `:Entity` MERGE creates nodes with **no** `provenance_state` — a post-change silent third state, not a legacy one | **Accepted — real hole, folded in.** `e.provenance_state = COALESCE(e.provenance_state, 'none')` on that MERGE. Reachable today: the consolidator deliberately falls through to it when `create_entity` fails (`consolidator.py:804-829`). |
| 2.4 | `authority` stored but its derivation never defined | **Accepted.** Defined: `urlsplit(referent).hostname` lowercased for `http(s)`, else the referent verbatim. Own unit test, incl. two versions of one page → one authority. |
| 2.5 | Pointer/hash integrity has no resolver or read-side check | **Partially accepted — recorded, not built.** T2 has no read consumer; a resolver now would be speculative. Decision recorded here and in the PR: integrity checking lands with the consumer. The `ON CREATE` pointer is deliberate (stable identity); the residual risk is a pointer dangling if its capture ages out. |
| 3.2 | Consolidator tests pass even if the service ignores `source_records` | **Accepted.** Cypher-shape tests *plus* a live Neo4j file that asserts the stored structures — and it is actually run (see below), not merely authored. |
| 3.3 | AC-1's negatives exercise only entities; relationships could be broken and still pass | **Accepted.** Relationship-specific seeded negatives added. |
| 3.4 | The live tests are excluded from `make test`, so AC-4/AC-5 rest on substring assertions that pass with invalid Cypher | **Accepted — the sharpest finding.** The live file is executed against the isolated test Neo4j (:7688) via `make test-infra-up`, and the observed output is the handoff evidence. Neo4j only; no LLM server, so the CLAUDE.md prohibition on LLM-requiring integration runs is not engaged. |
| 3.5 | AC-5 never asserts each stored id resolves to exactly one `:Source` | **Accepted.** Added to the live test — this is the A4b referential-integrity check. |
| 4.2 | `store_fact` (owner-provided knowledge) gets stamped `none`, which A6 would later read as `AGENT_DERIVED` | **Correct observation, but `none` is the right stamp here** — A5 permits exactly two values and forbids a third. The defect it predicts is real and belongs to T3: FRE-1347 must not map `none → AGENT_DERIVED` blindly, or owner statements silently lose `USER_STATED`. **Comment posted on FRE-1347** rather than a code change here. |

**My own finding, which the review did not reach.** `fetch_url` returns
`{"url", "text", "char_count", "truncated"}`, so rendering the whole result puts the **URL itself**
into the content the containment check sees — A4's false-association shape arriving through the
address rather than the content. Fold-in: reuse the existing value-driven argument-echo strip from
`grounding/source_registry.py` (made public — it is one definition of "the model's arguments
returning", not a second), plus a third seeded negative.

**Measured, not assumed, after the first version of that negative proved vacuous.** The risk fires
for **path and query segments** (`/wiki/SafeCart`, `?q=SafeCart`) and **not** for hostnames:
`normalize_tokens` keeps a dotted host as a single token, so `acmewidgets.example.com` never matches
`AcmeWidgets`. My first fixture used a host-only URL and passed with the strip removed — it was
testing nothing. The negative now parametrizes the three shapes that actually fire, and mutation
testing confirms all three fail without the strip.

## Decisions on the ticket's carried-open items

| Carried open | Decision | Why |
|---|---|---|
| How `content_hash` is computed | `sha256` over **the content actually checked** (post-echo-strip), with `content_hash_scope='captured_output_stripped'` stored on the node | The hash must certify what the check saw. Hashing bytes we never read would make "the page moved" undetectable in exactly the truncation case while claiming otherwise; the scope property keeps that limit visible. |
| Pointer to retained bytes | `capture://<trace_id>#tool_results/<index>` | The on-disk capture **is** the retained-bytes layer today; an R2 pointer would be a dangling join. |
| `retrieved_at` | `capture.timestamp` | Tool results carry `latency_ms` but no timestamp. Stated in the docstring rather than implied to be per-call. |
| False-negative rate | Per-capture counters `*_none_with_sources`, returned and logged | A5 requires the rate be countable, not the check widened. Counts the interesting population: items at `none` **while a source existed**. |

## Deliberate non-scope

Stances (owner-stated, not extracted world knowledge) · `verify_turn` (A4/A6 forbid the wiring — it
would put the inline path into Docs and break D3) · backfill + the `IS NULL` sentinel (**FRE-1348**) ·
entitlement following the terminus (**FRE-1347**) · `web_search` per-result referents (A2 defers).

## Steps

1. **`memory/provenance.py` (new)** — `SourceRecord` (frozen, `to_cypher_map()`),
   `sources_from_tool_results(...)`, `attribution_for_relationship(...)`, `associate(...)`.
   → `make test-file FILE=tests/personal_agent/memory/test_provenance.py`
2. **`grounding/source_registry.py`** — make `_strip_argument_echo` public; update call sites.
3. **`memory/service.py`** — two Cypher fragments; `source_records=()` on `create_entity` /
   `assert_claim` / `create_relationship`; `provenance_state` on the `create_conversation` inline
   MERGE; `ensure_source_id_constraint()`. **`service/app.py`** — startup call.
   → `make test-file FILE=tests/personal_agent/memory/test_provenance_cypher.py`
4. **`second_brain/consolidator.py`** — derive sources once per capture, associate per item, pass
   matched records to the three writes, add the four counters + summary log.
   → `make test-file FILE=tests/personal_agent/second_brain/test_consolidator_provenance.py`
5. **Live proof** — `make test-infra-up`, then run `test_provenance_live.py` against :7688.
6. **Gates** — `make test` · `make mypy` · `make ruff-check` · `make ruff-format` · `pre-commit run --all-files`.

## Acceptance criteria → evidence

| AC | Test | Asserted outcome |
|---|---|---|
| **AC-1** provenance reaches knowledge, and the reference actually supports it | `test_provenance.py`, `test_consolidator_provenance.py`, `test_provenance_live.py` | An entity named in a fetched page gets that page's `source_id`. **Negative (a)** contained entity must not come back `none` (kills universal-`none`, and the `ContainmentResult` bug). **Negative (b)** entity absent from every result gets **no** source (kills turn-level attribution). **Negative (c)** entity present only in the URL argument gets no source. Each repeated for relationships. |
| **AC-2** address survives the consolidation window | `test_consolidator_provenance.py` | Consolidation driven from the on-disk capture with no live session; `:Source.referent` == the fetched URL. |
| **AC-3** bus-independent | `test_consolidator_provenance.py` | Same assertion with the bus disabled. |
| **AC-4** merged canonical entity accumulates | `test_provenance_live.py` (**run**, not just authored) | Two writes of one name from distinct sources ⇒ **two** `SOURCED_FROM` edges, neither lost. |
| **AC-5** relationship path works and is not an edge | `test_provenance_cypher.py` + `test_provenance_live.py` | `rel.source_ids` accumulates via `apoc.coll.toSet(coalesce(...) + $source_ids)`; **no** `SOURCED_FROM` in the relationship statement; every stored id resolves to **exactly one** `:Source`. |

## Risk

Production KG write path. **Diff class: escalated.** Every new clause is additive and
`source_records=()` (the default for all existing callers) emits an empty `UNWIND`, so existing
behaviour is unchanged apart from the `provenance_state` stamp — which gets its own "empty is a
no-op" test.
